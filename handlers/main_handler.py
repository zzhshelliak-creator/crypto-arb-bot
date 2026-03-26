import asyncio
import logging
import time
import shared
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext

from handlers.states import SetAmount, SetMinProfit, SetFilters, SetBankFee, AddParticipant
from aiogram.exceptions import TelegramBadRequest
from handlers.keyboards import (
    main_menu_kb, main_text, scan_kb, retry_kb, live_kb, autoscan_status_kb,
    opportunity_kb, opportunities_list_kb,
    settings_kb, risk_level_kb, network_kb, banks_kb, banks_menu_kb, exchanges_kb, participants_kb,
    amount_kb, trading_mode_kb, presets_kb, cancel_input_kb, antiscam_kb, arb_types_kb,
)
from models.types import UserSettings
from utils.formatters import (
    format_opportunity, format_opportunities_list, format_scan_report,
    format_analytics, format_settings, format_favorites
)
from services.analytics import (
    record_scan, get_stats, save_favorite, get_favorites,
    get_participants, add_participant, remove_participant,
)
from storage import settings_storage

logger = logging.getLogger(__name__)
router = Router()

# Load persisted settings from disk on startup
user_settings: dict[int, UserSettings] = settings_storage.load_all()
user_opportunities: dict[int, list] = {}
user_live_tasks: dict[int, asyncio.Task] = {}
user_temp_buy_banks: dict[int, list] = {}
user_temp_sell_banks: dict[int, list] = {}
user_temp_exchanges: dict[int, list] = {}
user_temp_arb_types: dict[int, list] = {}


_OPP_TTL_SECONDS = 90   # Opportunity notification lives for 90 seconds


def _save_settings():
    """Persist all user settings to disk (non-blocking, best-effort)."""
    try:
        settings_storage.save_all(user_settings)
    except Exception as e:
        logger.warning(f"Failed to persist settings: {e}")


async def _schedule_delete(chat_id: int, message_id: int, delay: int = _OPP_TTL_SECONDS):
    """Видаляє повідомлення через delay секунд (за замовчуванням 30 хв)."""
    await asyncio.sleep(delay)
    try:
        await shared.bot_instance.delete_message(chat_id=chat_id, message_id=message_id)
        logger.debug(f"Auto-deleted message {message_id} in chat {chat_id} after {delay}s")
    except Exception:
        pass


def _expire_header(delay: int = _OPP_TTL_SECONDS) -> str:
    """Статичний рядок-шапка з часом закінчення."""
    expire_at = time.time() + delay
    expire_hm = time.strftime("%H:%M:%S", time.localtime(expire_at))
    return f"🔴 <b>АКТУАЛЬНО {delay} СЕК</b> • автовидалення о {expire_hm}"


def _countdown_line(expire_at: float) -> str:
    """Динамічний рядок відліку залишкового часу."""
    remaining = int(expire_at - time.time())
    if remaining <= 0:
        return "🔴 <b>ОРДЕР ЗАСТАРІВ</b> — не торгувати!"
    secs = remaining % 60
    mins = remaining // 60
    expire_hm = time.strftime("%H:%M:%S", time.localtime(expire_at))
    if mins >= 1:
        time_str = f"{mins} хв {secs:02d} с"
    else:
        urgency = "⚠️" if remaining > 30 else "🔴"
        time_str = f"{remaining} с"
        return f"{urgency} <b>ЗАЛИШИЛОСЬ {time_str}</b> • автовидалення о {expire_hm}"
    return f"⏳ <b>Залишилось {time_str}</b> • автовидалення о {expire_hm}"


async def _live_countdown_and_delete(
    chat_id: int,
    message_id: int,
    body_text: str,
    reply_markup,
    delay: int = _OPP_TTL_SECONDS,
):
    """
    Оновлює відлік кожні 15 с, потім видаляє повідомлення.
    """
    expire_at = time.time() + delay
    interval = 15

    while True:
        await asyncio.sleep(interval)
        remaining = expire_at - time.time()
        if remaining <= 5:
            break
        header = _countdown_line(expire_at)
        new_text = header + "\n—\n" + body_text
        try:
            await shared.bot_instance.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=new_text,
                reply_markup=reply_markup,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            pass  # Message already deleted / edited by user navigation

    try:
        await shared.bot_instance.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


user_temp_trading_mode: dict[int, str] = {}

# ─── Single-window tracking ────────────────────────────────────────────────
# Only ONE bot message per user is the "dialog window" (pinned at top).
# Every menu navigation edits this message in-place.
user_main_msg: dict[int, int] = {}   # user_id -> message_id of pinned dialog window


async def _set_main_msg(bot, chat_id: int, user_id: int, message_id: int) -> None:
    """Pin the new main message and delete the old one. Silent on errors."""
    # Read old ID from in-memory first, then fall back to persisted settings
    old_id = user_main_msg.get(user_id)
    if not old_id:
        settings = get_settings(user_id)
        old_id = settings.main_msg_id

    user_main_msg[user_id] = message_id

    # Persist to settings so it survives Railway redeploys
    settings = get_settings(user_id)
    settings.main_msg_id = message_id
    _save_settings()

    if old_id and old_id != message_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=old_id)
        except Exception:
            pass
    try:
        await bot.pin_chat_message(
            chat_id=chat_id, message_id=message_id, disable_notification=True
        )
    except Exception:
        pass


async def _try_delete(bot_or_msg, chat_id: int = None, message_id: int = None):
    """Silently delete a message."""
    try:
        if chat_id and message_id:
            await bot_or_msg.delete_message(chat_id=chat_id, message_id=message_id)
        else:
            await bot_or_msg.delete()
    except Exception:
        pass


# Auto-scan tracking
user_autoscan_status_msg: dict[int, int] = {}   # user_id -> message_id of status msg
user_autoscan_scan_count: dict[int, int] = {}   # user_id -> total scans done
user_autoscan_found_count: dict[int, int] = {}  # user_id -> total opportunities found
user_autoscan_last_fp: dict[int, str] = {}      # user_id -> fingerprint of last sent opp
user_autoscan_start_time: dict[int, float] = {} # user_id -> start timestamp


def _opp_fingerprint(opps: list) -> str:
    if not opps:
        return ""
    top = opps[0]
    return f"{top.buy_exchange}-{top.sell_exchange}-{top.arb_type}-{round(top.profit_uah / 50) * 50}"


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s} сек"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m} хв {s} сек"
    h, m = divmod(m, 60)
    return f"{h} год {m} хв"


def get_settings(user_id: int) -> UserSettings:
    if user_id not in user_settings:
        user_settings[user_id] = UserSettings()
    return user_settings[user_id]


@router.message(CommandStart())
async def cmd_start(message: Message):
    uid = message.from_user.id
    settings = get_settings(uid)
    await _try_delete(message)   # delete user's /start message
    sent = await message.answer(
        "👋 <b>Crypto Arbitrage Bot</b>\n\n"
        "Знаходжу реальні арбітражні можливості на P2P та Spot ринках.\n"
        "Тільки реальні угоди після всіх комісій.\n\n"
        + main_text(settings),
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )
    await _set_main_msg(message.bot, message.chat.id, uid, sent.message_id)


@router.message(Command("menu"))
async def cmd_menu(message: Message):
    uid = message.from_user.id
    settings = get_settings(uid)
    await _try_delete(message)   # delete user's /menu message
    main_id = user_main_msg.get(uid)
    if main_id:
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id, message_id=main_id,
                text=main_text(settings), reply_markup=main_menu_kb(), parse_mode="HTML",
            )
            return
        except TelegramBadRequest:
            pass
    sent = await message.answer(main_text(settings), reply_markup=main_menu_kb(), parse_mode="HTML")
    await _set_main_msg(message.bot, message.chat.id, uid, sent.message_id)


@router.message(Command("help"))
async def cmd_help(message: Message):
    help_text = """
🤖 <b>CRYPTO ARBITRAGE BOT — ПОВНИЙ СПИСОК ФУНКЦІЙ</b>

<b>📊 АРБІТРАЖ & СКАНУВАННЯ</b>
✅ Сканування з 8 бірж: Binance, Bybit, OKX, Bitget, MEXC, Gate.io, HTX, KuCoin
✅ 4 типи арбітражу (P2P↔P2P, Same-exchange, P2P › Spot, Triangular)
✅ Live Mode 24/7 з автосповіщеннями

<b>💰 РОЗРАХУНКИ</b>
✅ Точна математика ПІСЛЯ всіх комісій (мережа, банк, спот)
✅ Показує дві біржи в арбітражі: 🔀 Binance › Bybit
✅ Реалізм: ТІЛЬКИ профітні ордери (не теоретичні)

<b>🛡️ АНТИ-СКАМ ФІЛЬТРИ</b>
✅ Мінімум 95% completion, 30+ ордерів, продавець online
✅ Макс 15 хвилин час відповіді, ±3% від ринку

<b>⚙️ НАЛАШТУВАННЯ</b>
✅ 3 готові профілі (Conservative/Balanced/Aggressive)
✅ Вибір бірж, банків, мережі, мін-профіту
✅ Спосіб торгівлі (напряму / як 3 особа)
✅ Рівень ризику (LOW/MEDIUM/HIGH)

<b>📱 МЕНЮ</b>
🔍 SCAN MODE — одноразове сканування
⚡ LIVE MODE — автосканування 24/7
⚙️ SETTINGS — всі налаштування + профілі
❤️ FAVORITES — збереження можливостей
📊 ANALYTICS — статистика
👥 PARTICIPANTS — групове сканування

<b>🔧 ТЕХНІЧНІ ОСОБЛИВОСТІ</b>
✅ Fallback API (при блокуванні працює далі)
✅ Real-time ціни, актуальні комісії
✅ Кеш на 8 сек (не перевантажує API)

<b>📈 ГОТОВІ ПРОФІЛІ</b>
🟢 <b>Conservative:</b> 30K UAH, мін 200 грн
🟡 <b>Balanced (Рекомендовано):</b> 20K UAH, мін 50 грн
🔴 <b>Aggressive:</b> 10K UAH, мін 25 грн

<b>Команди:</b>
/start — початок
/menu — головне меню
/help — цей список

Натиснути /menu або натисни кнопку внизу!
"""
    await message.answer(help_text, parse_mode="HTML")


@router.callback_query(F.data == "back_main")
async def cb_back_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = call.from_user.id
    # Sync main message tracking (restores after bot restart)
    user_main_msg[uid] = call.message.message_id
    settings = get_settings(uid)
    try:
        await call.message.edit_text(main_text(settings), reply_markup=main_menu_kb(), parse_mode="HTML")
    except TelegramBadRequest:
        pass
    await call.answer()


@router.callback_query(F.data == "menu_scan")
async def cb_menu_scan(call: CallbackQuery):
    # Перенаправляємо напряму на скан — без проміжного екрану
    await cb_scan_start(call)


@router.callback_query(F.data == "scan_start")
async def cb_scan_start(call: CallbackQuery):
    settings = get_settings(call.from_user.id)
    await call.message.edit_text(
        "⏳ <b>Сканую ринок...</b>\n\nЗбираю P2P ордери та аналізую можливості...",
        parse_mode="HTML",
    )
    await call.answer()

    try:
        opportunities = await shared.arb_engine.scan(settings)
        if opportunities:
            await call.message.edit_text(
                "🔎 <b>Знайдено можливості — перевіряю актуальність цін...</b>",
                parse_mode="HTML",
            )
            opportunities = await shared.arb_engine.verify_opportunities(opportunities, settings)
            # Відкидаємо можливості, що не пройшли перевірку — ціна вже змінилась
            opportunities = [o for o in opportunities if o.verified]
        record_scan(opportunities)
        user_opportunities[call.from_user.id] = opportunities

        if not opportunities:
            stats = shared.arb_engine.last_scan_stats
            amount = stats.get("amount_uah", settings.amount_uah)
            min_viable = stats.get("min_viable_amount", 14100)
            network = stats.get("network", settings.network)

            # Find closest opportunities even if they don't match filters
            closest = await shared.arb_engine.find_closest_opportunities(
                shared.arb_engine.last_buy_orders,
                shared.arb_engine.last_sell_orders,
                settings,
            )

            amount_warn = ""
            if amount < min_viable:
                amount_warn = (
                    f"\n⚠️ Для крос-біржового арбітражу потрібно мін. "
                    f"<b>~{min_viable:,.0f} грн</b> (комісія мережі {network} ≈ 41 грн)."
                )

            text = (
                f"😔 <b>За твоїми фільтрами — 0 можливостей</b>\n\n"
                f"📊 Сканування: {stats.get('total_raw', 0)} ордерів › "
                f"{stats.get('trusted_buys', 0)} buy / {stats.get('trusted_sells', 0)} sell пройшли анти-скам"
                f"{amount_warn}\n\n"
            )

            if closest:
                text += "📍 <b>Найближчі до прибуткових (без твого фільтру):</b>\n\n"
                for i, c in enumerate(closest, 1):
                    profit = c["profit_uah"]
                    gap = c["gap_uah"]
                    spread = c["spread_pct"]
                    b_ex = c["buy_exchange"]
                    s_ex = c["sell_exchange"]
                    is_cross = b_ex != s_ex

                    if profit > 0:
                        profit_icon = "🟡"
                        profit_str = f"+{profit:,.0f} грн"
                    else:
                        profit_icon = "🔴"
                        profit_str = f"{profit:,.0f} грн"

                    if is_cross:
                        pair_str = f"🔀 {b_ex} › {s_ex}"
                        fee_note = f" (мережева комісія: {c['network_fee_uah']:.0f} грн)"
                    else:
                        pair_str = f"🔄 {b_ex}"
                        fee_note = ""

                    text += (
                        f"{profit_icon} <b>{pair_str}</b>\n"
                        f"   Спред: {spread:.3f}% | Профіт: <b>{profit_str}</b>{fee_note}\n"
                    )

                    if gap > 0:
                        needed = c.get("needed_amount_uah", 0)
                        text += f"   💡 Не вистачає: {gap:,.0f} грн до мін. профіту\n"
                        if needed > amount and needed > 0:
                            text += f"   📈 Збільш суму до ~{needed:,.0f} грн — і буде профітно\n"
                    text += "\n"

                text += (
                    "🔧 <b>Що змінити:</b>\n"
                    f"• Зменш мін. профіт в Settings\n"
                    f"• Збільш суму (💰 Amount)\n"
                    f"• Обери BEP20, SOL або APT (менша комісія)"
                )
            else:
                text += (
                    "🔧 <b>Що спробувати:</b>\n"
                    f"• Збільш суму (💰 Amount)\n"
                    f"• Зменш мін. профіт (Settings)\n"
                    f"• Ризик MEDIUM або HIGH\n"
                    f"• Обери мережу BEP20, SOL або APT"
                )

            await call.message.edit_text(text, reply_markup=retry_kb(), parse_mode="HTML")
        else:
            uid = call.from_user.id
            is_autoscan = uid in user_live_tasks and not user_live_tasks[uid].done()
            scan_stats = shared.arb_engine.last_scan_stats
            body = format_opportunities_list(opportunities)
            report = format_scan_report(scan_stats)
            text = _expire_header() + "\n—\n" + body + "\n—\n" + report
            await call.message.edit_text(
                text,
                reply_markup=opportunities_list_kb(opportunities, autoscan_running=is_autoscan),
                parse_mode="HTML",
            )
            asyncio.create_task(
                _schedule_delete(call.message.chat.id, call.message.message_id)
            )
    except Exception as e:
        logger.error(f"Scan error: {e}", exc_info=True)
        await call.message.edit_text(
            f"❌ Помилка сканування: {str(e)[:100]}\n\nСпробуй ще раз.",
            reply_markup=retry_kb(),
            parse_mode="HTML",
        )


@router.callback_query(F.data == "opp_list")
async def cb_opp_list(call: CallbackQuery):
    opps = user_opportunities.get(call.from_user.id, [])
    if not opps:
        await call.message.edit_text(
            "🔍 Немає збережених результатів. Натисни 🔍 Сканувати для пошуку.",
            reply_markup=retry_kb(),
            parse_mode="HTML",
        )
        await call.answer()
        return
    uid = call.from_user.id
    is_autoscan = uid in user_live_tasks and not user_live_tasks[uid].done()
    text = format_opportunities_list(opps)
    await call.message.edit_text(text, reply_markup=opportunities_list_kb(opps, autoscan_running=is_autoscan), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("opp_detail_"))
async def cb_opp_detail(call: CallbackQuery):
    index = int(call.data.split("_")[-1])
    opps = user_opportunities.get(call.from_user.id, [])
    if not opps or index >= len(opps):
        await call.answer("Можливість не знайдена", show_alert=True)
        return
    opp = opps[index]
    s = get_settings(call.from_user.id)
    text = format_opportunity(opp, index + 1, s.trading_mode, getattr(s, "bank_fee_uah", 0.0), buy_banks=s.buy_banks, sell_banks=s.sell_banks)
    await call.message.edit_text(text, reply_markup=opportunity_kb(index, len(opps)), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("opp_next_"))
async def cb_opp_next(call: CallbackQuery):
    index = int(call.data.split("_")[-1]) + 1
    opps = user_opportunities.get(call.from_user.id, [])
    if not opps or index >= len(opps):
        await call.answer("Кінець списку")
        return
    opp = opps[index]
    s = get_settings(call.from_user.id)
    text = format_opportunity(opp, index + 1, s.trading_mode, getattr(s, "bank_fee_uah", 0.0), buy_banks=s.buy_banks, sell_banks=s.sell_banks)
    await call.message.edit_text(text, reply_markup=opportunity_kb(index, len(opps)), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("opp_prev_"))
async def cb_opp_prev(call: CallbackQuery):
    index = int(call.data.split("_")[-1]) - 1
    if index < 0:
        await call.answer("Початок списку")
        return
    opps = user_opportunities.get(call.from_user.id, [])
    if not opps or index >= len(opps):
        await call.answer("Список застарів — зроби новий скан", show_alert=True)
        return
    opp = opps[index]
    s = get_settings(call.from_user.id)
    text = format_opportunity(opp, index + 1, s.trading_mode, getattr(s, "bank_fee_uah", 0.0), buy_banks=s.buy_banks, sell_banks=s.sell_banks)
    try:
        await call.message.edit_text(text, reply_markup=opportunity_kb(index, len(opps)), parse_mode="HTML")
    except TelegramBadRequest:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("opp_save_"))
async def cb_opp_save(call: CallbackQuery):
    index = int(call.data.split("_")[-1])
    opps = user_opportunities.get(call.from_user.id, [])
    if not opps or index >= len(opps):
        await call.answer("Не знайдено", show_alert=True)
        return
    save_favorite(opps[index])
    await call.answer("⭐ Збережено!", show_alert=True)


@router.callback_query(F.data == "menu_live")
async def cb_menu_live(call: CallbackQuery):
    uid = call.from_user.id
    is_running = uid in user_live_tasks and not user_live_tasks[uid].done()
    settings = get_settings(uid)

    if is_running:
        scan_count = user_autoscan_scan_count.get(uid, 0)
        found_count = user_autoscan_found_count.get(uid, 0)
        elapsed = _fmt_duration(time.time() - user_autoscan_start_time.get(uid, time.time()))
        status_text = (
            f"🤖 <b>Авто-Скан активний</b>\n\n"
            f"🟢 Статус: <b>Сканую...</b>\n"
            f"⏱ Запущено: <b>{elapsed} тому</b>\n"
            f"🔄 Сканувань: <b>{scan_count}</b>\n"
            f"💰 Знайдено можливостей: <b>{found_count}</b>\n"
            f"⏳ Інтервал: <b>{settings.scan_interval} сек</b>\n\n"
            f"Отримаю сповіщення коли знайду нову можливість."
        )
    else:
        status_text = (
            f"🤖 <b>Авто-Скан</b>\n\n"
            f"🔴 Статус: <b>Зупинено</b>\n"
            f"⏳ Інтервал: <b>{settings.scan_interval} сек</b>\n\n"
            f"Бот буде безперервно сканувати ринок і надсилати сповіщення коли знайде "
            f"реальну прибуткову можливість після всіх комісій."
        )

    await call.message.edit_text(
        status_text,
        reply_markup=live_kb(is_running),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data == "live_start")
async def cb_live_start(call: CallbackQuery):
    uid = call.from_user.id
    if uid in user_live_tasks and not user_live_tasks[uid].done():
        await call.answer("🤖 Авто-Скан вже запущено!", show_alert=True)
        return

    settings = get_settings(uid)

    # Init tracking state
    user_autoscan_scan_count[uid] = 0
    user_autoscan_found_count[uid] = 0
    user_autoscan_last_fp[uid] = ""
    user_autoscan_start_time[uid] = time.time()

    # Navigate to main menu so the user can keep using the bot normally
    # (auto-scan runs in background, sends NEW messages when it finds something)
    await call.message.edit_text(
        f"🤖 <b>Авто-Скан запущено!</b>\n\n"
        f"🟢 Сканую ринок у фоні кожні <b>{settings.scan_interval} сек</b>.\n"
        f"Надішлю окреме повідомлення щойно знайду прибуткову можливість.\n\n"
        f"Продовжуй користуватись ботом — авто-скан не заважає навігації.",
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )
    await call.answer("▶️ Авто-Скан запущено!")

    task = asyncio.create_task(_live_loop(call.message.chat.id, uid))
    user_live_tasks[uid] = task


@router.callback_query(F.data == "live_stop")
async def cb_live_stop(call: CallbackQuery):
    uid = call.from_user.id
    if uid in user_live_tasks:
        user_live_tasks[uid].cancel()
        del user_live_tasks[uid]

    scan_count = user_autoscan_scan_count.get(uid, 0)
    found_count = user_autoscan_found_count.get(uid, 0)
    elapsed = _fmt_duration(time.time() - user_autoscan_start_time.get(uid, time.time()))

    await call.message.edit_text(
        f"⏸ <b>Авто-Скан зупинено</b>\n\n"
        f"📊 Підсумок сесії:\n"
        f"├ Тривалість: <b>{elapsed}</b>\n"
        f"├ Сканувань виконано: <b>{scan_count}</b>\n"
        f"└ Можливостей знайдено: <b>{found_count}</b>\n\n"
        f"Запусти знову коли буде потрібно.",
        reply_markup=live_kb(False),
        parse_mode="HTML",
    )
    await call.answer("⏹ Зупинено")

    # Cleanup
    for d in (user_autoscan_scan_count, user_autoscan_found_count,
              user_autoscan_last_fp, user_autoscan_start_time, user_autoscan_status_msg):
        d.pop(uid, None)


async def _live_loop(chat_id: int, user_id: int):
    """
    Runs in the background. Does NOT edit any existing messages (to avoid
    interfering with the user's navigation). Only sends NEW messages when a
    genuinely new profitable opportunity is discovered.
    """
    while True:
        try:
            # Re-read settings each iteration so changes take effect immediately
            settings = get_settings(user_id)

            scan_count = user_autoscan_scan_count.get(user_id, 0) + 1
            user_autoscan_scan_count[user_id] = scan_count

            opps = await shared.arb_engine.scan(settings)
            if opps:
                opps = await shared.arb_engine.verify_opportunities(opps, settings)
                # Відкидаємо можливості що не пройшли перевірку — ціна вже змінилась на момент повтор.запиту
                opps = [o for o in opps if o.verified]
            # Drop stale opportunities — never notify about orders older than TTL
            now = time.time()
            opps = [o for o in opps if o.scanned_at <= 0 or (now - o.scanned_at) <= _OPP_TTL_SECONDS]
            record_scan(opps)
            user_opportunities[user_id] = opps

            found_total = user_autoscan_found_count.get(user_id, 0)
            new_fp = _opp_fingerprint(opps)
            last_fp = user_autoscan_last_fp.get(user_id, "")

            if opps and new_fp != last_fp:
                # New opportunity found — send a fresh notification message
                user_autoscan_last_fp[user_id] = new_fp
                user_autoscan_found_count[user_id] = found_total + len(opps)

                top = opps[0]
                elapsed = _fmt_duration(time.time() - user_autoscan_start_time.get(user_id, time.time()))
                scan_report = format_scan_report(shared.arb_engine.last_scan_stats)
                alert_body = (
                    f"🔔 <b>Авто-Скан: нова можливість!</b>\n"
                    f"скан #{scan_count} • запущено {elapsed} тому\n\n"
                    + format_opportunity(top, 1, settings.trading_mode, getattr(settings, "bank_fee_uah", 0.0), buy_banks=settings.buy_banks, sell_banks=settings.sell_banks)
                    + f"\n\n🔍 Всього знайдено в цьому скані: {len(opps)}"
                    + "\n—\n" + scan_report
                )
                alert_kb = opportunities_list_kb(opps, autoscan_running=True)
                alert_text = _expire_header() + "\n—\n" + alert_body
                sent = await shared.bot_instance.send_message(
                    chat_id, alert_text,
                    parse_mode="HTML",
                    reply_markup=alert_kb,
                )
                asyncio.create_task(
                    _live_countdown_and_delete(chat_id, sent.message_id, alert_body, alert_kb)
                )

                # Notify participants
                participants = get_participants(user_id)
                for p in participants:
                    try:
                        p_body = (
                            f"🔔 <b>Live Alert від власника!</b>\n\n"
                            + format_opportunity(top, 1, settings.trading_mode, getattr(settings, "bank_fee_uah", 0.0), buy_banks=settings.buy_banks, sell_banks=settings.sell_banks)
                            + f"\n\n🔍 Всього знайдено: {len(opps)}"
                        )
                        p_text = _expire_header() + "\n—\n" + p_body
                        p_sent = await shared.bot_instance.send_message(
                            p["user_id"], p_text,
                            parse_mode="HTML",
                        )
                        asyncio.create_task(_schedule_delete(p["user_id"], p_sent.message_id))
                    except Exception as pe:
                        logger.warning(f"Failed to notify participant {p['user_id']}: {pe}")

            elif not opps and last_fp:
                # Opportunities disappeared — reset fingerprint so next find triggers alert
                user_autoscan_last_fp[user_id] = ""

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Auto-scan loop error: {e}")

        await asyncio.sleep(settings.scan_interval)


def _amount_prompt_text(current: float) -> str:
    return (
        f"💰 <b>Сума для торгівлі</b>\n\n"
        f"Поточна: <b>{current:,.0f} UAH</b>\n\n"
        f"Просто напиши будь-яке число — або обери швидкий варіант:"
    )


@router.callback_query(F.data == "menu_amount")
async def cb_menu_amount(call: CallbackQuery, state: FSMContext):
    settings = get_settings(call.from_user.id)
    await call.message.edit_text(
        _amount_prompt_text(settings.amount_uah),
        reply_markup=amount_kb(settings.amount_uah),
        parse_mode="HTML",
    )
    await state.set_state(SetAmount.waiting_for_amount)
    await state.update_data(bot_msg_id=call.message.message_id, chat_id=call.message.chat.id)
    await call.answer()


@router.callback_query(F.data.startswith("amount_set_"))
async def cb_amount_preset(call: CallbackQuery, state: FSMContext):
    await state.clear()
    value = float(call.data.split("amount_set_")[1])
    settings = get_settings(call.from_user.id)
    settings.amount_uah = value
    _save_settings()
    await call.message.edit_text(
        f"✅ Сума: <b>{value:,.0f} грн</b>",
        reply_markup=settings_kb(settings),
        parse_mode="HTML",
    )
    await call.answer(f"✅ {value:,.0f} грн")


@router.callback_query(F.data == "amount_custom")
async def cb_amount_custom(call: CallbackQuery, state: FSMContext):
    settings = get_settings(call.from_user.id)
    await call.message.edit_text(
        f"✏️ <b>Введіть суму вручну</b>\n\n"
        f"Поточна: <b>{settings.amount_uah:,.0f} UAH</b>\n\n"
        f"Напишіть будь-яке число в гривнях.\n"
        f"Наприклад: <code>7500</code> або <code>123456</code>",
        parse_mode="HTML",
    )
    await state.set_state(SetAmount.waiting_for_amount)
    await state.update_data(bot_msg_id=call.message.message_id, chat_id=call.message.chat.id)
    await call.answer()


async def _edit_or_answer(message: Message, bot_msg_id: int | None, chat_id: int | None,
                          text: str, reply_markup=None, parse_mode="HTML"):
    """Редагує попереднє бот-повідомлення або відправляє нове і пінить його."""
    uid = message.from_user.id
    effective_id = bot_msg_id or user_main_msg.get(uid)
    effective_chat = chat_id or message.chat.id
    if effective_id and effective_chat:
        try:
            await message.bot.edit_message_text(
                chat_id=effective_chat, message_id=effective_id,
                text=text, reply_markup=reply_markup, parse_mode=parse_mode,
            )
            return
        except TelegramBadRequest:
            pass
    # Fallback: send new message and make it the new main window
    sent = await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
    await _set_main_msg(message.bot, message.chat.id, uid, sent.message_id)


@router.message(SetAmount.waiting_for_amount)
async def process_amount(message: Message, state: FSMContext):
    data = await state.get_data()
    bot_msg_id = data.get("bot_msg_id")
    chat_id = data.get("chat_id")
    try:
        await message.delete()
    except Exception:
        pass
    try:
        raw = message.text.strip().replace(",", "").replace(" ", "").replace("грн", "").replace("uah", "").replace("UAH", "")
        amount = float(raw)
        if amount <= 0:
            await _edit_or_answer(message, bot_msg_id, chat_id, "❌ Сума має бути більше 0")
            return
        if amount > 100_000_000:
            await _edit_or_answer(message, bot_msg_id, chat_id, "❌ Занадто велика сума. Максимум: 100 000 000 UAH")
            return
        settings = get_settings(message.from_user.id)
        settings.amount_uah = amount
        _save_settings()
        await state.clear()
        await _edit_or_answer(
            message, bot_msg_id, chat_id,
            f"✅ Сума: <b>{amount:,.0f} грн</b>",
            reply_markup=settings_kb(settings),
        )
    except ValueError:
        await _edit_or_answer(
            message, bot_msg_id, chat_id,
            "❌ <b>Невірний формат</b>\n\nВведіть просто число, наприклад:\n"
            "<code>7500</code>\n<code>50000</code>\n<code>123456.78</code>",
        )


@router.callback_query(F.data == "menu_settings")
async def cb_menu_settings(call: CallbackQuery, state: FSMContext):
    await state.clear()
    settings = get_settings(call.from_user.id)
    text = format_settings(settings)
    try:
        await call.message.edit_text(text, reply_markup=settings_kb(settings), parse_mode="HTML")
    except TelegramBadRequest:
        pass
    await call.answer()


@router.callback_query(F.data == "set_amount")
async def cb_set_amount(call: CallbackQuery, state: FSMContext):
    settings = get_settings(call.from_user.id)
    await call.message.edit_text(
        _amount_prompt_text(settings.amount_uah),
        reply_markup=amount_kb(settings.amount_uah),
        parse_mode="HTML",
    )
    await state.set_state(SetAmount.waiting_for_amount)
    await state.update_data(bot_msg_id=call.message.message_id, chat_id=call.message.chat.id)
    await call.answer()


@router.callback_query(F.data == "set_min_profit")
async def cb_set_min_profit(call: CallbackQuery, state: FSMContext):
    settings = get_settings(call.from_user.id)
    await call.message.edit_text(
        f"📈 <b>Мінімальний прибуток</b>\n\n"
        f"Поточний: <b>{settings.min_profit_uah:,.0f} грн</b>\n\n"
        f"Введіть число — мінімальний чистий прибуток з угоди в гривнях.\n"
        f"Рекомендовано: від <b>50 грн</b>.",
        reply_markup=cancel_input_kb("menu_settings"),
        parse_mode="HTML",
    )
    await state.set_state(SetMinProfit.waiting_for_min_profit)
    await state.update_data(bot_msg_id=call.message.message_id, chat_id=call.message.chat.id)
    await call.answer()


@router.message(SetMinProfit.waiting_for_min_profit)
async def process_min_profit(message: Message, state: FSMContext):
    data = await state.get_data()
    bot_msg_id = data.get("bot_msg_id")
    chat_id = data.get("chat_id")
    try:
        await message.delete()
    except Exception:
        pass
    try:
        val = float(message.text.replace(",", "").replace(" ", ""))
        if val < 0:
            await _edit_or_answer(message, bot_msg_id, chat_id, "❌ Мінімальний профіт не може бути від'ємним")
            return
        settings = get_settings(message.from_user.id)
        settings.min_profit_uah = val
        _save_settings()
        await state.clear()
        await _edit_or_answer(
            message, bot_msg_id, chat_id,
            f"✅ Мін. профіт: <b>{val:,.0f} грн</b>",
            reply_markup=settings_kb(settings),
        )
    except ValueError:
        await _edit_or_answer(message, bot_msg_id, chat_id, "❌ Невірний формат числа")


@router.callback_query(F.data == "set_risk")
async def cb_set_risk(call: CallbackQuery):
    settings = get_settings(call.from_user.id)
    await call.message.edit_text(
        "⚠️ <b>Рівень ризику</b>\n\n"
        "🟢 LOW — тільки найнадійніші угоди\n"
        "🟡 MEDIUM — баланс прибутку і ризику\n"
        "🔴 HIGH — всі можливості (включаючи ризикові)",
        reply_markup=risk_level_kb(settings.risk_level),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data.startswith("risk_"))
async def cb_risk_set(call: CallbackQuery):
    risk = call.data.split("_")[1]
    settings = get_settings(call.from_user.id)
    settings.risk_level = risk
    _save_settings()
    await call.message.edit_text(
        f"✅ Ризик встановлено: <b>{risk}</b>",
        reply_markup=settings_kb(settings),
        parse_mode="HTML",
    )
    await call.answer(f"Ризик: {risk}")


@router.callback_query(F.data == "set_antiscam")
async def cb_set_antiscam(call: CallbackQuery):
    settings = get_settings(call.from_user.id)
    mc = getattr(settings, "min_completion_rate", 90.0)
    await call.message.edit_text(
        "🛡 <b>Анті-скам — мін. % виконання продавця</b>\n\n"
        "Чим <b>нижче</b> — більше продавців у скані, але вищий ризик шахраїв.\n"
        "Чим <b>вище</b> — менше продавців, але надійніші.\n\n"
        "🔸 <b>60%</b> — максимум ордерів, зустрічаються ризикові\n"
        "🔸 <b>70%</b> — хороший баланс для активного ринку\n"
        "🔸 <b>80%</b> — безпечно, більшість шахраїв відфільтровано\n"
        "✅ <b>90%</b> — рекомендовано, найнадійніші продавці",
        reply_markup=antiscam_kb(mc),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data.startswith("antiscam_"))
async def cb_antiscam_set(call: CallbackQuery):
    val = float(call.data.split("_")[1])
    settings = get_settings(call.from_user.id)
    settings.min_completion_rate = val
    _save_settings()
    risk_desc = {60: "⚠️ Ризиковано", 70: "🟡 Помірно", 80: "🟢 Безпечно", 90: "✅ Надійно"}.get(int(val), "")
    await call.message.edit_text(
        f"🛡 Анті-скам встановлено: <b>{val:.0f}%</b>  {risk_desc}\n\n"
        + format_settings(settings),
        reply_markup=settings_kb(settings),
        parse_mode="HTML",
    )
    await call.answer(f"Анті-скам: {val:.0f}%")


@router.callback_query(F.data == "set_bank_fee")
async def cb_set_bank_fee(call: CallbackQuery, state: FSMContext):
    settings = get_settings(call.from_user.id)
    bf = getattr(settings, "bank_fee_uah", 0.0)
    await call.message.edit_text(
        f"🏧 <b>Комісія банку (вручну)</b>\n\n"
        f"Поточна: <b>{bf:,.0f} грн</b>\n\n"
        f"Введи суму комісії в гривнях, яку знімає банк при переказі.\n"
        f"Наприклад: <code>25</code>, <code>50</code>, <code>0</code> (щоб скинути).\n\n"
        f"Ця сума відніматиметься від прибутку в кожній картці арбітражу.",
        reply_markup=cancel_input_kb("menu_settings"),
        parse_mode="HTML",
    )
    await state.set_state(SetBankFee.waiting_for_bank_fee)
    await state.update_data(bot_msg_id=call.message.message_id, chat_id=call.message.chat.id)
    await call.answer()


@router.message(SetBankFee.waiting_for_bank_fee)
async def process_bank_fee(message: Message, state: FSMContext):
    data = await state.get_data()
    bot_msg_id = data.get("bot_msg_id")
    chat_id = data.get("chat_id")
    try:
        await message.delete()
    except Exception:
        pass
    try:
        val = float(message.text.replace(",", ".").replace(" ", ""))
        if val < 0:
            await _edit_or_answer(message, bot_msg_id, chat_id, "❌ Комісія не може бути від'ємною. Введіть 0 або більше.")
            return
        settings = get_settings(message.from_user.id)
        settings.bank_fee_uah = val
        _save_settings()
        await state.clear()
        label = f"{val:,.0f} грн" if val > 0 else "0 грн (вимкнено)"
        await _edit_or_answer(
            message, bot_msg_id, chat_id,
            f"✅ Комісія банку: <b>{label}</b>\n\n" + format_settings(settings),
            reply_markup=settings_kb(settings),
        )
    except ValueError:
        await _edit_or_answer(message, bot_msg_id, chat_id, "❌ Введи число, наприклад: <code>25</code>")


@router.callback_query(F.data == "set_network")
async def cb_set_network(call: CallbackQuery):
    settings = get_settings(call.from_user.id)
    await call.message.edit_text(
        "🌐 <b>Мережа виводу</b>\n\n"
        "Вибери мережу для переказу USDT між біржами:",
        reply_markup=network_kb(settings.network),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data.startswith("network_"))
async def cb_network_set(call: CallbackQuery):
    network = call.data.split("_")[1]
    settings = get_settings(call.from_user.id)
    settings.network = network
    _save_settings()
    await call.message.edit_text(
        f"✅ Мережа: <b>{network}</b>",
        reply_markup=settings_kb(settings),
        parse_mode="HTML",
    )
    await call.answer(f"Мережа: {network}")


@router.callback_query(F.data == "set_banks")
async def cb_set_banks(call: CallbackQuery):
    await call.message.edit_text(
        "🏦 <b>Банк</b>\n\nОбери який банк налаштувати:",
        reply_markup=banks_menu_kb(),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data == "set_buy_banks")
async def cb_set_buy_banks(call: CallbackQuery):
    settings = get_settings(call.from_user.id)
    user_temp_buy_banks[call.from_user.id] = list(settings.buy_banks)
    await call.message.edit_text(
        "🏦 <b>Банк КУПІВЛІ</b>\n\nЦим банком ти платиш продавцю коли купуєш USDT:",
        reply_markup=banks_kb("buy", user_temp_buy_banks[call.from_user.id]),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data == "set_sell_banks")
async def cb_set_sell_banks(call: CallbackQuery):
    settings = get_settings(call.from_user.id)
    user_temp_sell_banks[call.from_user.id] = list(settings.sell_banks)
    await call.message.edit_text(
        "🏦 <b>Банк ПРОДАЖУ</b>\n\nНа цей банк покупець надсилає гроші коли ти продаєш USDT:",
        reply_markup=banks_kb("sell", user_temp_sell_banks[call.from_user.id]),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data.startswith("buy_bank_toggle_"))
async def cb_buy_bank_toggle(call: CallbackQuery):
    bank = "_".join(call.data.split("_")[3:])
    uid = call.from_user.id
    if uid not in user_temp_buy_banks:
        user_temp_buy_banks[uid] = list(get_settings(uid).buy_banks)
    if bank in user_temp_buy_banks[uid]:
        user_temp_buy_banks[uid].remove(bank)
    else:
        user_temp_buy_banks[uid].append(bank)
    await call.message.edit_reply_markup(reply_markup=banks_kb("buy", user_temp_buy_banks[uid]))
    await call.answer()


@router.callback_query(F.data.startswith("sell_bank_toggle_"))
async def cb_sell_bank_toggle(call: CallbackQuery):
    bank = "_".join(call.data.split("_")[3:])
    uid = call.from_user.id
    if uid not in user_temp_sell_banks:
        user_temp_sell_banks[uid] = list(get_settings(uid).sell_banks)
    if bank in user_temp_sell_banks[uid]:
        user_temp_sell_banks[uid].remove(bank)
    else:
        user_temp_sell_banks[uid].append(bank)
    await call.message.edit_reply_markup(reply_markup=banks_kb("sell", user_temp_sell_banks[uid]))
    await call.answer()


@router.callback_query(F.data == "buy_banks_save")
async def cb_buy_banks_save(call: CallbackQuery):
    uid = call.from_user.id
    settings = get_settings(uid)
    settings.buy_banks = user_temp_buy_banks.get(uid, settings.buy_banks)
    _save_settings()
    await call.message.edit_text(
        f"✅ Банк купівлі збережено: <b>{', '.join(settings.buy_banks)}</b>",
        reply_markup=settings_kb(settings),
        parse_mode="HTML",
    )
    await call.answer("Збережено!")


@router.callback_query(F.data == "sell_banks_save")
async def cb_sell_banks_save(call: CallbackQuery):
    uid = call.from_user.id
    settings = get_settings(uid)
    settings.sell_banks = user_temp_sell_banks.get(uid, settings.sell_banks)
    _save_settings()
    await call.message.edit_text(
        f"✅ Банк продажу збережено: <b>{', '.join(settings.sell_banks)}</b>",
        reply_markup=settings_kb(settings),
        parse_mode="HTML",
    )
    await call.answer("Збережено!")


@router.callback_query(F.data == "set_exchanges")
async def cb_set_exchanges(call: CallbackQuery):
    settings = get_settings(call.from_user.id)
    user_temp_exchanges[call.from_user.id] = list(settings.exchanges)
    await call.message.edit_text(
        "📡 <b>Вибери біржі</b>\n\nПозначені біржі будуть скануватись:",
        reply_markup=exchanges_kb(user_temp_exchanges[call.from_user.id]),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data.startswith("ex_toggle_"))
async def cb_ex_toggle(call: CallbackQuery):
    exchange = call.data.split("_")[2]
    uid = call.from_user.id
    if uid not in user_temp_exchanges:
        user_temp_exchanges[uid] = list(get_settings(uid).exchanges)
    if exchange in user_temp_exchanges[uid]:
        if len(user_temp_exchanges[uid]) > 1:
            user_temp_exchanges[uid].remove(exchange)
        else:
            await call.answer("Потрібна хоча б одна біржа!", show_alert=True)
            return
    else:
        user_temp_exchanges[uid].append(exchange)
    await call.message.edit_reply_markup(reply_markup=exchanges_kb(user_temp_exchanges[uid]))
    await call.answer()


@router.callback_query(F.data == "exchanges_save")
async def cb_exchanges_save(call: CallbackQuery):
    uid = call.from_user.id
    settings = get_settings(uid)
    settings.exchanges = user_temp_exchanges.get(uid, settings.exchanges)
    _save_settings()
    await call.message.edit_text(
        f"✅ Біржі збережено: <b>{', '.join(settings.exchanges)}</b>",
        reply_markup=settings_kb(settings),
        parse_mode="HTML",
    )
    await call.answer("Збережено!")


ALL_EXCHANGES = ["Binance", "Bybit", "OKX", "Bitget", "MEXC", "Gate.io", "HTX", "KuCoin"]
ALL_BANKS = ["PrivatBank", "Monobank", "PUMB", "A-Bank", "Oschadbank", "Raiffeisen"]
ALL_ARB_TYPES = ["p2p_same", "cross_exchange", "triangular"]
ARB_TYPE_NAMES = {
    "p2p_same": "P2P › P2P (одна біржа)",
    "cross_exchange": "P2P крос-біржа",
    "triangular": "Triangular (Spot)",
}


@router.callback_query(F.data == "set_arb_types")
async def cb_set_arb_types(call: CallbackQuery):
    settings = get_settings(call.from_user.id)
    user_temp_arb_types[call.from_user.id] = list(getattr(settings, "arb_types", ALL_ARB_TYPES))
    await call.message.edit_text(
        "🔀 <b>Типи арбітражу</b>\n\n"
        "Вибери які типи арбітражу шукати:\n\n"
        "• <b>P2P › P2P</b> — купуєш/продаєш на одній біржі\n"
        "• <b>P2P крос-біржа</b> — купуєш на одній, продаєш на іншій\n"
        "• <b>Triangular</b> — через спот-маркет між двома біржами\n\n"
        "⚠️ Потрібен хоча б один тип.",
        reply_markup=arb_types_kb(user_temp_arb_types[call.from_user.id]),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data.startswith("arb_toggle_"))
async def cb_arb_type_toggle(call: CallbackQuery):
    key = call.data.split("arb_toggle_")[1]
    uid = call.from_user.id
    if uid not in user_temp_arb_types:
        user_temp_arb_types[uid] = list(getattr(get_settings(uid), "arb_types", ALL_ARB_TYPES))
    current = user_temp_arb_types[uid]
    if key in current:
        if len(current) <= 1:
            await call.answer("⚠️ Потрібен хоча б один тип!", show_alert=True)
            return
        current.remove(key)
    else:
        current.append(key)
    await call.message.edit_reply_markup(reply_markup=arb_types_kb(current))
    await call.answer()


@router.callback_query(F.data == "arb_types_select_all")
async def cb_arb_types_select_all(call: CallbackQuery):
    uid = call.from_user.id
    user_temp_arb_types[uid] = list(ALL_ARB_TYPES)
    await call.message.edit_reply_markup(reply_markup=arb_types_kb(user_temp_arb_types[uid]))
    await call.answer()


@router.callback_query(F.data == "arb_types_save")
async def cb_arb_types_save(call: CallbackQuery):
    uid = call.from_user.id
    settings = get_settings(uid)
    saved = user_temp_arb_types.get(uid, ALL_ARB_TYPES)
    settings.arb_types = saved
    _save_settings()
    names = ", ".join(ARB_TYPE_NAMES.get(k, k) for k in saved)
    await call.message.edit_text(
        f"✅ <b>Типи арбітражу збережено:</b>\n{names}",
        reply_markup=settings_kb(settings),
        parse_mode="HTML",
    )
    await call.answer("Збережено!")


@router.callback_query(F.data == "select_all")
async def cb_select_all(call: CallbackQuery):
    """Включити всі біржі, всі банки, всі мережі одним натисканням."""
    uid = call.from_user.id
    settings = get_settings(uid)
    settings.exchanges = list(ALL_EXCHANGES)
    settings.buy_banks = list(ALL_BANKS)
    settings.sell_banks = list(ALL_BANKS)
    settings.network = "ALL"
    settings.risk_level = "HIGH"
    settings.arb_types = list(ALL_ARB_TYPES)
    _save_settings()
    await call.message.edit_text(
        format_settings(settings),
        reply_markup=settings_kb(settings),
        parse_mode="HTML",
    )
    await call.answer("🌟 Увімкнено: всі 8 бірж, 6 банків, всі мережі, всі типи арбітражу!", show_alert=True)


@router.callback_query(F.data == "buy_banks_select_all")
async def cb_buy_banks_select_all(call: CallbackQuery):
    uid = call.from_user.id
    user_temp_buy_banks[uid] = list(ALL_BANKS)
    await call.message.edit_reply_markup(reply_markup=banks_kb("buy", user_temp_buy_banks[uid]))
    await call.answer("✅ Всі банки вибрано")


@router.callback_query(F.data == "sell_banks_select_all")
async def cb_sell_banks_select_all(call: CallbackQuery):
    uid = call.from_user.id
    user_temp_sell_banks[uid] = list(ALL_BANKS)
    await call.message.edit_reply_markup(reply_markup=banks_kb("sell", user_temp_sell_banks[uid]))
    await call.answer("✅ Всі банки вибрано")


@router.callback_query(F.data == "exchanges_select_all")
async def cb_exchanges_select_all(call: CallbackQuery):
    uid = call.from_user.id
    user_temp_exchanges[uid] = list(ALL_EXCHANGES)
    await call.message.edit_reply_markup(reply_markup=exchanges_kb(user_temp_exchanges[uid]))
    await call.answer("✅ Всі біржі вибрано")


@router.callback_query(F.data == "set_payment_source")
async def cb_set_trading_mode(call: CallbackQuery):
    settings = get_settings(call.from_user.id)
    uid = call.from_user.id
    user_temp_trading_mode[uid] = settings.trading_mode
    await call.message.edit_text(
        f"🛡️ <b>Спосіб торгівлі</b>\n\n"
        f"Як ти хочеш торгувати?",
        reply_markup=trading_mode_kb(settings.trading_mode),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data == "tm_select_direct")
async def cb_tm_select_direct(call: CallbackQuery):
    uid = call.from_user.id
    user_temp_trading_mode[uid] = "direct"
    await call.message.edit_reply_markup(
        reply_markup=trading_mode_kb("direct")
    )
    await call.answer()


@router.callback_query(F.data == "tm_select_third")
async def cb_tm_select_third(call: CallbackQuery):
    uid = call.from_user.id
    user_temp_trading_mode[uid] = "third_party"
    await call.message.edit_reply_markup(
        reply_markup=trading_mode_kb("third_party")
    )
    await call.answer()


@router.callback_query(F.data == "tm_save")
async def cb_tm_save(call: CallbackQuery):
    uid = call.from_user.id
    settings = get_settings(uid)
    if uid in user_temp_trading_mode:
        settings.trading_mode = user_temp_trading_mode[uid]
    _save_settings()
    mode_text = "🤝 Напряму (я купую/продаю)" if settings.trading_mode == "direct" else "🛡️ Як 3 особа (гарант)"

    await call.message.edit_text(
        f"✅ <b>Спосіб торгівлі:</b> {mode_text}",
        reply_markup=settings_kb(settings),
        parse_mode="HTML",
    )
    await call.answer("Збережено!")


@router.callback_query(F.data == "set_presets")
async def cb_set_presets(call: CallbackQuery):
    await call.message.edit_text(
        "📋 <b>Рекомендовані профілі</b>\n\n"
        "🟢 <b>Консервативний:</b> Макс сума, мін ризик, більший мін-профіт\n"
        "🟡 <b>Збалансований:</b> Оптимум - популярний вибір (рекомендовано)\n"
        "🔴 <b>Агресивний:</b> Мала сума, більш часто скануєм, вищий ризик\n",
        reply_markup=presets_kb(),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data == "preset_conservative")
async def cb_preset_conservative(call: CallbackQuery):
    uid = call.from_user.id
    settings = get_settings(uid)
    settings.amount_uah = 30000.0
    settings.min_profit_uah = 200.0
    settings.risk_level = "MEDIUM"
    settings.network = "TRC20"
    settings.trading_mode = "direct"
    settings.scan_interval = 10
    _save_settings()
    await call.message.edit_text(
        format_settings(settings),
        reply_markup=settings_kb(settings),
        parse_mode="HTML",
    )
    await call.answer("✅ Консервативний профіль активовано!")


@router.callback_query(F.data == "preset_balanced")
async def cb_preset_balanced(call: CallbackQuery):
    uid = call.from_user.id
    settings = get_settings(uid)
    settings.amount_uah = 20000.0
    settings.min_profit_uah = 50.0
    settings.risk_level = "MEDIUM"
    settings.network = "TRC20"
    settings.trading_mode = "direct"
    settings.scan_interval = 30
    _save_settings()
    await call.message.edit_text(
        format_settings(settings),
        reply_markup=settings_kb(settings),
        parse_mode="HTML",
    )
    await call.answer("✅ Збалансований профіль активовано!")


@router.callback_query(F.data == "preset_aggressive")
async def cb_preset_aggressive(call: CallbackQuery):
    uid = call.from_user.id
    settings = get_settings(uid)
    settings.amount_uah = 10000.0
    settings.min_profit_uah = 25.0
    settings.risk_level = "HIGH"
    settings.network = "BEP20"
    settings.trading_mode = "direct"
    settings.scan_interval = 60
    _save_settings()
    await call.message.edit_text(
        format_settings(settings),
        reply_markup=settings_kb(settings),
        parse_mode="HTML",
    )
    await call.answer("✅ Агресивний профіль активовано!")


@router.callback_query(F.data == "set_interval")
async def cb_set_interval(call: CallbackQuery, state: FSMContext):
    settings = get_settings(call.from_user.id)
    await call.message.edit_text(
        f"⏱ <b>Інтервал Авто-Скану</b>\n\n"
        f"Поточний: <b>{settings.scan_interval} сек</b>\n\n"
        f"Введіть інтервал в секундах (мін: 10, макс: 300).\n"
        f"Рекомендовано: <b>30–60 сек</b>.",
        reply_markup=cancel_input_kb("menu_settings"),
        parse_mode="HTML",
    )
    await state.set_state(SetFilters.waiting_for_interval)
    await state.update_data(bot_msg_id=call.message.message_id, chat_id=call.message.chat.id)
    await call.answer()


@router.message(SetFilters.waiting_for_interval)
async def process_interval(message: Message, state: FSMContext):
    data = await state.get_data()
    bot_msg_id = data.get("bot_msg_id")
    chat_id = data.get("chat_id")
    await _try_delete(message)
    try:
        val = int(message.text.strip())
        if val < 10 or val > 300:
            await _edit_or_answer(message, bot_msg_id, chat_id, "❌ Інтервал від 10 до 300 секунд")
            return
        settings = get_settings(message.from_user.id)
        settings.scan_interval = val
        _save_settings()
        await state.clear()
        await _edit_or_answer(
            message, bot_msg_id, chat_id,
            f"✅ Інтервал авто-скану: <b>{val} сек</b>",
            reply_markup=settings_kb(settings),
        )
    except ValueError:
        await _edit_or_answer(message, bot_msg_id, chat_id, "❌ Введіть ціле число")


@router.callback_query(F.data == "menu_favorites")
async def cb_menu_favorites(call: CallbackQuery):
    favs = get_favorites()
    text = format_favorites(favs)
    await call.message.edit_text(text, reply_markup=main_menu_kb(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "menu_analytics")
async def cb_menu_analytics(call: CallbackQuery):
    stats = get_stats()
    text = format_analytics(stats)
    await call.message.edit_text(text, reply_markup=main_menu_kb(), parse_mode="HTML")
    await call.answer()


# ─── PARTICIPANTS ────────────────────────────────────────────────

def _participants_text(participants: list[dict]) -> str:
    count = len(participants)
    if not participants:
        return (
            "👥 <b>Учасники</b>\n\n"
            "Список порожній.\n\n"
            "Додайте учасників — вони також отримуватимуть "
            "сповіщення Live Mode коли ви запустите сканування."
        )
    lines = [f"👥 <b>Учасники ({count})</b>\n"]
    for i, p in enumerate(participants, 1):
        added = p.get("added_at", "")[:10]
        lines.append(f"{i}. 👤 <b>{p['name']}</b>  <code>{p['user_id']}</code>  ({added})")
    lines.append("\nВони отримують всі Live Mode сповіщення.")
    return "\n".join(lines)


@router.callback_query(F.data == "menu_participants")
async def cb_menu_participants(call: CallbackQuery):
    parts = get_participants(call.from_user.id)
    await call.message.edit_text(
        _participants_text(parts),
        reply_markup=participants_kb(parts),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data == "part_add")
async def cb_part_add(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text(
        "➕ <b>Додати учасника</b>\n\n"
        "Попросіть учасника надіслати вам своє <b>Telegram ID</b>.\n\n"
        "Отримати ID можна через бота <a href='https://t.me/userinfobot'>@userinfobot</a> "
        "або <a href='https://t.me/getmyid_bot'>@getmyid_bot</a>.\n\n"
        "Введіть числовий Telegram ID учасника:",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await state.set_state(AddParticipant.waiting_for_user_id)
    await state.update_data(bot_msg_id=call.message.message_id, chat_id=call.message.chat.id)
    await call.answer()


@router.message(AddParticipant.waiting_for_user_id)
async def process_add_participant(message: Message, state: FSMContext):
    data = await state.get_data()
    bot_msg_id = data.get("bot_msg_id")
    chat_id = data.get("chat_id")
    await _try_delete(message)
    text = message.text.strip() if message.text else ""
    try:
        participant_id = int(text)
    except ValueError:
        await _edit_or_answer(
            message, bot_msg_id, chat_id,
            "❌ Невірний формат. Введіть числовий Telegram ID, наприклад:\n<code>123456789</code>",
        )
        return

    if participant_id == message.from_user.id:
        await _edit_or_answer(message, bot_msg_id, chat_id, "❌ Не можна додати себе як учасника.")
        return

    name = f"User {participant_id}"
    added = add_participant(message.from_user.id, participant_id, name)
    await state.clear()
    parts = get_participants(message.from_user.id)

    if added:
        await _edit_or_answer(
            message, bot_msg_id, chat_id,
            f"✅ <b>Учасника додано!</b>\n\n"
            f"👤 ID: <code>{participant_id}</code>\n\n"
            f"Тепер він отримуватиме Live Mode сповіщення.\n"
            f"Загалом учасників: <b>{len(parts)}</b>",
            reply_markup=participants_kb(parts),
        )
    else:
        await _edit_or_answer(
            message, bot_msg_id, chat_id,
            f"⚠️ Учасник <code>{participant_id}</code> вже є у списку.",
            reply_markup=participants_kb(parts),
        )


@router.callback_query(F.data.startswith("part_remove_"))
async def cb_part_remove(call: CallbackQuery):
    participant_id = int(call.data.split("_")[-1])
    removed = remove_participant(call.from_user.id, participant_id)
    parts = get_participants(call.from_user.id)
    if removed:
        await call.answer(f"❌ Учасника {participant_id} видалено", show_alert=True)
    else:
        await call.answer("Учасника не знайдено", show_alert=True)
    await call.message.edit_text(
        _participants_text(parts),
        reply_markup=participants_kb(parts),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("part_info_"))
async def cb_part_info(call: CallbackQuery):
    participant_id = int(call.data.split("_")[-1])
    parts = get_participants(call.from_user.id)
    p = next((x for x in parts if x["user_id"] == participant_id), None)
    if p:
        await call.answer(
            f"👤 {p['name']}\nID: {p['user_id']}\nДодано: {p.get('added_at', '')[:10]}",
            show_alert=True,
        )
    else:
        await call.answer("Учасника не знайдено", show_alert=True)
