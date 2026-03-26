from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


# ─────────────────────────────────────────────────────────────
#  ГОЛОВНЕ МЕНЮ
# ─────────────────────────────────────────────────────────────

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Сканувати зараз", callback_data="scan_start")],
        [InlineKeyboardButton(text="⚡ Авто-Скан 24/7", callback_data="menu_live")],
        [
            InlineKeyboardButton(text="⚙️ Налаштування", callback_data="menu_settings"),
            InlineKeyboardButton(text="📊 Статистика", callback_data="menu_analytics"),
        ],
        [
            InlineKeyboardButton(text="⭐ Обране", callback_data="menu_favorites"),
            InlineKeyboardButton(text="👥 Учасники", callback_data="menu_participants"),
        ],
    ])


def main_text(settings=None) -> str:
    if settings is None:
        return "📌 <b>Головне меню</b>\n\nВибери дію:"
    buy_b = ", ".join(settings.buy_banks[:2]) + ("…" if len(settings.buy_banks) > 2 else "")
    sell_b = ", ".join(settings.sell_banks[:2]) + ("…" if len(settings.sell_banks) > 2 else "")
    exchanges = ", ".join(settings.exchanges[:4]) + ("…" if len(settings.exchanges) > 4 else "")
    mode = "Напряму" if settings.trading_mode == "direct" else "Як 3 особа"
    return (
        "📌 <b>Головне меню</b>\n\n"
        f"💰 <b>{settings.amount_uah:,.0f} грн</b>  ·  📈 мін. <b>{settings.min_profit_uah:,.0f} грн</b>  ·  ⚠️ {settings.risk_level}\n"
        f"🏦 Купівля: {buy_b}  ·  Продаж: {sell_b}\n"
        f"📡 {exchanges}  ·  🌐 {settings.network}  ·  🤝 {mode}\n\n"
        "Натисни 🔍 <b>Сканувати зараз</b> для пошуку арбітражу."
    )


# ─────────────────────────────────────────────────────────────
#  СКАНУВАННЯ
# ─────────────────────────────────────────────────────────────

def retry_kb() -> InlineKeyboardMarkup:
    """Клавіатура після невдалого сканування."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Спробувати знову", callback_data="scan_start")],
        [
            InlineKeyboardButton(text="⚙️ Налаштування", callback_data="menu_settings"),
            InlineKeyboardButton(text="🏠 Головне меню", callback_data="back_main"),
        ],
    ])


# ─────────────────────────────────────────────────────────────
#  АВТО-СКАН
# ─────────────────────────────────────────────────────────────

def live_kb(is_running: bool) -> InlineKeyboardMarkup:
    buttons = []
    if is_running:
        buttons.append([
            InlineKeyboardButton(text="⏸ Зупинити", callback_data="live_stop"),
            InlineKeyboardButton(text="📋 Результати", callback_data="opp_list"),
        ])
    else:
        buttons.append([InlineKeyboardButton(text="▶️ Запустити Авто-Скан", callback_data="live_start")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def autoscan_status_kb(scan_count: int) -> InlineKeyboardMarkup:
    results_text = f"📋 Результати ({scan_count} скан.)" if scan_count else "📋 Результати"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⏸ Зупинити", callback_data="live_stop"),
            InlineKeyboardButton(text=results_text, callback_data="opp_list"),
        ],
        [InlineKeyboardButton(text="🏠 Головне меню", callback_data="back_main")],
    ])


# ─────────────────────────────────────────────────────────────
#  РЕЗУЛЬТАТИ АРБІТРАЖУ
# ─────────────────────────────────────────────────────────────

def opportunities_list_kb(opportunities, autoscan_running: bool = False) -> InlineKeyboardMarkup:
    buttons = []

    if opportunities and hasattr(opportunities[0], "profit_uah"):
        for i, opp in enumerate(opportunities):
            buy_ex = opp.buy_exchange[:3].upper()
            sell_ex = opp.sell_exchange[:3].upper()
            profit = f"+{opp.profit_uah:,.0f} грн"
            if opp.buy_exchange == opp.sell_exchange:
                label = f"#{i + 1}  {buy_ex}  {profit}"
            else:
                label = f"#{i + 1}  {buy_ex} › {sell_ex}  {profit}"
            buttons.append([InlineKeyboardButton(text=label, callback_data=f"opp_detail_{i}")])
    else:
        total = opportunities if isinstance(opportunities, int) else len(opportunities)
        for i in range(total):
            buttons.append([InlineKeyboardButton(text=f"#{i + 1} Деталі", callback_data=f"opp_detail_{i}")])

    if autoscan_running:
        buttons.append([
            InlineKeyboardButton(text="🔄 Оновити", callback_data="scan_start"),
            InlineKeyboardButton(text="⏸ Стоп", callback_data="live_stop"),
        ])
    else:
        buttons.append([
            InlineKeyboardButton(text="🔄 Ресканувати", callback_data="scan_start"),
            InlineKeyboardButton(text="⚡ Авто-Скан", callback_data="menu_live"),
        ])
    buttons.append([InlineKeyboardButton(text="🏠 Головне меню", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def opportunity_kb(index: int, total: int) -> InlineKeyboardMarkup:
    buttons = []
    nav = []
    if index > 0:
        nav.append(InlineKeyboardButton(text="◀️ Попередня", callback_data=f"opp_prev_{index}"))
    if index < total - 1:
        nav.append(InlineKeyboardButton(text="Наступна ▶️", callback_data=f"opp_next_{index}"))
    if nav:
        buttons.append(nav)
    buttons.append([
        InlineKeyboardButton(text="⭐ Зберегти", callback_data=f"opp_save_{index}"),
        InlineKeyboardButton(text="📋 До списку", callback_data="opp_list"),
    ])
    buttons.append([InlineKeyboardButton(text="🏠 Головне меню", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ─────────────────────────────────────────────────────────────
#  СУМА
# ─────────────────────────────────────────────────────────────

def amount_kb(current: float) -> InlineKeyboardMarkup:
    presets = [1_000, 5_000, 10_000, 20_000, 50_000, 100_000, 200_000, 500_000]
    rows = []
    row = []
    for p in presets:
        mark = "✅ " if current == p else ""
        row.append(InlineKeyboardButton(text=f"{mark}{p:,}", callback_data=f"amount_set_{p}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton(text="✏️ Своя сума", callback_data="amount_custom"),
        InlineKeyboardButton(text="🔙 Назад", callback_data="menu_settings"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ─────────────────────────────────────────────────────────────
#  НАЛАШТУВАННЯ
# ─────────────────────────────────────────────────────────────

def antiscam_kb(current: float = 90.0) -> InlineKeyboardMarkup:
    def mark(v): return "✅ " if current == v else ""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"{mark(60)}60% — Більше продавців", callback_data="antiscam_60"),
            InlineKeyboardButton(text=f"{mark(70)}70%", callback_data="antiscam_70"),
        ],
        [
            InlineKeyboardButton(text=f"{mark(80)}80%", callback_data="antiscam_80"),
            InlineKeyboardButton(text=f"{mark(90)}90% — Рекоменд.", callback_data="antiscam_90"),
        ],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_settings")],
    ])


def settings_kb(settings=None) -> InlineKeyboardMarkup:
    if settings:
        amount_label = f"💰 {settings.amount_uah:,.0f} грн"
        profit_label = f"📈 Мін: {settings.min_profit_uah:,.0f} грн"
        risk_icons = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}
        risk_icon = risk_icons.get(settings.risk_level, "⚠️")
        risk_label = f"{risk_icon} {settings.risk_level}"
        net = settings.network
        network_label = f"🌐 {'Всі мережі' if net == 'ALL' else net}"
        mode_name = "Напряму" if settings.trading_mode == "direct" else "Як 3 особа"
        mode_label = f"🤝 {mode_name}"
        mc = getattr(settings, "min_completion_rate", 90)
        antiscam_label = f"🛡 Анті-скам: {mc:.0f}%"
        bf = getattr(settings, "bank_fee_uah", 0.0)
        bankfee_label = f"🏧 Комісія банку: {bf:,.0f} грн" if bf > 0 else "🏧 Комісія банку: 0 грн"
    else:
        amount_label = "💰 Сума"
        profit_label = "📈 Мін профіт"
        risk_label = "⚠️ Ризик"
        network_label = "🌐 Мережа"
        mode_label = "🤝 Режим"
        antiscam_label = "🛡 Анті-скам"
        bankfee_label = "🏧 Комісія банку"

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=amount_label, callback_data="set_amount"),
            InlineKeyboardButton(text=profit_label, callback_data="set_min_profit"),
        ],
        [
            InlineKeyboardButton(text=risk_label, callback_data="set_risk"),
            InlineKeyboardButton(text=network_label, callback_data="set_network"),
        ],
        [
            InlineKeyboardButton(text="🏦 Банк", callback_data="set_banks"),
        ],
        [
            InlineKeyboardButton(text="📡 Біржі", callback_data="set_exchanges"),
        ],
        [
            InlineKeyboardButton(text=mode_label, callback_data="set_payment_source"),
            InlineKeyboardButton(text=antiscam_label, callback_data="set_antiscam"),
        ],
        [
            InlineKeyboardButton(text=bankfee_label, callback_data="set_bank_fee"),
            InlineKeyboardButton(text="⏱ Авто-інтервал", callback_data="set_interval"),
        ],
        [
            InlineKeyboardButton(text="🔀 Типи арбітражу", callback_data="set_arb_types"),
            InlineKeyboardButton(text="🌟 Включити все", callback_data="select_all"),
        ],
        [InlineKeyboardButton(text="📋 Швидкі пресети", callback_data="set_presets")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")],
    ])


def banks_menu_kb() -> InlineKeyboardMarkup:
    """Підменю вибору типу банку — купівля або продаж."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏦 Банк купівлі", callback_data="set_buy_banks")],
        [InlineKeyboardButton(text="🏦 Банк продажу", callback_data="set_sell_banks")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_settings")],
    ])


def risk_level_kb(current: str = "") -> InlineKeyboardMarkup:
    def mark(r): return "✅ " if r == current else ""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"{mark('LOW')}🟢 LOW", callback_data="risk_LOW"),
            InlineKeyboardButton(text=f"{mark('MEDIUM')}🟡 MEDIUM", callback_data="risk_MEDIUM"),
            InlineKeyboardButton(text=f"{mark('HIGH')}🔴 HIGH", callback_data="risk_HIGH"),
        ],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_settings")],
    ])


def network_kb(current: str = "") -> InlineKeyboardMarkup:
    def mark(n): return "✅ " if n == current else ""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{mark('ALL')}🌟 Всі мережі (автовибір найдешевшої)",
            callback_data="network_ALL",
        )],
        [
            InlineKeyboardButton(text=f"{mark('TRC20')}TRC20 (Tron, ~$1)", callback_data="network_TRC20"),
            InlineKeyboardButton(text=f"{mark('BEP20')}BEP20 (BSC, ~$0.5)", callback_data="network_BEP20"),
        ],
        [
            InlineKeyboardButton(text=f"{mark('SOL')}SOL (Solana, ~$0.1)", callback_data="network_SOL"),
            InlineKeyboardButton(text=f"{mark('APT')}APT (Aptos, ~$0.5)", callback_data="network_APT"),
        ],
        [
            InlineKeyboardButton(text=f"{mark('ERC20')}ERC20 (ETH, ~$5)", callback_data="network_ERC20"),
        ],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_settings")],
    ])


def arb_types_kb(selected: list[str]) -> InlineKeyboardMarkup:
    ALL_TYPES = [
        ("p2p_same",       "P2P › P2P (одна біржа)"),
        ("cross_exchange",  "P2P крос-біржа"),
        ("triangular",      "Triangular (Spot)"),
    ]
    buttons = []
    for key, label in ALL_TYPES:
        check = "✅" if key in selected else "☐"
        buttons.append([InlineKeyboardButton(
            text=f"{check} {label}", callback_data=f"arb_toggle_{key}"
        )])
    all_selected = all(k in selected for k, _ in ALL_TYPES)
    all_label = "✅ Всі типи вибрано" if all_selected else "🌟 Вибрати всі типи"
    buttons.append([InlineKeyboardButton(text=all_label, callback_data="arb_types_select_all")])
    buttons.append([
        InlineKeyboardButton(text="✔️ Зберегти", callback_data="arb_types_save"),
        InlineKeyboardButton(text="🔙 Назад", callback_data="menu_settings"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def banks_kb(side: str, selected: list[str]) -> InlineKeyboardMarkup:
    """
    side: "buy" або "sell"
    Callback-prefixes: {side}_bank_toggle_*, {side}_banks_select_all, {side}_banks_save
    """
    all_banks = ["PrivatBank", "Monobank", "PUMB", "A-Bank", "Oschadbank", "Raiffeisen"]
    all_selected = set(selected) >= set(all_banks)
    buttons = []
    row = []
    for bank in all_banks:
        check = "✅" if bank in selected else "☐"
        row.append(InlineKeyboardButton(text=f"{check} {bank}", callback_data=f"{side}_bank_toggle_{bank}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    all_label = "✅ Всі банки вибрано" if all_selected else "🌟 Вибрати всі банки"
    buttons.append([InlineKeyboardButton(text=all_label, callback_data=f"{side}_banks_select_all")])
    buttons.append([
        InlineKeyboardButton(text="✔️ Зберегти", callback_data=f"{side}_banks_save"),
        InlineKeyboardButton(text="🔙 Назад", callback_data="set_banks"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def exchanges_kb(selected: list[str]) -> InlineKeyboardMarkup:
    all_exchanges = ["Binance", "Bybit", "OKX", "Bitget", "MEXC", "Gate.io", "HTX", "KuCoin"]
    all_selected = len(selected) >= len(all_exchanges)
    buttons = []
    row = []
    for ex in all_exchanges:
        check = "✅" if ex in selected else "☐"
        row.append(InlineKeyboardButton(text=f"{check} {ex}", callback_data=f"ex_toggle_{ex}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    all_label = "✅ Всі біржі вибрано" if all_selected else "🌟 Вибрати всі біржі"
    buttons.append([InlineKeyboardButton(text=all_label, callback_data="exchanges_select_all")])
    buttons.append([
        InlineKeyboardButton(text="✔️ Зберегти", callback_data="exchanges_save"),
        InlineKeyboardButton(text="🔙 Назад", callback_data="menu_settings"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def trading_mode_kb(mode: str) -> InlineKeyboardMarkup:
    direct_check = "✅" if mode == "direct" else "☐"
    third_check = "✅" if mode == "third_party" else "☐"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{direct_check} 🤝 Напряму (я купую/продаю)", callback_data="tm_set_direct")],
        [InlineKeyboardButton(text=f"{third_check} 🛡️ Як 3 особа (гарант)", callback_data="tm_set_third")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_settings")],
    ])


def presets_kb(active: str = "") -> InlineKeyboardMarkup:
    def mark(p): return "✅ " if p == active else ""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{mark('conservative')}🟢 Conservative — безпечно", callback_data="preset_conservative")],
        [InlineKeyboardButton(text=f"{mark('balanced')}🟡 Balanced — рекомендовано", callback_data="preset_balanced")],
        [InlineKeyboardButton(text=f"{mark('aggressive')}🔴 Aggressive — швидко", callback_data="preset_aggressive")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_settings")],
    ])


# ─────────────────────────────────────────────────────────────
#  УЧАСНИКИ
# ─────────────────────────────────────────────────────────────

def participants_kb(participants: list[dict]) -> InlineKeyboardMarkup:
    buttons = []
    for p in participants:
        uid = p["user_id"]
        name = p.get("name", str(uid))
        buttons.append([
            InlineKeyboardButton(text=f"👤 {name}", callback_data=f"part_info_{uid}"),
            InlineKeyboardButton(text="❌ Видалити", callback_data=f"part_remove_{uid}"),
        ])
    buttons.append([InlineKeyboardButton(text="➕ Додати учасника", callback_data="part_add")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ─────────────────────────────────────────────────────────────
#  ДОПОМІЖНІ КЛАВІАТУРИ ДЛЯ ВВЕДЕННЯ ТЕКСТУ
# ─────────────────────────────────────────────────────────────

def cancel_input_kb(back_callback: str = "menu_settings") -> InlineKeyboardMarkup:
    """Кнопка «Назад» для екранів де очікується введення тексту."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data=back_callback)]
    ])


# ─────────────────────────────────────────────────────────────
#  ЗВОРОТНА СУМІСНІСТЬ
# ─────────────────────────────────────────────────────────────

def scan_kb(trading_mode: str = "direct") -> InlineKeyboardMarkup:
    """Залишено для зворотної сумісності."""
    return retry_kb()
