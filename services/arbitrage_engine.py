import asyncio
import logging
import statistics
import time
from models.types import (
    P2POrder, ArbitrageOpportunity, ArbitrageType, RiskLevel, SpeedType,
    UserSettings
)
from services.exchange_api import ExchangeAPI, WITHDRAWAL_FEES_USDT, SPOT_TRADING_FEE

logger = logging.getLogger(__name__)

# --- Anti-scam thresholds ---
# completion_rate  : 95% is the industry standard for trusted P2P traders
# min_monthly_orders: 30 monthly orders (APIs return monthly stats for most exchanges)
# max_price_deviation: price must be within 3% of the market average to avoid scam prices
ANTI_SCAM = {
    "min_completion_rate": 90.0,
    "min_monthly_orders": 15,
    "max_price_deviation_pct": 5.0,
    "max_release_time_sec": 1800,     # 30 minutes max release time
    "require_online": True,            # seller must be currently online
}


def classify_speed(avg_release_time: int, is_online: bool) -> SpeedType:
    if not is_online:
        return SpeedType.SLOW
    if avg_release_time <= 180:
        return SpeedType.FAST
    elif avg_release_time <= 600:
        return SpeedType.MEDIUM
    return SpeedType.SLOW


def check_anti_scam(
    order: P2POrder,
    market_prices: list[float],
    side: str,
    min_completion: float | None = None,
) -> tuple[bool, str]:
    """
    Returns (passed, reason).  reason is non-empty when the check fails.
    min_completion overrides the global ANTI_SCAM threshold when provided.
    """
    threshold = min_completion if min_completion is not None else ANTI_SCAM["min_completion_rate"]
    if order.completion_rate < threshold:
        return False, f"completion {order.completion_rate:.0f}% < {threshold:.0f}%"

    if order.total_orders < ANTI_SCAM["min_monthly_orders"]:
        return False, f"orders {order.total_orders} < {ANTI_SCAM['min_monthly_orders']}"

    if ANTI_SCAM["require_online"] and not order.is_online:
        return False, "seller offline"

    if order.avg_release_time > ANTI_SCAM["max_release_time_sec"]:
        return False, f"release time {order.avg_release_time // 60} min > 15 min"

    if market_prices:
        avg = statistics.mean(market_prices)
        if avg == 0:
            return True, ""
        deviation = abs(order.price - avg) / avg * 100

        # A suspiciously cheap BUY price is a scam indicator
        if side == "BUY" and order.price < avg * (1 - ANTI_SCAM["max_price_deviation_pct"] / 100):
            return False, f"buy price {order.price:.2f} too low vs avg {avg:.2f}"

        # A suspiciously high SELL price is also a scam
        if side == "SELL" and order.price > avg * (1 + ANTI_SCAM["max_price_deviation_pct"] / 100):
            return False, f"sell price {order.price:.2f} too high vs avg {avg:.2f}"

        if deviation > ANTI_SCAM["max_price_deviation_pct"] * 2:
            return False, f"price deviation {deviation:.1f}% > max {ANTI_SCAM['max_price_deviation_pct'] * 2:.0f}%"

    return True, ""


def calculate_score(opp: ArbitrageOpportunity) -> float:
    spread_score = min(opp.spread_pct * 12, 35)
    speed_map = {SpeedType.FAST: 25, SpeedType.MEDIUM: 15, SpeedType.SLOW: 0}
    speed_score = speed_map.get(opp.speed, 0)
    liquidity_score = 15 if opp.liquidity_ok else 0
    trust_score = (opp.seller_completion_rate / 100) * 15
    volatility_score = 10 if opp.volatility_ok else 0
    return spread_score + speed_score + liquidity_score + trust_score + volatility_score


def assess_risk(opp: ArbitrageOpportunity) -> RiskLevel:
    if (opp.score >= 70
            and opp.speed == SpeedType.FAST
            and opp.seller_completion_rate >= 95
            and opp.liquidity_ok):
        return RiskLevel.LOW
    elif opp.score >= 30 and opp.seller_completion_rate >= 90:
        return RiskLevel.MEDIUM
    return RiskLevel.HIGH


def get_execution_ease(speed: SpeedType, liquidity_ok: bool) -> str:
    if speed == SpeedType.FAST and liquidity_ok:
        return "🟢 Easy"
    elif speed == SpeedType.MEDIUM or (speed == SpeedType.FAST and not liquidity_ok):
        return "🟡 Medium"
    return "🔴 Hard"


# Загальні назви які не є конкретним банком — ігноруємо
_GENERIC_METHODS = {
    "bank transfer", "банківський переказ", "банківський рахунок",
    "bank", "transfer", "переказ", "wire transfer",
    "банківський", "bank vlasnyi rakhunok", "bankvlasnyi rakhunok",
}


def find_common_payment_methods(
    buy_order: P2POrder,
    sell_order: P2POrder,
    user_banks: list[str] | None = None,
) -> list[str]:
    """
    Повертає банки які є одночасно:
      - у продавця buy-ордера
      - у покупця sell-ордера
      - у списку банків користувача (user_banks)
    Виключає загальні назви ('Bank Transfer' тощо).
    """
    sell_lower = {m.lower().strip() for m in sell_order.payment_methods if m}
    user_lower = {b.lower().strip() for b in user_banks} if user_banks else None
    common = []
    seen = set()
    for m in buy_order.payment_methods:
        if not m:
            continue
        key = m.lower().strip()
        if key in _GENERIC_METHODS or key in seen:
            continue
        if key not in sell_lower:
            continue
        if user_lower is not None and key not in user_lower:
            continue
        common.append(m)
        seen.add(key)
    return sorted(common)


def _pick_payment_method(order: P2POrder, user_banks: list[str] | None = None) -> str:
    """
    Повертає першу конкретну назву банку з ордера яка є у списку банків користувача.
    Якщо user_banks не задано — повертає першу не-generic назву.
    Якщо user_banks задано але жоден не збігся — повертає "" (не підставляємо чужий банк).
    """
    user_lower = {b.lower().strip() for b in user_banks} if user_banks else None
    for m in order.payment_methods:
        if not m:
            continue
        key = m.lower().strip()
        if key in _GENERIC_METHODS:
            continue
        if user_lower is not None and key not in user_lower:
            continue
        return m.strip()
    # Якщо user_banks заданий — не використовуємо fallback, повертаємо ""
    if user_lower:
        return ""
    # Якщо фільтр не заданий — повертаємо першу не-generic назву
    for m in order.payment_methods:
        if m and m.strip() and m.lower().strip() not in _GENERIC_METHODS:
            return m.strip()
    return ""


class ArbitrageEngine:
    def __init__(self, api: ExchangeAPI):
        self.api = api
        self.last_scan_stats: dict = {}
        self.last_buy_orders: list[P2POrder] = []
        self.last_sell_orders: list[P2POrder] = []

    def _compatible_amount(self, buy: P2POrder, sell: P2POrder, preferred_uah: float) -> float | None:
        """
        Find the best trade amount (UAH) that fits BOTH orders' min/max limits
        and their available liquidity.  Returns None if no valid overlap exists.
        Prefers the user's preferred_uah but adjusts up/down to satisfy constraints.
        """
        if buy.price <= 0 or sell.price <= 0:
            return None

        # UAH ranges from each order's declared limits
        buy_min = buy.min_amount if buy.min_amount > 0 else 0.0
        buy_max = buy.max_amount if buy.max_amount > 0 else float("inf")
        sell_min = sell.min_amount if sell.min_amount > 0 else 0.0
        sell_max = sell.max_amount if sell.max_amount > 0 else float("inf")

        # Cap by available USDT liquidity (convert to UAH at each order's price)
        if buy.available_amount > 0:
            buy_max = min(buy_max, buy.available_amount * buy.price)
        if sell.available_amount > 0:
            sell_max = min(sell_max, sell.available_amount * sell.price)

        # Intersection of both ranges
        lo = max(buy_min, sell_min)
        hi = min(buy_max, sell_max)

        if lo > hi or hi <= 0:
            return None  # No compatible amount exists

        # Clamp user preference to the valid range
        return max(lo, min(hi, preferred_uah))

    def _amounts_compatible(self, buy: P2POrder, sell: P2POrder, amount_uah: float) -> tuple[bool, str]:
        """Legacy wrapper — kept for callers that only need a bool."""
        result = self._compatible_amount(buy, sell, amount_uah)
        if result is None:
            return False, "no compatible amount range"
        return True, ""

    def _best_network(self, exchange: str) -> str:
        """Return the cheapest withdrawal network for the given exchange."""
        fees = WITHDRAWAL_FEES_USDT.get(exchange, {})
        if not fees:
            return "TRC20"
        return min(fees, key=fees.get)

    def _compute_p2p_profit(
        self,
        buy_order: P2POrder,
        sell_order: P2POrder,
        amount_uah: float,
        network: str,
        is_cross_exchange: bool,
    ) -> dict:
        """
        Compute real executable profit after ALL fees:
          - Network withdrawal fee (cross-exchange only)
          - P2P platform fee: 0% (P2P is free on all major exchanges)
          - Bank transfer: included in the P2P spread (no extra cost)
        When network == 'ALL', automatically picks the cheapest network
        for the buy exchange.
        """
        usdt_bought = amount_uah / buy_order.price

        if is_cross_exchange:
            # Auto-select cheapest network when ALL is chosen
            resolved_network = (
                self._best_network(buy_order.exchange)
                if network == "ALL"
                else network
            )
            withdrawal_fee_usdt = WITHDRAWAL_FEES_USDT.get(buy_order.exchange, {}).get(resolved_network, 1.0)
            usdt_after_fees = usdt_bought - withdrawal_fee_usdt
            withdrawal_fee_uah = withdrawal_fee_usdt * sell_order.price
        else:
            resolved_network = network if network != "ALL" else "TRC20"
            withdrawal_fee_usdt = 0.0
            withdrawal_fee_uah = 0.0
            usdt_after_fees = usdt_bought

        uah_received = usdt_after_fees * sell_order.price
        total_fees_uah = withdrawal_fee_uah
        profit_uah = uah_received - amount_uah
        profit_pct = (profit_uah / amount_uah) * 100
        spread = sell_order.price - buy_order.price
        spread_pct = (spread / buy_order.price) * 100

        return {
            "usdt_bought": usdt_bought,
            "usdt_after_fees": usdt_after_fees,
            "withdrawal_fee_usdt": withdrawal_fee_usdt,
            "withdrawal_fee_uah": withdrawal_fee_uah,
            "total_fees_uah": total_fees_uah,
            "uah_received": uah_received,
            "profit_uah": profit_uah,
            "profit_pct": profit_pct,
            "spread": spread,
            "spread_pct": spread_pct,
            "resolved_network": resolved_network,
        }

    async def find_p2p_to_p2p_same_exchange(
        self, buy_orders: list[P2POrder], sell_orders: list[P2POrder],
        settings: UserSettings
    ) -> list[ArbitrageOpportunity]:
        """
        Same-exchange P2P arbitrage:
        Buy USDT from a SELL ad → immediately sell to a BUY ad on the same platform.
        Both sides must share a common bank payment method.
        """
        opportunities = []
        buy_prices = [o.price for o in buy_orders if o.price > 0]
        sell_prices = [o.price for o in sell_orders if o.price > 0]

        # Group by exchange
        exchange_groups: dict[str, tuple[list, list]] = {}
        for o in buy_orders:
            exchange_groups.setdefault(o.exchange, ([], []))
            exchange_groups[o.exchange][0].append(o)
        for o in sell_orders:
            if o.exchange in exchange_groups:
                exchange_groups[o.exchange][1].append(o)

        for exchange, (buys, sells) in exchange_groups.items():
            for buy in buys[:20]:
                ok, reason = check_anti_scam(buy, buy_prices, "BUY", settings.min_completion_rate)
                if not ok:
                    logger.debug(f"[{exchange}] BUY {buy.nickname} rejected: {reason}")
                    continue
                for sell in sells[:20]:
                    ok, reason = check_anti_scam(sell, sell_prices, "SELL", settings.min_completion_rate)
                    if not ok:
                        logger.debug(f"[{exchange}] SELL {sell.nickname} rejected: {reason}")
                        continue

                    # Same-exchange P2P requires a shared bank (filtered by user settings)
                    common_methods = find_common_payment_methods(buy, sell, settings.banks)
                    if not common_methods:
                        logger.debug(f"[{exchange}] No common payment methods between {buy.nickname} and {sell.nickname}")
                        continue

                    # Sell price must beat buy price
                    if sell.price <= buy.price:
                        continue

                    trade_uah = self._compatible_amount(buy, sell, settings.amount_uah)
                    if trade_uah is None:
                        logger.debug(f"[{exchange}] No compatible amount range for orders")
                        continue

                    calc = self._compute_p2p_profit(buy, sell, trade_uah, settings.network, False)

                    if calc["profit_uah"] < settings.min_profit_uah:
                        continue

                    speed = classify_speed(
                        max(buy.avg_release_time, sell.avg_release_time),
                        buy.is_online and sell.is_online,
                    )

                    release_min = max(buy.avg_release_time, sell.avg_release_time) // 60

                    opp = ArbitrageOpportunity(
                        arb_type=ArbitrageType.P2P_TO_P2P,
                        buy_exchange=exchange,
                        sell_exchange=exchange,
                        buy_price=buy.price,
                        sell_price=sell.price,
                        spread=calc["spread"],
                        spread_pct=calc["spread_pct"],
                        profit_uah=calc["profit_uah"],
                        profit_pct=calc["profit_pct"],
                        amount_usdt=calc["usdt_bought"],
                        buy_order=buy,
                        sell_order=sell,
                        payment_method=common_methods[0],
                        execution_ease="",
                        speed=speed,
                        liquidity_ok=True,
                        seller_completion_rate=min(buy.completion_rate, sell.completion_rate),
                        seller_total_orders=min(buy.total_orders, sell.total_orders),
                        risk=RiskLevel.LOW,
                        score=0,
                        trade_steps=[
                            f"1. Надіслати {settings.amount_uah:,.0f} UAH через {common_methods[0].title()} › {buy.nickname}",
                            f"2. Отримати {calc['usdt_bought']:.2f} USDT @ {buy.price:.2f} грн (avg release ~{release_min} хв)",
                            f"3. Виставити USDT на продаж › {sell.nickname} @ {sell.price:.2f} грн",
                            f"4. Отримати {calc['uah_received']:,.0f} UAH на {common_methods[0].title()}",
                            f"5. Профіт: +{calc['profit_uah']:,.0f} UAH ({calc['profit_pct']:.2f}%) | Комісії: 0 UAH",
                        ],
                        fees_breakdown={
                            "network_fee_usdt": 0,
                            "bank_fee_uah": 0,
                            "total_fees_uah": 0,
                            "profit_uah": calc["profit_uah"],
                        },
                        network=settings.network,
                    )
                    opp.score = calculate_score(opp)
                    opp.risk = assess_risk(opp)
                    opp.execution_ease = get_execution_ease(opp.speed, opp.liquidity_ok)
                    opportunities.append(opp)

        return opportunities

    async def find_cross_exchange(
        self, buy_orders: list[P2POrder], sell_orders: list[P2POrder],
        settings: UserSettings
    ) -> list[ArbitrageOpportunity]:
        """
        Cross-exchange P2P:
        Buy USDT via P2P on exchange A → withdraw via network → sell via P2P on exchange B.
        Network withdrawal fee is deducted from profit.

        Groups orders by exchange so every exchange pair is always checked,
        even when one exchange returns many more rows than another.
        """
        opportunities = []
        buy_prices = [o.price for o in buy_orders if o.price > 0]
        sell_prices = [o.price for o in sell_orders if o.price > 0]

        # Group by exchange — take best 15 from each so no exchange crowds out others
        PER_EXCHANGE = 15
        buy_by_ex: dict[str, list[P2POrder]] = {}
        for o in buy_orders:
            buy_by_ex.setdefault(o.exchange, [])
            if len(buy_by_ex[o.exchange]) < PER_EXCHANGE:
                buy_by_ex[o.exchange].append(o)

        sell_by_ex: dict[str, list[P2POrder]] = {}
        for o in sell_orders:
            sell_by_ex.setdefault(o.exchange, [])
            if len(sell_by_ex[o.exchange]) < PER_EXCHANGE:
                sell_by_ex[o.exchange].append(o)

        logger.info(
            f"Cross-exchange: buy groups={list(buy_by_ex.keys())}, "
            f"sell groups={list(sell_by_ex.keys())}"
        )

        # Check every buy-exchange × sell-exchange pair
        for buy_ex, buys in buy_by_ex.items():
            for sell_ex, sells in sell_by_ex.items():
                if buy_ex == sell_ex:
                    continue
                for buy in buys:
                    ok, reason = check_anti_scam(buy, buy_prices, "BUY", settings.min_completion_rate)
                    if not ok:
                        logger.debug(f"[cross {buy_ex}→{sell_ex}] BUY {buy.nickname} rejected: {reason}")
                        continue
                    for sell in sells:
                        ok, reason = check_anti_scam(sell, sell_prices, "SELL", settings.min_completion_rate)
                        if not ok:
                            continue
                        if sell.price <= buy.price:
                            continue

                        trade_uah = self._compatible_amount(buy, sell, settings.amount_uah)
                        if trade_uah is None:
                            continue

                        calc = self._compute_p2p_profit(buy, sell, trade_uah, settings.network, True)

                        if calc["profit_uah"] < settings.min_profit_uah:
                            continue

                        speed = classify_speed(
                            max(buy.avg_release_time, sell.avg_release_time),
                            buy.is_online and sell.is_online,
                        )

                        withdrawal_fee_usdt = calc["withdrawal_fee_usdt"]
                        withdrawal_fee_uah = calc["withdrawal_fee_uah"]
                        resolved_net = calc.get("resolved_network", settings.network)
                        release_min = max(buy.avg_release_time, sell.avg_release_time) // 60

                        opp = ArbitrageOpportunity(
                            arb_type=ArbitrageType.CROSS_EXCHANGE,
                            buy_exchange=buy.exchange,
                            sell_exchange=sell.exchange,
                            buy_price=buy.price,
                            sell_price=sell.price,
                            spread=calc["spread"],
                            spread_pct=calc["spread_pct"],
                            profit_uah=calc["profit_uah"],
                            profit_pct=calc["profit_pct"],
                            amount_usdt=calc["usdt_bought"],
                            buy_order=buy,
                            sell_order=sell,
                            payment_method=_pick_payment_method(buy, settings.banks),
                            execution_ease="",
                            speed=speed,
                            liquidity_ok=True,
                            seller_completion_rate=min(buy.completion_rate, sell.completion_rate),
                            seller_total_orders=min(buy.total_orders, sell.total_orders),
                            risk=RiskLevel.LOW,
                            score=0,
                            trade_steps=[
                                f"1. Купити {calc['usdt_bought']:.2f} USDT на {buy.exchange} @ {buy.price:.2f} грн",
                                f"   └─ Продавець: {buy.nickname} | Час: ~{release_min} хв",
                                f"2. Вивести {calc['usdt_bought']:.2f} USDT › {sell.exchange} через {resolved_net}",
                                f"   └─ Комісія мережі: {withdrawal_fee_usdt:.2f} USDT ≈ {withdrawal_fee_uah:.0f} грн",
                                f"3. Продати {calc['usdt_after_fees']:.2f} USDT на {sell.exchange} @ {sell.price:.2f} грн",
                                f"4. Отримати {calc['uah_received']:,.0f} UAH",
                                f"5. Профіт: +{calc['profit_uah']:,.0f} UAH ({calc['profit_pct']:.2f}%) після всіх комісій",
                            ],
                            fees_breakdown={
                                "network": resolved_net,
                                "withdrawal_fee_usdt": withdrawal_fee_usdt,
                                "withdrawal_fee_uah": round(withdrawal_fee_uah, 2),
                                "total_fees_uah": round(calc["total_fees_uah"], 2),
                                "profit_uah": calc["profit_uah"],
                            },
                            network=resolved_net,
                        )
                        opp.score = calculate_score(opp)
                        opp.risk = assess_risk(opp)
                        opp.execution_ease = get_execution_ease(opp.speed, opp.liquidity_ok)
                        opportunities.append(opp)

        return opportunities

    async def find_triangular(self, settings: UserSettings) -> list[ArbitrageOpportunity]:
        """
        Spot triangular: BTC/USDT price difference between two spot exchanges.
        Includes both sides' trading fees (taker 0.1%).
        Only viable if spread > both fees combined AND volatility is low.
        Tries multiple exchange pairs in case some are geo-blocked.
        """
        opportunities = []
        try:
            # Fetch all spot prices concurrently; some may be geo-blocked
            prices = await asyncio.gather(
                self.api.fetch_spot_price("Binance", "BTC"),
                self.api.fetch_spot_price("Bybit", "BTC"),
                self.api.fetch_spot_price("OKX", "BTC"),
                self.api.fetch_spot_price("KuCoin", "BTC"),
                return_exceptions=True,
            )
            named = {}
            for name, p in zip(["Binance", "Bybit", "OKX", "KuCoin"], prices):
                if p and not isinstance(p, Exception) and p.bid > 0 and p.ask > 0:
                    named[name] = p

            if len(named) < 2:
                logger.info(f"Triangular: only {len(named)} spot prices available — skipping")
                return []

            logger.info(f"Triangular: got spot prices from {list(named.keys())}")

            # Build all exchange pairs to check
            exchange_list = list(named.items())
            candidates = []
            for i, (ex_a, price_a) in enumerate(exchange_list):
                for ex_b, price_b in exchange_list[i + 1:]:
                    candidates += [
                        (ex_a, price_a.ask, ex_b, price_b.bid, price_a, price_b),
                        (ex_b, price_b.ask, ex_a, price_a.bid, price_b, price_a),
                    ]

            for buy_ex, buy_price, sell_ex, sell_price, buy_data, sell_data in candidates:
                if sell_price <= buy_price:
                    continue

                usdt_amount = settings.amount_uah / buy_price
                btc_bought = usdt_amount / buy_price
                fee_buy = btc_bought * SPOT_TRADING_FEE.get(buy_ex, 0.001)
                btc_after_buy_fee = btc_bought - fee_buy
                usdt_received = btc_after_buy_fee * sell_price
                fee_sell = usdt_received * SPOT_TRADING_FEE.get(sell_ex, 0.001)
                usdt_final = usdt_received - fee_sell
                profit_usdt = usdt_final - usdt_amount
                profit_uah = profit_usdt * buy_price   # approximate
                profit_pct = (profit_usdt / usdt_amount) * 100
                spread = sell_price - buy_price
                spread_pct = (spread / buy_price) * 100

                if profit_uah < settings.min_profit_uah:
                    continue

                # Reject if volatility too high — risky to execute cross-exchange
                vol_ok = (abs(buy_data.price_change_pct) < 2.0
                          and abs(sell_data.price_change_pct) < 2.0)
                if not vol_ok:
                    logger.debug(f"Triangular {buy_ex}->{sell_ex} skipped: high volatility")
                    continue

                total_fees_usdt = (fee_buy * sell_price) + fee_sell

                opp = ArbitrageOpportunity(
                    arb_type=ArbitrageType.TRIANGULAR,
                    buy_exchange=buy_ex,
                    sell_exchange=sell_ex,
                    buy_price=buy_price,
                    sell_price=sell_price,
                    spread=spread,
                    spread_pct=spread_pct,
                    profit_uah=profit_uah,
                    profit_pct=profit_pct,
                    amount_usdt=usdt_amount,
                    buy_order=None,
                    sell_order=None,
                    payment_method="Spot",
                    execution_ease="🟢 Easy",
                    speed=SpeedType.FAST,
                    liquidity_ok=True,
                    seller_completion_rate=100,
                    seller_total_orders=999999,
                    risk=RiskLevel.LOW,
                    score=0,
                    trade_steps=[
                        f"1. Купити {usdt_amount:.4f} BTC на {buy_ex} @ {buy_price:,.2f} USDT",
                        f"   └─ Комісія: {fee_buy:.6f} BTC",
                        f"2. Продати {btc_after_buy_fee:.4f} BTC на {sell_ex} @ {sell_price:,.2f} USDT",
                        f"   └─ Комісія: {fee_sell:.4f} USDT",
                        f"3. Профіт: +{profit_usdt:.4f} USDT ≈ {profit_uah:,.0f} UAH ({profit_pct:.3f}%)",
                        f"   └─ Всього комісій: {total_fees_usdt:.4f} USDT",
                    ],
                    fees_breakdown={
                        "spot_fee_buy_btc": round(fee_buy, 8),
                        "spot_fee_sell_usdt": round(fee_sell, 6),
                        "total_fees_usdt": round(total_fees_usdt, 6),
                        "profit_usdt": round(profit_usdt, 6),
                        "profit_uah": round(profit_uah, 2),
                    },
                    volatility_ok=vol_ok,
                    network=settings.network,
                )
                opp.score = calculate_score(opp)
                opportunities.append(opp)

        except Exception as e:
            logger.warning(f"Triangular error: {e}")

        return opportunities

    async def find_closest_opportunities(
        self,
        buy_orders: list[P2POrder],
        sell_orders: list[P2POrder],
        settings: UserSettings,
        top_n: int = 3,
    ) -> list[dict]:
        """
        When no opportunities pass the user's filters — find the closest ones.
        Returns raw profit data with hints on what needs to change.
        """
        results = []
        buy_prices = [o.price for o in buy_orders if o.price > 0]
        sell_prices = [o.price for o in sell_orders if o.price > 0]

        # Cross-exchange candidates (no profit filter)
        for buy in buy_orders[:15]:
            ok, _ = check_anti_scam(buy, buy_prices, "BUY", settings.min_completion_rate)
            if not ok:
                continue
            for sell in sell_orders[:15]:
                if sell.exchange == buy.exchange:
                    continue
                ok, _ = check_anti_scam(sell, sell_prices, "SELL", settings.min_completion_rate)
                if not ok:
                    continue
                if sell.price <= buy.price:
                    continue

                calc = self._compute_p2p_profit(buy, sell, settings.amount_uah, settings.network, True)
                profit = calc["profit_uah"]
                gap = settings.min_profit_uah - profit

                # How much amount needed to reach min profit
                if calc["spread_pct"] > 0:
                    needed_amount = (settings.min_profit_uah + calc["withdrawal_fee_uah"]) / (calc["spread_pct"] / 100)
                else:
                    needed_amount = 0

                results.append({
                    "buy_exchange": buy.exchange,
                    "sell_exchange": sell.exchange,
                    "buy_price": buy.price,
                    "sell_price": sell.price,
                    "spread_pct": calc["spread_pct"],
                    "profit_uah": profit,
                    "gap_uah": max(0, gap),
                    "needed_amount_uah": round(needed_amount),
                    "network_fee_uah": calc["withdrawal_fee_uah"],
                    "buy_nickname": buy.nickname,
                    "sell_nickname": sell.nickname,
                    "network": settings.network,
                    "type": "cross",
                })

        # Same-exchange candidates
        exchange_groups: dict[str, tuple[list, list]] = {}
        for o in buy_orders:
            exchange_groups.setdefault(o.exchange, ([], []))
            exchange_groups[o.exchange][0].append(o)
        for o in sell_orders:
            if o.exchange in exchange_groups:
                exchange_groups[o.exchange][1].append(o)

        for exchange, (buys, sells) in exchange_groups.items():
            for buy in buys[:10]:
                ok, _ = check_anti_scam(buy, buy_prices, "BUY", settings.min_completion_rate)
                if not ok:
                    continue
                for sell in sells[:10]:
                    ok, _ = check_anti_scam(sell, sell_prices, "SELL", settings.min_completion_rate)
                    if not ok:
                        continue
                    if sell.price <= buy.price:
                        continue
                    common = find_common_payment_methods(buy, sell, settings.banks or None)
                    if not common:
                        continue
                    calc = self._compute_p2p_profit(buy, sell, settings.amount_uah, settings.network, False)
                    profit = calc["profit_uah"]
                    gap = settings.min_profit_uah - profit
                    results.append({
                        "buy_exchange": exchange,
                        "sell_exchange": exchange,
                        "buy_price": buy.price,
                        "sell_price": sell.price,
                        "spread_pct": calc["spread_pct"],
                        "profit_uah": profit,
                        "gap_uah": max(0, gap),
                        "needed_amount_uah": round(settings.amount_uah * (settings.min_profit_uah / profit)) if profit > 0 else 0,
                        "network_fee_uah": 0,
                        "buy_nickname": buy.nickname,
                        "sell_nickname": sell.nickname,
                        "network": settings.network,
                        "type": "same",
                        "payment": common[0],
                    })

        # Sort: closest to profitable first (highest profit = smallest gap)
        results.sort(key=lambda x: x["profit_uah"], reverse=True)
        return results[:top_n]

    def estimate_min_viable_amount(self, network: str = "TRC20") -> float:
        """Calculate the minimum trade amount (UAH) at which cross-exchange arb can be profitable."""
        fee_usdt = WITHDRAWAL_FEES_USDT.get("Binance", {}).get(network, 1.0)
        typical_p2p_price = 41.0
        fee_uah = fee_usdt * typical_p2p_price
        # Need 1% net profit after fee; solve for amount: amount * 0.01 > fee_uah + 100
        return round((fee_uah + 100) / 0.01)

    async def scan(self, settings: UserSettings) -> list[ArbitrageOpportunity]:
        logger.info(
            f"Scan start | amount={settings.amount_uah:.0f} UAH | "
            f"min_profit={settings.min_profit_uah:.0f} UAH | "
            f"risk={settings.risk_level} | exchanges={settings.exchanges}"
        )

        # Determine which arb types are enabled (default: all)
        arb_types = set(getattr(settings, "arb_types", ["p2p_same", "cross_exchange", "triangular"]))
        if not arb_types:
            arb_types = {"p2p_same", "cross_exchange", "triangular"}

        need_p2p = bool(arb_types & {"p2p_same", "cross_exchange"})
        need_tri = "triangular" in arb_types

        buy_task = self.api.fetch_all_p2p("BUY", settings.amount_uah, settings.exchanges, settings.banks) if need_p2p else asyncio.sleep(0)
        sell_task = self.api.fetch_all_p2p("SELL", settings.amount_uah, settings.exchanges, settings.banks) if need_p2p else asyncio.sleep(0)
        triangular_task = self.find_triangular(settings) if need_tri else asyncio.sleep(0)

        buy_orders_raw, sell_orders_raw, tri_opps_raw = await asyncio.gather(
            buy_task, sell_task, triangular_task, return_exceptions=True
        )

        buy_orders = [] if (isinstance(buy_orders_raw, Exception) or not need_p2p) else list(buy_orders_raw or [])
        sell_orders = [] if (isinstance(sell_orders_raw, Exception) or not need_p2p) else list(sell_orders_raw or [])
        tri_opps = [] if (isinstance(tri_opps_raw, Exception) or not need_tri) else list(tri_opps_raw or [])

        if need_p2p and isinstance(buy_orders_raw, Exception):
            logger.error(f"Buy orders fetch failed: {buy_orders_raw}")
        if need_p2p and isinstance(sell_orders_raw, Exception):
            logger.error(f"Sell orders fetch failed: {sell_orders_raw}")
        if need_tri and isinstance(tri_opps_raw, Exception):
            logger.warning(f"Triangular scan failed: {tri_opps_raw}")

        self.last_buy_orders = list(buy_orders)
        self.last_sell_orders = list(sell_orders)

        logger.info(
            f"Fetched {len(buy_orders)} buy orders, {len(sell_orders)} sell orders "
            f"from {len(settings.exchanges)} exchanges"
        )

        # Log how many orders pass anti-scam for diagnostics
        buy_prices = [o.price for o in buy_orders if o.price > 0]
        sell_prices = [o.price for o in sell_orders if o.price > 0]
        mc = settings.min_completion_rate
        trusted_buys = sum(1 for o in buy_orders if check_anti_scam(o, buy_prices, "BUY", mc)[0])
        trusted_sells = sum(1 for o in sell_orders if check_anti_scam(o, sell_prices, "SELL", mc)[0])
        logger.info(f"Trusted orders: {trusted_buys} buy, {trusted_sells} sell (after anti-scam)")

        same_ex_task = self.find_p2p_to_p2p_same_exchange(buy_orders, sell_orders, settings) if "p2p_same" in arb_types else asyncio.sleep(0)
        cross_ex_task = self.find_cross_exchange(buy_orders, sell_orders, settings) if "cross_exchange" in arb_types else asyncio.sleep(0)

        same_ex_raw, cross_ex_raw = await asyncio.gather(
            same_ex_task, cross_ex_task, return_exceptions=True
        )
        same_ex_opps = [] if (isinstance(same_ex_raw, Exception) or "p2p_same" not in arb_types) else list(same_ex_raw or [])
        cross_ex_opps = [] if (isinstance(cross_ex_raw, Exception) or "cross_exchange" not in arb_types) else list(cross_ex_raw or [])

        if "p2p_same" in arb_types and isinstance(same_ex_raw, Exception):
            logger.error(f"Same-exchange scan error: {same_ex_raw}")
        if "cross_exchange" in arb_types and isinstance(cross_ex_raw, Exception):
            logger.error(f"Cross-exchange scan error: {cross_ex_raw}")

        all_opps = list(same_ex_opps) + list(cross_ex_opps) + list(tri_opps)

        # Apply risk filter
        risk_filter = {
            "LOW":    [RiskLevel.LOW],
            "MEDIUM": [RiskLevel.LOW, RiskLevel.MEDIUM],
            "HIGH":   [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH],
        }
        allowed_risks = risk_filter.get(settings.risk_level, [RiskLevel.LOW, RiskLevel.MEDIUM])

        filtered = [
            o for o in all_opps
            if o.risk in allowed_risks and o.is_viable
        ]
        filtered.sort(key=lambda x: x.score, reverse=True)

        # Exchange coverage: count per exchange
        ex_counts = {}
        for o in list(buy_orders) + list(sell_orders):
            ex_counts[o.exchange] = ex_counts.get(o.exchange, 0) + 1

        # Min viable amount for cross-exchange given current network
        min_viable = self.estimate_min_viable_amount(settings.network)

        self.last_scan_stats = {
            "total_raw": len(buy_orders) + len(sell_orders),
            "trusted_buys": trusted_buys,
            "trusted_sells": trusted_sells,
            "same_ex": len(same_ex_opps),
            "cross_ex": len(cross_ex_opps),
            "triangular": len(tri_opps),
            "final": len(filtered),
            "exchanges_with_data": ex_counts,
            "requested_exchanges": list(settings.exchanges),
            "risk_level": settings.risk_level,
            "amount_uah": settings.amount_uah,
            "min_viable_amount": min_viable,
            "network": settings.network,
        }

        logger.info(
            f"Scan complete: {len(same_ex_opps)} same-ex, {len(cross_ex_opps)} cross-ex, "
            f"{len(tri_opps)} triangular → {len(filtered)} viable after risk filter"
        )
        return filtered[:10]

    async def verify_opportunities(
        self, opps: list[ArbitrageOpportunity], settings: UserSettings
    ) -> list[ArbitrageOpportunity]:
        """
        Повторно запитує ордери з бірж, задіяних у топ-можливостях, і підтверджує,
        що ціни не змінились більш ніж на 0.5% з моменту першого скану.
        Позначає кожну можливість як verified=True/False.
        """
        if not opps:
            return opps

        # Знаходимо унікальні біржі для перевірки (тільки P2P-типи)
        exchanges_needed: set[str] = set()
        for o in opps:
            if o.buy_order:
                exchanges_needed.add(o.buy_exchange)
            if o.sell_order:
                exchanges_needed.add(o.sell_exchange)
        exchanges_needed = {
            e for e in exchanges_needed
            if e in ["Binance", "Bybit", "OKX", "Bitget", "MEXC", "Gate.io", "HTX", "KuCoin"]
        }

        if not exchanges_needed:
            return opps

        logger.info(f"Verify: re-fetching from {exchanges_needed}")
        verify_start = time.time()

        try:
            buy2, sell2 = await asyncio.gather(
                self.api.fetch_all_p2p(
                    "BUY", settings.amount_uah,
                    list(exchanges_needed), settings.banks
                ),
                self.api.fetch_all_p2p(
                    "SELL", settings.amount_uah,
                    list(exchanges_needed), settings.banks
                ),
            )
        except Exception as e:
            logger.warning(f"Verify: re-fetch failed — {e}")
            return opps

        # Індексуємо re-fetched ордери за (exchange, side) → найкраща ціна
        buy_price_by_ex: dict[str, float] = {}
        sell_price_by_ex: dict[str, float] = {}
        for o in buy2:
            if o.price > 0:
                cur = buy_price_by_ex.get(o.exchange, 0)
                buy_price_by_ex[o.exchange] = max(cur, o.price)  # найвища buy (користувач продає дорожче)
        for o in sell2:
            if o.price > 0:
                cur = sell_price_by_ex.get(o.exchange, float("inf"))
                sell_price_by_ex[o.exchange] = min(cur, o.price)  # найнижча sell (користувач купує дешевше)

        now = time.time()
        TOLERANCE = 0.005  # 0.5%

        for opp in opps:
            v_buy = buy_price_by_ex.get(opp.buy_exchange, 0)
            v_sell = sell_price_by_ex.get(opp.sell_exchange, 0)
            if v_buy <= 0 or v_sell <= 0:
                opp.verified = False
                continue
            buy_ok = abs(v_buy - opp.buy_price) / (opp.buy_price or 1) <= TOLERANCE
            sell_ok = abs(v_sell - opp.sell_price) / (opp.sell_price or 1) <= TOLERANCE
            opp.verified = buy_ok and sell_ok
            opp.verified_at = now
            opp.verified_buy_price = v_buy
            opp.verified_sell_price = v_sell

        verified_count = sum(1 for o in opps if o.verified)
        elapsed = time.time() - verify_start
        logger.info(
            f"Verify done in {elapsed:.1f}s: {verified_count}/{len(opps)} confirmed "
            f"(buy prices: {buy_price_by_ex}, sell prices: {sell_price_by_ex})"
        )
        return opps
