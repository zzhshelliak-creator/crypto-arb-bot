import asyncio
import aiohttp
import logging
import time
from typing import Optional
from models.types import P2POrder, SpotPrice

logger = logging.getLogger(__name__)

CACHE = {}
CACHE_TTL = 8


def _cache_get(key: str):
    entry = CACHE.get(key)
    if entry and time.time() - entry["ts"] < CACHE_TTL:
        return entry["data"]
    return None


def _cache_set(key: str, data):
    CACHE[key] = {"data": data, "ts": time.time()}


def clear_exchange_cache(exchanges: list[str]) -> int:
    """Видаляє кешовані P2P дані для зазначених бірж, щоб наступний запит пішов напряму до API."""
    removed = 0
    keys_to_del = [
        k for k in list(CACHE.keys())
        if any(ex.lower() in k.lower() for ex in exchanges)
    ]
    for k in keys_to_del:
        CACHE.pop(k, None)
        removed += 1
    return removed


def _banks_key(banks: list[str]) -> str:
    return "_".join(sorted(b.lower() for b in banks)) if banks else "all"


# Generic payment labels that are NOT specific banks — never count as a match for a user's bank filter.
# Must stay in sync with _GENERIC_METHODS in arbitrage_engine.py and _GENERIC_BANK_LABELS in formatters.py
_GENERIC_PAYMENT_KEYS: set[str] = {
    "bank transfer", "банківський переказ", "банківський рахунок",
    "bank", "transfer", "переказ", "wire transfer",
    "банківський", "bank vlasnyi rakhunok", "bankvlasnyi rakhunok",
}


def _bank_match(pay_methods: list[str], banks: list[str]) -> bool:
    """Return True if at least one user-selected bank is explicitly present in the order.

    Rules:
    - If user has no bank filter → accept any order.
    - Generic payment labels (e.g. 'Банківський переказ', 'bank transfer') are never
      counted as a concrete bank, even if they appear in pay_methods.
    - An order whose payment methods are *only* generic labels is rejected when the
      user has a bank filter (because no specific bank from the filter matches).
    """
    if not banks:
        return True
    # Only specific (non-generic) methods count toward a match
    specific = {m.lower().strip() for m in pay_methods if m and m.lower().strip() not in _GENERIC_PAYMENT_KEYS}
    return any(b.lower().strip() in specific for b in banks)


NETWORK_FEES = {
    "TRC20": 1.0,
    "ERC20": 5.0,
    "BEP20": 0.5,
    "SOL": 0.1,
    "APT": 0.5,
}

SPOT_TRADING_FEE = {
    "Binance": 0.001,
    "Bybit":   0.001,
    "OKX":     0.001,
}

WITHDRAWAL_FEES_USDT = {
    "Binance": {"TRC20": 1.0,  "ERC20": 4.5,  "BEP20": 0.8, "SOL": 0.1, "APT": 0.8},
    "Bybit":   {"TRC20": 1.0,  "ERC20": 5.0,  "BEP20": 0.8, "SOL": 0.1, "APT": 1.0},
    "OKX":     {"TRC20": 1.5,  "ERC20": 5.5,  "BEP20": 0.8, "SOL": 0.1, "APT": 0.5},
}

P2P_BANKS = {
    "Binance": ["PrivatBank", "Monobank", "PUMB", "A-Bank", "Oschadbank", "Raiffeisen"],
    "Bybit":   ["PrivatBank", "Monobank", "PUMB", "A-Bank"],
    "OKX":     ["PrivatBank", "Monobank", "PUMB"],
}

BANK_COMMISSIONS = {
    "PrivatBank": 0.0,
    "Monobank": 0.0,
    "PUMB": 0.0,
    "A-Bank": 0.0,
    "Oschadbank": 0.0,
    "Raiffeisen": 1.5,
}

BYBIT_PAYMENT_MAP = {
    "1":   "Банківський переказ",
    "14":  "PrivatBank",
    "31":  "PrivatBank",
    "43":  "Monobank",
    "49":  "Ukrgasbank",
    "60":  "Oschadbank",
    "61":  "A-Bank",
    "63":  "PUMB",
    "64":  "PUMB",
    "80":  "Oschadbank",
    "526": "PUMB",
    "544": "Globus Bank",
    "545": "Raiffeisen",
    "623": "A-Bank",
    "660": "Ukrsibbank",
    "773": "Sense SuperApp",
}

# Нормалізація назв банків з різних бірж до єдиного формату
_BANK_NORMALIZE: dict[str, str | None] = {
    "ABank":                  "A-Bank",
    "PUMBBank":               "PUMB",
    "Raiffaisen Bank":        "Raiffeisen",
    "RaiffeisenBankAval":     "Raiffeisen",
    "Raiffaizen":             "Raiffeisen",
    "SenseSuperApp":          "Sense SuperApp",
    "BankVlasnyiRakhunok":    "Банківський рахунок",
    "Bank Vlasnyi Rakhunok":  "Банківський рахунок",
    "Bank Transfer":          None,   # generic — не конкретний банк
    "Monobankiban":           "Monobank",
    "alliancecard":           "Alliance Card",
    "bank":                   None,   # занадто загальна назва — пропускаємо
    "Idea Bank":              "Idea Bank",
}


def _normalize_bank(name: str) -> str | None:
    """Нормалізує назву банку. Повертає None якщо треба пропустити."""
    if not name or not name.strip():
        return None
    # Пропускаємо чисті числа (незамаплені коди Bybit)
    if name.strip().isdigit():
        return None
    return _BANK_NORMALIZE.get(name, name)

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
}


class ExchangeAPI:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if not self.session or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=4, connect=2)
            self.session = aiohttp.ClientSession(timeout=timeout)
        return self.session

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def _get(self, url: str, params: dict = None, headers: dict = None, retries: int = 0):
        session = await self._get_session()
        h = {**_BROWSER_HEADERS, **(headers or {})}
        try:
            async with session.get(url, params=params, headers=h) as resp:
                if resp.status == 200:
                    return await resp.json(content_type=None)
                logger.warning(f"HTTP {resp.status} for {url}")
        except asyncio.TimeoutError:
            logger.warning(f"Timeout for {url}")
        except Exception as e:
            logger.warning(f"Error fetching {url}: {e}")
        return None

    async def _post(self, url: str, json_data: dict = None, headers: dict = None, retries: int = 0):
        session = await self._get_session()
        h = {**_BROWSER_HEADERS, "Content-Type": "application/json", **(headers or {})}
        try:
            async with session.post(url, json=json_data, headers=h) as resp:
                if resp.status == 200:
                    return await resp.json(content_type=None)
                logger.warning(f"POST HTTP {resp.status} for {url}")
        except asyncio.TimeoutError:
            logger.warning(f"POST Timeout for {url}")
        except Exception as e:
            logger.warning(f"POST Error {url}: {e}")
        return None

    # ─────────────────────────── BINANCE ───────────────────────────

    async def fetch_binance_p2p(self, side: str, amount: float, banks: list[str]) -> list[P2POrder]:
        cache_key = f"binance_p2p_{side}_{amount}_{_banks_key(banks)}"
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        # Binance limits rows to max 20 — fetch 3 pages to get 60 results total
        url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
        base_payload = {
            "fiat": "UAH",
            "rows": 20,
            "tradeType": side,
            "asset": "USDT",
            "countries": [],
            "proMerchantAds": False,
            "shieldMerchantAds": False,
            "filterType": "all",
            "periods": [],
            "additionalKycVerifyFilter": 0,
            "publisherType": None,
            "payTypes": banks if banks else [],
            "classifies": ["mass", "profession"],
            "transAmount": str(int(amount)) if amount else "",
        }
        headers = {
            "Content-Type": "application/json",
            "Referer": "https://p2p.binance.com/",
            "Origin": "https://p2p.binance.com",
        }
        all_items: list = []
        for page in range(1, 4):  # pages 1, 2, 3
            payload = {**base_payload, "page": page}
            data = await self._post(url, json_data=payload, headers=headers)
            page_items = (data or {}).get("data") or []
            if not page_items:
                break
            all_items.extend(page_items)
            if len(page_items) < 20:
                break  # last page

        orders = []
        for item in all_items:
            try:
                adv = item.get("adv", {})
                advertiser = item.get("advertiser", {})
                pay_types = [p.get("identifier", "") for p in adv.get("tradeMethods", [])]
                if not _bank_match(pay_types, banks):
                    continue
                order = P2POrder(
                    exchange="Binance",
                    order_id=adv.get("advNo", ""),
                    side=side,
                    price=float(adv.get("price", 0)),
                    min_amount=float(adv.get("minSingleTransAmount", 0)),
                    max_amount=float(adv.get("maxSingleTransAmount", 0)),
                    available_amount=float(adv.get("tradableQuantity", 0)),
                    completion_rate=float(advertiser.get("monthFinishRate", 0)) * 100,
                    total_orders=int(advertiser.get("monthOrderCount", 0)),
                    is_merchant=bool(advertiser.get("userType") == "merchant"),
                    payment_methods=pay_types,
                    avg_release_time=int(adv.get("avgLeadTimeSec", 300) or 300),
                    is_online=bool(advertiser.get("activeTimeInSecond", 9999) < 300),
                    nickname=advertiser.get("nickName", "Unknown"),
                )
                orders.append(order)
            except Exception as e:
                logger.debug(f"Binance P2P parse error: {e}")

        logger.info(f"Binance P2P {side}: fetched {len(orders)} orders")
        _cache_set(cache_key, orders)
        return orders

    # ─────────────────────────── BYBIT ───────────────────────────

    async def fetch_bybit_p2p(self, side: str, amount: float, banks: list[str]) -> list[P2POrder]:
        cache_key = f"bybit_p2p_{side}_{amount}_{_banks_key(banks)}"
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        side_map = {"BUY": "1", "SELL": "0"}
        payload = {
            "tokenId": "USDT",
            "currencyId": "UAH",
            "payment": [],
            "side": side_map.get(side, "1"),
            "size": "50",
            "page": "1",
            "amount": str(int(amount)) if amount else "",
        }
        headers = {
            "Content-Type": "application/json",
            "Referer": "https://www.bybit.com/fiat/trade/otc/",
            "Origin": "https://www.bybit.com",
        }

        data = None
        for url in [
            "https://api2.bybit.com/fiat/otc/item/online",
            "https://api.bybit.com/fiat/otc/item/online",
        ]:
            data = await self._post(url, json_data=payload, headers=headers)
            if data and "result" in data and "items" in data.get("result", {}):
                break

        orders = []
        if data and "result" in data and "items" in data["result"]:
            for item in data["result"]["items"]:
                try:
                    raw_payments = item.get("payments", [])
                    pay_methods = []
                    for p in raw_payments:
                        if isinstance(p, dict):
                            pay_methods.append(p.get("paymentType", ""))
                        else:
                            # Bybit returns numeric string IDs — map to bank names
                            pay_methods.append(BYBIT_PAYMENT_MAP.get(str(p), str(p)))
                    if not _bank_match(pay_methods, banks):
                        continue
                    completion = float(item.get("recentExecuteRate", "0") or 0)
                    order = P2POrder(
                        exchange="Bybit",
                        order_id=item.get("id", ""),
                        side=side,
                        price=float(item.get("price", 0)),
                        min_amount=float(item.get("minAmount", 0)),
                        max_amount=float(item.get("maxAmount", 0)),
                        available_amount=float(item.get("quantity", 0)),
                        completion_rate=completion * 100 if completion <= 1 else completion,
                        total_orders=int(item.get("recentOrderNum", 0)),
                        is_merchant=bool(item.get("authTag") == "merchant"),
                        payment_methods=pay_methods,
                        avg_release_time=int(item.get("avgReleaseTime", 300) or 300),
                        is_online=bool(item.get("isOnline", False)),
                        nickname=item.get("nickName", "Unknown"),
                    )
                    orders.append(order)
                except Exception as e:
                    logger.debug(f"Bybit P2P parse error: {e}")

        logger.info(f"Bybit P2P {side}: fetched {len(orders)} orders")
        _cache_set(cache_key, orders)
        return orders

    # ─────────────────────────── OKX ───────────────────────────

    async def fetch_okx_p2p(self, side: str, amount: float, banks: list[str]) -> list[P2POrder]:
        cache_key = f"okx_p2p_{side}_{amount}_{_banks_key(banks)}"
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        # OKX reversed: user wants to BUY USDT → they need a SELL ad from the market
        side_map = {"BUY": "sell", "SELL": "buy"}
        params = {
            "quoteCurrency": "UAH",
            "baseCurrency": "USDT",
            "side": side_map.get(side, "sell"),
            "paymentMethod": "all",
            "userType": "all",
            "showTrade": "false",
            "showFollow": "false",
            "showAlreadyTraded": "false",
            "isAds": "false",
            "limit": "50",
            "page": "1",
        }
        if amount:
            params["amount"] = str(int(amount))

        data = None
        for url in [
            "https://www.okx.com/v3/c2c/tradingOrders/books",
            "https://okx.com/v3/c2c/tradingOrders/books",
        ]:
            data = await self._get(url, params=params)
            if data and "data" in data:
                break

        orders = []
        if data and "data" in data:
            d = data["data"]
            # OKX returns {"data": {"buy": [...], "sell": [...]}}
            items = []
            if isinstance(d, dict):
                key = "sell" if side == "BUY" else "buy"
                items = d.get(key, [])
            elif isinstance(d, list):
                items = d

            for item in items:
                try:
                    pay_methods = []
                    raw_pay = item.get("paymentMethods", item.get("payments", []))
                    if isinstance(raw_pay, list):
                        for p in raw_pay:
                            if isinstance(p, dict):
                                pay_methods.append(p.get("paymentMethod", p.get("name", "")))
                            else:
                                pay_methods.append(str(p))
                    if not _bank_match(pay_methods, banks):
                        continue
                    completion_raw = float(item.get("completionRate", item.get("completedRate", 0)) or 0)
                    completion = completion_raw * 100 if completion_raw <= 1 else completion_raw
                    avg_pay_time = item.get("avgPayTime", 5)
                    try:
                        avg_pay_time = float(avg_pay_time)
                    except Exception:
                        avg_pay_time = 5.0
                    # OKX release time: paymentTimeoutMinutes (in minutes)
                    release_min = float(item.get("paymentTimeoutMinutes", avg_pay_time) or avg_pay_time)
                    order = P2POrder(
                        exchange="OKX",
                        order_id=item.get("id", ""),
                        side=side,
                        price=float(item.get("price", 0)),
                        min_amount=float(item.get("quoteMinAmountPerOrder", item.get("minAmount", 0)) or 0),
                        max_amount=float(item.get("quoteMaxAmountPerOrder", item.get("maxAmount", 0)) or 0),
                        available_amount=float(item.get("availableAmount", item.get("quantity", 0)) or 0),
                        completion_rate=completion,
                        # OKX uses "completedOrderQuantity" for trade count
                        total_orders=int(item.get("completedOrderQuantity", item.get("completedOrderCount", item.get("orderCount", 0))) or 0),
                        is_merchant=False,
                        payment_methods=pay_methods,
                        avg_release_time=int(release_min * 60),
                        # OKX API doesn't expose real-time online status — treat all as online
                        is_online=True,
                        nickname=item.get("nickName", item.get("realName", "Unknown")),
                    )
                    orders.append(order)
                except Exception as e:
                    logger.debug(f"OKX P2P parse error: {e}")

        logger.info(f"OKX P2P {side}: fetched {len(orders)} orders")
        _cache_set(cache_key, orders)
        return orders

    # ─────────────────────────── SPOT ───────────────────────────

    async def fetch_spot_price(self, exchange: str, symbol: str = "USDT") -> Optional[SpotPrice]:
        cache_key = f"spot_{exchange}_{symbol}"
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        price = None
        try:
            if exchange == "Binance":
                for url in [
                    "https://data-api.binance.vision/api/v3/ticker/24hr",
                    "https://api1.binance.com/api/v3/ticker/24hr",
                    "https://api2.binance.com/api/v3/ticker/24hr",
                    "https://api3.binance.com/api/v3/ticker/24hr",
                ]:
                    data = await self._get(url, params={"symbol": "BTCUSDT"}, retries=0)
                    if data and "bidPrice" in data:
                        price = SpotPrice(
                            exchange=exchange, symbol="BTC/USDT",
                            bid=float(data.get("bidPrice", 0)),
                            ask=float(data.get("askPrice", 0)),
                            volume_24h=float(data.get("volume", 0)),
                            price_change_pct=float(data.get("priceChangePercent", 0)),
                        )
                        break

            elif exchange == "Bybit":
                # Bybit frequently blocks server IPs; try multiple endpoints
                for url in [
                    "https://api.bybit.com/v5/market/tickers",
                    "https://api.bytick.com/v5/market/tickers",
                    "https://api2.bybit.com/v5/market/tickers",
                ]:
                    data = await self._get(url, params={"category": "spot", "symbol": "BTCUSDT"}, retries=0)
                    if data and "result" in data and data["result"].get("list"):
                        item = data["result"]["list"][0]
                        price = SpotPrice(
                            exchange=exchange, symbol="BTC/USDT",
                            bid=float(item.get("bid1Price", 0)),
                            ask=float(item.get("ask1Price", 0)),
                            volume_24h=float(item.get("volume24h", 0)),
                            price_change_pct=float(item.get("price24hPcnt", 0)) * 100,
                        )
                        break

            elif exchange == "OKX":
                url = "https://www.okx.com/api/v5/market/ticker"
                data = await self._get(url, params={"instId": "BTC-USDT"})
                if data and "data" in data and data["data"]:
                    item = data["data"][0]
                    price = SpotPrice(
                        exchange=exchange, symbol="BTC/USDT",
                        bid=float(item.get("bidPx", 0)),
                        ask=float(item.get("askPx", 0)),
                        volume_24h=float(item.get("vol24h", 0)),
                        price_change_pct=float(item.get("change24h", 0)) * 100,
                    )

        except Exception as e:
            logger.warning(f"Spot price fetch error {exchange}: {e}")

        if price:
            logger.debug(f"Spot {exchange} BTC: bid={price.bid:.2f} ask={price.ask:.2f}")
            _cache_set(cache_key, price)
        return price

    # ─────────────────────────── COMBINED ───────────────────────────

    async def fetch_all_p2p(self, side: str, amount: float, exchanges: list[str], banks: list[str]) -> list[P2POrder]:
        EXCHANGE_FETCHERS = {
            "Binance": self.fetch_binance_p2p,
            "Bybit":   self.fetch_bybit_p2p,
            "OKX":     self.fetch_okx_p2p,
        }
        EXCHANGE_TIMEOUT = 7.0  # max seconds per exchange

        async def _fetch_with_timeout(ex: str, fetcher) -> list[P2POrder]:
            try:
                return await asyncio.wait_for(fetcher(side, amount, banks), timeout=EXCHANGE_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning(f"{ex} P2P {side}: timed out after {EXCHANGE_TIMEOUT}s — skipped")
                return []
            except Exception as e:
                logger.warning(f"{ex} P2P {side}: exception — {e}")
                return []

        tasks = [
            _fetch_with_timeout(ex, fetcher)
            for ex, fetcher in EXCHANGE_FETCHERS.items()
            if ex in exchanges
        ]
        results = await asyncio.gather(*tasks)
        all_orders = []
        for r in results:
            all_orders.extend(r)

        # Нормалізуємо назви банків — прибираємо дублікати і незрозумілі коди
        for order in all_orders:
            normalized = []
            seen = set()
            for b in order.payment_methods:
                nb = _normalize_bank(b)
                if nb and nb not in seen:
                    normalized.append(nb)
                    seen.add(nb)
            order.payment_methods = normalized

        logger.info(
            f"fetch_all_p2p {side}: total {len(all_orders)} orders from "
            f"{[ex for ex in exchanges if ex in EXCHANGE_FETCHERS]}"
        )
        return all_orders
