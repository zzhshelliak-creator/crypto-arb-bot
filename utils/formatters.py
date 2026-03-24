import time
from models.types import ArbitrageOpportunity, ArbitrageType, RiskLevel, SpeedType

BANK_COMMISSIONS = {
    "PrivatBank": 0.0,
    "Monobank": 0.0,
    "PUMB": 0.0,
    "A-Bank": 0.0,
    "Oschadbank": 0.0,
    "Raiffeisen": 1.5,
}


def _build_steps(opp: ArbitrageOpportunity, trading_mode: str, net_profit: float, net_profit_pct: float) -> str:
    payment = opp.payment_method
    buy_ex = opp.buy_exchange
    sell_ex = opp.sell_exchange
    is_cross = buy_ex != sell_ex
    amount_uah_approx = opp.amount_usdt * opp.buy_price

    seller_nick = opp.buy_order.nickname if opp.buy_order else "продавець"

    if is_cross:
        return "\n".join([
            f"1. Надіслати {amount_uah_approx:,.0f} UAH {payment} → {seller_nick}",
            f"2. Отримати {opp.amount_usdt:.0f} USDT @ {opp.buy_price:.2f}",
            f"3. Переказ {opp.network} → {sell_ex}",
            f"4. Продати @ {opp.sell_price:.2f} UAH",
            f"5. +{net_profit:.0f} UAH (+{net_profit_pct:.2f}%) ✅",
        ])
    else:
        sell_nick = opp.sell_order.nickname if opp.sell_order else "покупець"
        return "\n".join([
            f"1. Надіслати {amount_uah_approx:,.0f} UAH {payment} → {seller_nick}",
            f"2. Отримати {opp.amount_usdt:.0f} USDT @ {opp.buy_price:.2f}",
            f"3. Виставити на продаж → {sell_nick} @ {opp.sell_price:.2f}",
            f"4. Отримати {opp.amount_usdt * opp.sell_price:,.0f} UAH",
            f"5. +{net_profit:.0f} UAH (+{net_profit_pct:.2f}%) ✅",
        ])


def format_opportunity(opp: ArbitrageOpportunity, index: int = 1, trading_mode: str = "direct", extra_bank_fee_uah: float = 0.0) -> str:
    risk_emoji = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}.get(
        opp.risk.value if hasattr(opp.risk, "value") else str(opp.risk), "⚪"
    )
    risk_str = opp.risk.value if hasattr(opp.risk, "value") else str(opp.risk)
    arb_type_str = opp.arb_type.value if hasattr(opp.arb_type, "value") else str(opp.arb_type)

    buy_ex = opp.buy_exchange
    sell_ex = opp.sell_exchange
    is_cross = buy_ex != sell_ex
    exchange_line = f"Біржа: {buy_ex} → {sell_ex}" if is_cross else f"Біржа: {buy_ex}"

    # Gross profit (before any fees)
    gross_profit = opp.spread * opp.amount_usdt

    # Bank fee based on payment method commission + manual extra fee
    payment = opp.payment_method
    bank_commission_pct = BANK_COMMISSIONS.get(payment, 0.0)
    amount_uah_approx = opp.amount_usdt * opp.buy_price
    bank_fee_uah = amount_uah_approx * bank_commission_pct / 100 + extra_bank_fee_uah

    # Network (withdrawal) fee
    net_fee_uah = opp.fees_breakdown.get("withdrawal_fee_uah", 0.0)
    network = opp.network or opp.fees_breakdown.get("network", "TRC20")

    # Total fees and net profit
    total_fees_uah = bank_fee_uah + net_fee_uah
    net_profit = opp.profit_uah - bank_fee_uah
    net_profit_pct = (net_profit / amount_uah_approx * 100) if amount_uah_approx > 0 else 0.0

    # Seller info
    seller = opp.buy_order
    online_str = "Зараз онлайн" if (seller and seller.is_online) else "Офлайн"
    if seller and seller.avg_release_time:
        mins = seller.avg_release_time // 60
        release_str = f"прибл.{mins} хв" if mins > 0 else "< 1 хв"
    else:
        release_str = "—"
    completion = opp.seller_completion_rate
    total_orders_count = opp.seller_total_orders

    # Trading mode label
    mode_text = "3-я особа" if trading_mode == "third_party" else "Напряму"

    # Score bar (10 characters wide)
    filled = min(10, int(opp.score / 10))
    score_bar = "█" * filled + "░" * (10 - filled)

    # Fees section lines
    if bank_fee_uah > 0:
        pct_part = f" ({bank_commission_pct:.1f}%)" if bank_commission_pct > 0 else ""
        manual_part = f" +{extra_bank_fee_uah:.0f} грн вруч." if extra_bank_fee_uah > 0 else ""
        bank_line = f"├ Банк ({payment}): -{bank_fee_uah:.0f} UAH{pct_part}{manual_part}"
    else:
        bank_line = f"├ Банк ({payment}): 0 UAH (0%)"

    if net_fee_uah > 0:
        net_line = f"├ Мережа ({network}): -{net_fee_uah:.0f} UAH"
    else:
        net_line = f"├ Мережа ({network}): 0 UAH"

    steps = _build_steps(opp, trading_mode, net_profit, net_profit_pct)

    # Verification status line
    if opp.verified and opp.verified_at > 0:
        age_sec = int(time.time() - opp.verified_at)
        if age_sec < 5:
            age_str = "щойно"
        elif age_sec < 60:
            age_str = f"{age_sec} сек тому"
        else:
            age_str = f"{age_sec // 60} хв тому"
        verify_line = f"✅ <b>Ціни підтверджено</b> ({age_str}) — реальний ринок"
        price_check = ""
        if opp.verified_buy_price and abs(opp.verified_buy_price - opp.buy_price) > 0.001:
            price_check = f" (перевірено: {opp.verified_buy_price:.2f}/{opp.verified_sell_price:.2f})"
        verify_line += price_check
    else:
        verify_line = "⚠️ Ціни отримані з першого скану — можуть змінитись"

    msg = (
        f"🔥 <b>АРБІТРАЖ #{index}</b>\n"
        f"Тип: {arb_type_str}\n"
        f"{exchange_line}\n"
        f"{verify_line}\n\n"

        f"💰 <b>Ціни:</b>\n"
        f"├ Купівля: {opp.buy_price:.2f} UAH\n"
        f"└ Продаж: {opp.sell_price:.2f} UAH\n\n"

        f"📊 <b>Результат:</b>\n"
        f"├ Спред: {opp.spread:.2f} UAH ({opp.spread_pct:.2f}%)\n"
        f"├ Валовий профіт: +{gross_profit:.0f} UAH\n"
        f"├ ROI (до комісій): +{opp.spread_pct:.2f}%\n"
        f"└ Обсяг: {opp.amount_usdt:.0f} USDT\n\n"

        f"💸 <b>Комісії:</b>\n"
        f"{bank_line}\n"
        f"{net_line}\n"
        f"├ Разом витрат: -{total_fees_uah:.0f} UAH\n"
        f"└ Чистий профіт: +{net_profit:.0f} UAH ({net_profit_pct:.2f}%)\n\n"

        f"⚡ <b>Продавець:</b>\n"
        f"├ Тип: {mode_text} ✅\n"
        f"├ Рейтинг: {completion:.1f}% ✅\n"
        f"├ Ордерів: {total_orders_count} ✅\n"
        f"├ Онлайн: {online_str} ✅\n"
        f"├ Відповідь: {release_str} ✅\n"
        f"└ Ризик: {risk_emoji} {risk_str}\n\n"

        f"🏦 Платіж: {payment}\n"
        f"👥 Учасник: Я\n\n"

        f"📋 <b>Кроки:</b>\n"
        f"{steps}\n\n"

        f"🏆 Оцінка: {opp.score:.0f}/100 [{score_bar}]"
    )
    return msg


def format_opportunities_list(opps: list[ArbitrageOpportunity]) -> str:
    if not opps:
        return "😔 Наразі нема привабливих можливостей. Спробуйте пізніше або змініть налаштування."

    lines = [f"🔍 <b>Знайдено {len(opps)} можливостей:</b>\n"]
    for i, opp in enumerate(opps, 1):
        risk_e = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}.get(
            opp.risk.value if hasattr(opp.risk, "value") else str(opp.risk), "⚪"
        )
        arb_type = opp.arb_type.value if hasattr(opp.arb_type, "value") else str(opp.arb_type)
        ex_info = (
            opp.buy_exchange if opp.buy_exchange == opp.sell_exchange
            else f"{opp.buy_exchange}→{opp.sell_exchange}"
        )
        lines.append(
            f"{i}. {risk_e} <b>{arb_type}</b> | {ex_info}\n"
            f"   💰 +{opp.profit_uah:.0f} грн ({opp.spread_pct:.2f}%) | ⭐ {opp.score:.0f}/100"
        )
    return "\n".join(lines)


def format_analytics(stats: dict) -> str:
    avg = stats.get("avg_profit", 0)
    best = stats.get("best_profit", 0)
    best_ex = stats.get("best_exchange") or "—"
    best_type = stats.get("best_type") or "—"
    scans = stats.get("scans", 0)
    total = stats.get("total_opportunities", 0)
    last = stats.get("last_scan") or "—"

    return (
        f"📊 <b>Аналітика</b>\n\n"
        f"🔍 Сканувань: <b>{scans}</b>\n"
        f"💡 Знайдено можливостей: <b>{total}</b>\n"
        f"💰 Сер. профіт: <b>{avg:.0f} грн</b>\n"
        f"🏆 Кращий профіт: <b>{best:.0f} грн</b>\n"
        f"🏅 Кращa біржа: <b>{best_ex}</b>\n"
        f"📈 Кращий тип: <b>{best_type}</b>\n"
        f"🕐 Останнє сканування: {last[:19] if last != '—' else '—'}"
    )


def format_settings(settings) -> str:
    banks = ", ".join(settings.banks) if settings.banks else "Всі"
    exchanges = ", ".join(settings.exchanges) if settings.exchanges else "Всі"
    mode_text = "🤝 Напряму (я купую/продаю)" if settings.trading_mode == "direct" else "🛡️ Як 3 особа (гарант)"
    network_text = "🌟 Всі мережі (автовибір)" if settings.network == "ALL" else settings.network
    mc = getattr(settings, "min_completion_rate", 90.0)
    bf = getattr(settings, "bank_fee_uah", 0.0)
    bank_fee_text = f"{bf:,.0f} грн" if bf > 0 else "0 грн"
    return (
        f"⚙️ <b>Поточні налаштування</b>\n\n"
        f"💵 Сума: <b>{settings.amount_uah:,.0f} UAH</b>\n"
        f"💰 Мін. профіт: <b>{settings.min_profit_uah:,.0f} UAH</b>\n"
        f"⚠️ Ризик: <b>{settings.risk_level}</b>\n"
        f"🛡 Анті-скам: <b>{mc:.0f}%</b>\n"
        f"🏧 Комісія банку: <b>{bank_fee_text}</b>\n"
        f"🏦 Банки: <b>{banks}</b>\n"
        f"🌐 Мережа: <b>{network_text}</b>\n"
        f"🛡️ Тип: <b>{mode_text}</b>\n"
        f"🔄 Інтервал: <b>{settings.scan_interval} сек</b>\n"
        f"📡 Біржі: <b>{exchanges}</b>"
    )


def format_favorites(favorites: list) -> str:
    if not favorites:
        return "⭐ Збережених можливостей немає."
    lines = ["⭐ <b>Обрані можливості:</b>\n"]
    for i, f in enumerate(favorites[:10], 1):
        risk_e = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}.get(f.get("risk", ""), "⚪")
        lines.append(
            f"{i}. {risk_e} {f.get('type', '?')} | {f.get('buy_exchange', '?')}→{f.get('sell_exchange', '?')}\n"
            f"   💰 +{f.get('profit_uah', 0):.0f} грн | {f.get('saved_at', '')[:16]}"
        )
    return "\n".join(lines)
