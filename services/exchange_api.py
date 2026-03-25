import asyncio
import aiohttp
import logging
import os
import time
from typing import Optional
from models.types import P2POrder, SpotPrice

logger = logging.getLogger(__name__)

# Exchanges that block datacenter/cloud IPs — route through proxy when PROXY_URL is set
_BLOCKED_EXCHANGE_DOMAINS = {
    "mexc.com", "otc.mexc.com",
    "gate.io", "gateio.ws",
    "htx.com",
    "bitget.com",
    "kucoin.com",
}

def _needs_proxy(url: str) -> bool:
    """Return True if this URL belongs to an exchange that blocks cloud IPs."""
    proxy_url = os.getenv("PROXY_URL", "").strip()
    if not proxy_url:
        return False
    return any(domain in url for domain in _BLOCKED_EXCHANGE_DOMAINS)

CACHE = {}
CACHE_TTL = 8


def _cache_get(key: str):
    entry = CACHE.get(key)
    if entry and time.time() - entry["ts"] < CACHE_TTL:
        return entry["data"]
    return None


def _cache_set(key: str, data):
    CACHE[key] = {"data": data, "ts": time.time()}


def _banks_key(banks: list[str]) -> str:
    return "_".join(sorted(b.lower() for b in banks)) if banks else "all"


def _bank_match(pay_methods: list[str], banks: list[str]) -> bool:
    if not banks:
        return True
    pay_lower = [m.lower() for m in pay_methods]
    for bank in banks:
        b = bank.lower()
        if any(b in p or p in b for p in pay_lower):
            return True
    return False


NETWORK_FEES = {
    "TRC20": 1.0,
    "ERC20": 5.0,
    "BEP20": 0.5,
    "SOL": 0.1,
    "APT": 0.5,
}

SPOT_TRADING_FEE = {
    "Binance": 0.001,
    "Bybit": 0.001,
    "OKX": 0.001,
    "Bitget": 0.001,
    "MEXC": 0.002,
    "KuCoin": 0.001,
    "Gate.io": 0.002,
    "HTX": 0.002,
    "Crypto.com": 0.0025,
}

WITHDRAWAL_FEES_USDT = {
    "Binance": {"TRC20": 1.0,  "ERC20": 4.5,  "BEP20": 0.8, "SOL": 0.1, "APT": 0.8},
    "Bybit":   {"TRC20": 1.0,  "ERC20": 5.0,  "BEP20": 0.8, "SOL": 0.1, "APT": 1.0},
    "OKX":     {"TRC20": 1.5,  "ERC20": 5.5,  "BEP20": 0.8, "SOL": 0.1, "APT": 0.5},
    "Bitget":  {"TRC20": 1.0,  "ERC20": 5.0,  "BEP20": 0.5, "SOL": 0.1, "APT": 0.5},
    "MEXC":    {"TRC20": 3.0,  "ERC20": 8.0,  "BEP20": 1.0, "SOL": 0.1, "APT": 1.0},
    "Gate.io": {"TRC20": 1.0,  "ERC20": 4.0,  "BEP20": 0.5, "SOL": 0.1, "APT": 0.5},
    "HTX":     {"TRC20": 1.0,  "ERC20": 4.5,  "BEP20": 0.8, "SOL": 0.1, "APT": 1.0},
    "KuCoin":  {"TRC20": 0.8,  "ERC20": 4.0,  "BEP20": 0.5, "SOL": 0.1, "APT": 0.5},
}

P2P_BANKS = {
    "Binance": ["PrivatBank", "Monobank", "PUMB", "A-Bank", "Oschadbank", "Raiffeisen"],
    "Bybit":   ["PrivatBank", "Monobank", "PUMB", "A-Bank"],
    "OKX":     ["PrivatBank", "Monobank", "PUMB"],
    "Bitget":  ["PrivatBank", "Monobank", "A-Bank"],
    "MEXC":    ["PrivatBank", "Monobank"],
    "Gate.io": ["PrivatBank", "Monobank", "PUMB"],
    "HTX":     ["PrivatBank", "Monobank"],
    "KuCoin":  ["PrivatBank", "Monobank"],
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
    "Bank Transfer":          None,
    "Monobankiban":           "Monobank",
    "alliancecard":           "Alliance Card",
    "bank":                   None,  # занадто загальна назва — пропускаємо
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
        proxy = os.getenv("PROXY_URL", "").strip() if _needs_proxy(url) else None
        if proxy:
            logger.debug(f"Using proxy for {url}")
        try:
            async with session.get(url, params=params, headers=h, proxy=proxy or None) as resp:
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
        proxy = os.getenv("PROXY_URL", "").strip() if _needs_proxy(url) else None
        if proxy:
            logger.debug(f"Using proxy for POST {url}")
        try:
            async with session.post(url, json=json_data, headers=h, proxy=proxy or None) as resp:
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

    # ─────────────────────────── BITGET ───────────────────────────

    async def fetch_bitget_p2p(self, side: str, amount: float, banks: list[str]) -> list[P2POrder]:
        cache_key = f"bitget_p2p_{side}_{amount}_{_banks_key(banks)}"
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        # Bitget changed their C2C API. Side: BUY=0 means user wants to buy USDT (sellers listed)
        side_map = {"BUY": "0", "SELL": "1"}
        payload = {
            "tokenId": "USDT",
            "currencyId": "UAH",
            "side": side_map.get(side, "0"),
            "size": 50,
            "page": 1,
        }
        if amount:
            payload["amount"] = str(int(amount))

        headers = {
            "Content-Type": "application/json",
            "Referer": "https://www.bitget.com/p2p/",
            "Origin": "https://www.bitget.com",
        }

        data = None
        for url in [
            "https://www.bitget.com/v1/p2p/pub/ad/list",
            "https://api.bitget.com/api/v2/p2p/adv/listPublic",
            "https://www.bitget.com/api/v2/p2p/adv/list",
            "https://api.bitget.com/api/v2/c2c/adv/list",
        ]:
            data = await self._post(url, json_data=payload, headers=headers)
            if data and ("data" in data or "result" in data):
                break

        orders = []
        items = []
        if data:
            d = data.get("data", data.get("result", None))
            if isinstance(d, list):
                items = d
            elif isinstance(d, dict):
                items = d.get("items", d.get("data", d.get("list", d.get("ads", []))))

        for item in items:
            try:
                pay_methods = []
                raw_pay = item.get("payments", item.get("tradeMethodList", item.get("paymentList", [])))
                if isinstance(raw_pay, list):
                    for p in raw_pay:
                        m = p.get("paymentType", p.get("payId", p.get("name", ""))) if isinstance(p, dict) else str(p)
                        if m:
                            pay_methods.append(str(m))
                if not _bank_match(pay_methods, banks):
                    continue
                completion_raw = float(item.get("completionRate", item.get("orderCompletionRate", item.get("finishRate", 0))) or 0)
                completion = completion_raw * 100 if completion_raw <= 1 else completion_raw
                avg_time_raw = float(item.get("avgReleaseTime", item.get("avgPayTime", 5)) or 5)
                avg_time = int(avg_time_raw * 60) if avg_time_raw < 200 else int(avg_time_raw)
                order = P2POrder(
                    exchange="Bitget",
                    order_id=str(item.get("id", item.get("advOrderId", item.get("adId", "")))),
                    side=side,
                    price=float(item.get("price", 0)),
                    min_amount=float(item.get("minAmount", item.get("minOrderAmount", item.get("minSingleAmount", 0))) or 0),
                    max_amount=float(item.get("maxAmount", item.get("maxOrderAmount", item.get("maxSingleAmount", 0))) or 0),
                    available_amount=float(item.get("quantity", item.get("amount", item.get("surplus", 0))) or 0),
                    completion_rate=completion,
                    total_orders=int(item.get("recentOrderNum", item.get("completedOrderNumber", item.get("orderCount", 0))) or 0),
                    is_merchant=bool(item.get("authTag") == "merchant" or item.get("isMerchant", False)),
                    payment_methods=pay_methods,
                    avg_release_time=avg_time,
                    is_online=bool(item.get("isOnline", item.get("online", False))),
                    nickname=item.get("nickName", item.get("name", item.get("userName", "Unknown"))),
                )
                orders.append(order)
            except Exception as e:
                logger.debug(f"Bitget P2P parse error: {e}")

        logger.info(f"Bitget P2P {side}: fetched {len(orders)} orders")
        _cache_set(cache_key, orders)
        return orders

    # ─────────────────────────── MEXC ───────────────────────────

    async def fetch_mexc_p2p(self, side: str, amount: float, banks: list[str]) -> list[P2POrder]:
        cache_key = f"mexc_p2p_{side}_{amount}"
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        side_map = {"BUY": "1", "SELL": "2"}
        headers = {
            "Content-Type": "application/json",
            "Referer": "https://www.mexc.com/otc",
            "Origin": "https://www.mexc.com",
        }

        data = None
        # Try POST endpoints first
        post_payload = {
            "coinName": "USDT",
            "currency": "UAH",
            "tradeType": side_map.get(side, "1"),
            "page": 1,
            "pageSize": 50,
        }
        if amount:
            post_payload["amount"] = str(int(amount))

        for url in [
            "https://www.mexc.com/api/kyc/otc/adv/list",
            "https://www.mexc.com/api/otc/adv/list",
            "https://www.mexc.com/api/p2p/c2c/adv/list",
        ]:
            data = await self._post(url, json_data=post_payload, headers=headers)
            if data and ("data" in data or "result" in data):
                break

        # Fallback: GET endpoints
        if not data or ("data" not in data and "result" not in data):
            get_params = {
                "coinName": "USDT",
                "currency": "UAH",
                "tradeType": side_map.get(side, "1"),
                "page": 1,
                "pageSize": 50,
            }
            for url in [
                "https://www.mexc.com/api/otc/adv/list",
                "https://otc.mexc.com/api/otc/item/list",
                "https://www.mexc.com/api/fiat/otc/ad/list",
            ]:
                data = await self._get(url, params=get_params, headers=headers)
                if data and ("data" in data or "result" in data):
                    break

        orders = []
        items = []
        if data:
            d = data.get("data", data.get("result", None))
            if isinstance(d, list):
                items = d
            elif isinstance(d, dict):
                items = d.get("list", d.get("items", d.get("data", d.get("records", []))))

        for item in items:
            try:
                pay_methods = []
                raw_pay = item.get("payWay", item.get("payTypes", item.get("payments", item.get("paymentMethods", ""))))
                if isinstance(raw_pay, str):
                    pay_methods = [p.strip() for p in raw_pay.split(",") if p.strip()]
                elif isinstance(raw_pay, list):
                    for p in raw_pay:
                        pay_methods.append(p.get("name", p.get("paymentType", str(p))) if isinstance(p, dict) else str(p))
                if not _bank_match(pay_methods, banks):
                    continue
                completion_raw = float(item.get("completionRate", item.get("finishRate", item.get("successRate", 0))) or 0)
                completion = completion_raw * 100 if completion_raw <= 1 else completion_raw
                avg_pay = float(item.get("avgPayTime", item.get("avgReleaseTime", 5)) or 5)
                avg_time = int(avg_pay * 60) if avg_pay < 200 else int(avg_pay)
                order = P2POrder(
                    exchange="MEXC",
                    order_id=str(item.get("id", item.get("advId", item.get("orderId", "")))),
                    side=side,
                    price=float(item.get("price", 0)),
                    min_amount=float(item.get("minLimit", item.get("minAmount", item.get("minSingleAmount", 0))) or 0),
                    max_amount=float(item.get("maxLimit", item.get("maxAmount", item.get("maxSingleAmount", 0))) or 0),
                    available_amount=float(item.get("tradableQuantity", item.get("quantity", item.get("amount", 0))) or 0),
                    completion_rate=completion,
                    total_orders=int(item.get("orderCount", item.get("finishCount", item.get("totalOrders", 0))) or 0),
                    is_merchant=bool(item.get("certified", item.get("isMerchant", False))),
                    payment_methods=pay_methods,
                    avg_release_time=avg_time,
                    is_online=bool(item.get("isOnline", item.get("online", False))),
                    nickname=item.get("nickName", item.get("name", item.get("userName", "Unknown"))),
                )
                orders.append(order)
            except Exception as e:
                logger.debug(f"MEXC P2P parse error: {e}")

        logger.info(f"MEXC P2P {side}: fetched {len(orders)} orders")
        _cache_set(cache_key, orders)
        return orders

    # ─────────────────────────── GATE.IO ───────────────────────────

    async def fetch_gate_p2p(self, side: str, amount: float, banks: list[str]) -> list[P2POrder]:
        cache_key = f"gate_p2p_{side}_{amount}_{_banks_key(banks)}"
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        # Gate.io C2C - BUY=user buys USDT, SELL=user sells USDT
        side_map = {"BUY": "sell", "SELL": "buy"}
        headers = {
            "Content-Type": "application/json",
            "Referer": "https://www.gate.io/p2p/",
            "Origin": "https://www.gate.io",
        }

        data = None
        payload = {
            "currency": "UAH",
            "coin": "USDT",
            "type": side_map.get(side, "sell"),
            "page": 1,
            "limit": 50,
        }
        if amount:
            payload["amount"] = str(int(amount))

        for url in [
            "https://www.gate.io/api/c2c/v1/ad/list",
            "https://www.gate.io/api/fiat/c2c/adv/list",
            "https://api.gateio.ws/api/v4/c2c/ads",
        ]:
            data = await self._post(url, json_data=payload, headers=headers)
            if data and ("data" in data or "result" in data or isinstance(data, list)):
                break

        orders = []
        items = []
        if data:
            if isinstance(data, list):
                items = data
            else:
                d = data.get("data", data.get("result", data.get("list", [])))
                if isinstance(d, list):
                    items = d
                elif isinstance(d, dict):
                    items = d.get("list", d.get("items", d.get("ads", [])))

        for item in items:
            try:
                pay_methods = []
                raw_pay = item.get("payTypes", item.get("paymentMethods", item.get("payments", [])))
                if isinstance(raw_pay, list):
                    for p in raw_pay:
                        pay_methods.append(p.get("name", p.get("paymentType", str(p))) if isinstance(p, dict) else str(p))
                elif isinstance(raw_pay, str) and raw_pay:
                    pay_methods = [raw_pay]
                if not _bank_match(pay_methods, banks):
                    continue
                completion_raw = float(item.get("completionRate", item.get("finishRate", 0)) or 0)
                completion = completion_raw * 100 if completion_raw <= 1 else completion_raw
                avg_pay = float(item.get("avgReleaseTime", item.get("avgPayTime", 5)) or 5)
                avg_time = int(avg_pay * 60) if avg_pay < 200 else int(avg_pay)
                order = P2POrder(
                    exchange="Gate.io",
                    order_id=str(item.get("id", item.get("adId", ""))),
                    side=side,
                    price=float(item.get("price", 0)),
                    min_amount=float(item.get("minAmount", item.get("minSingleAmount", 0)) or 0),
                    max_amount=float(item.get("maxAmount", item.get("maxSingleAmount", 0)) or 0),
                    available_amount=float(item.get("quantity", item.get("amount", item.get("available", 0))) or 0),
                    completion_rate=completion,
                    total_orders=int(item.get("orderCount", item.get("totalOrders", item.get("completedOrders", 0))) or 0),
                    is_merchant=bool(item.get("isMerchant", item.get("certified", False))),
                    payment_methods=pay_methods,
                    avg_release_time=avg_time,
                    is_online=bool(item.get("isOnline", item.get("online", False))),
                    nickname=item.get("nickName", item.get("name", item.get("userName", "Unknown"))),
                )
                orders.append(order)
            except Exception as e:
                logger.debug(f"Gate.io P2P parse error: {e}")

        logger.info(f"Gate.io P2P {side}: fetched {len(orders)} orders")
        _cache_set(cache_key, orders)
        return orders

    # ─────────────────────────── HTX (Huobi) ───────────────────────────

    async def fetch_htx_p2p(self, side: str, amount: float, banks: list[str]) -> list[P2POrder]:
        cache_key = f"htx_p2p_{side}_{amount}_{_banks_key(banks)}"
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        # HTX/Huobi P2P: tradeType 1=user buys USDT (SELL ads shown), 2=user sells USDT (BUY ads)
        side_map = {"BUY": "1", "SELL": "2"}
        payload = {
            "lang": "en",
            "coinId": "2",          # 2 = USDT
            "currency": "UAH",
            "tradeType": side_map.get(side, "1"),
            "paymentIds": "",
            "orderAmount": str(int(amount)) if amount else "",
            "page": "1",
            "pageSize": "50",
        }
        headers = {
            "Content-Type": "application/json",
            "Referer": "https://www.htx.com/p2p/",
            "Origin": "https://www.htx.com",
        }

        data = None
        for url in [
            "https://www.htx.com/p2p/api/public/advert/list",
            "https://api.htx.com/p2p/api/public/advert/list",
        ]:
            data = await self._post(url, json_data=payload, headers=headers)
            if data and ("data" in data or "result" in data):
                break

        orders = []
        items = []
        if data:
            d = data.get("data", data.get("result", None))
            if isinstance(d, list):
                items = d
            elif isinstance(d, dict):
                items = d.get("list", d.get("items", d.get("ads", d.get("advertList", []))))

        for item in items:
            try:
                pay_methods = []
                raw_pay = item.get("payMethods", item.get("paymentMethods", item.get("payments", [])))
                if isinstance(raw_pay, list):
                    for p in raw_pay:
                        name = p.get("name", p.get("paymentType", p.get("paymentMethod", ""))) if isinstance(p, dict) else str(p)
                        if name:
                            pay_methods.append(name)
                elif isinstance(raw_pay, str) and raw_pay:
                    pay_methods = [raw_pay]
                if not _bank_match(pay_methods, banks):
                    continue
                completion_raw = float(item.get("completionRate", item.get("finishRate", item.get("successRate", 0))) or 0)
                completion = completion_raw * 100 if completion_raw <= 1 else completion_raw
                avg_pay = float(item.get("avgReleaseTime", item.get("avgPayTime", 5)) or 5)
                avg_time = int(avg_pay * 60) if avg_pay < 200 else int(avg_pay)
                last_seen = int(item.get("lastLoginTime", 0) or 0)
                is_online = last_seen < 300 if last_seen > 0 else bool(item.get("isOnline", item.get("online", False)))
                order = P2POrder(
                    exchange="HTX",
                    order_id=str(item.get("id", item.get("advertId", item.get("orderId", "")))),
                    side=side,
                    price=float(item.get("price", 0)),
                    min_amount=float(item.get("minLimit", item.get("minAmount", item.get("minOrderAmount", 0))) or 0),
                    max_amount=float(item.get("maxLimit", item.get("maxAmount", item.get("maxOrderAmount", 0))) or 0),
                    available_amount=float(item.get("quantity", item.get("amount", item.get("surplus", 0))) or 0),
                    completion_rate=completion,
                    total_orders=int(item.get("orderCount", item.get("totalOrders", item.get("finishCount", 0))) or 0),
                    is_merchant=bool(item.get("isMerchant", item.get("certified", False))),
                    payment_methods=pay_methods,
                    avg_release_time=avg_time,
                    is_online=is_online,
                    nickname=item.get("nickName", item.get("name", item.get("userName", "Unknown"))),
                )
                orders.append(order)
            except Exception as e:
                logger.debug(f"HTX P2P parse error: {e}")

        logger.info(f"HTX P2P {side}: fetched {len(orders)} orders")
        _cache_set(cache_key, orders)
        return orders

    # ─────────────────────────── KuCoin ───────────────────────────

    async def fetch_kucoin_p2p(self, side: str, amount: float, banks: list[str]) -> list[P2POrder]:
        cache_key = f"kucoin_p2p_{side}_{amount}_{_banks_key(banks)}"
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        # KuCoin: side "SELL" = ad sellers are selling USDT (user BUYs), "BUY" = buyers want to buy (user SELLs)
        side_map = {"BUY": "SELL", "SELL": "BUY"}
        params = {
            "currency": "UAH",
            "coin": "USDT",
            "side": side_map.get(side, "SELL"),
            "page": 1,
            "pageSize": 50,
            "lang": "en_US",
        }
        if amount:
            params["amount"] = str(int(amount))

        headers = {
            "Content-Type": "application/json",
            "Referer": "https://www.kucoin.com/otc",
            "Origin": "https://www.kucoin.com",
            "Accept": "application/json",
        }

        data = None
        for url in [
            "https://www.kucoin.com/_api/otc/ad/list",
            "https://www.kucoin.com/api/v1/otc/ad/list",
        ]:
            data = await self._get(url, params=params, headers=headers)
            if data and ("data" in data or "items" in data):
                break

        orders = []
        items = []
        if data:
            d = data.get("data", data.get("items", data.get("result", None)))
            if isinstance(d, list):
                items = d
            elif isinstance(d, dict):
                items = d.get("items", d.get("list", d.get("ads", d.get("data", []))))

        for item in items:
            try:
                pay_methods = []
                raw_pay = item.get("payTypes", item.get("payments", item.get("paymentMethods", [])))
                if isinstance(raw_pay, list):
                    for p in raw_pay:
                        name = p.get("name", p.get("paymentType", "")) if isinstance(p, dict) else str(p)
                        if name:
                            pay_methods.append(name)
                elif isinstance(raw_pay, str) and raw_pay:
                    pay_methods = [raw_pay]
                if not _bank_match(pay_methods, banks):
                    continue
                completion_raw = float(item.get("completionRate", item.get("finishRate", 0)) or 0)
                completion = completion_raw * 100 if completion_raw <= 1 else completion_raw
                avg_pay = float(item.get("avgPayTime", item.get("avgReleaseTime", 5)) or 5)
                avg_time = int(avg_pay * 60) if avg_pay < 200 else int(avg_pay)
                order = P2POrder(
                    exchange="KuCoin",
                    order_id=str(item.get("id", item.get("adId", ""))),
                    side=side,
                    price=float(item.get("floatRatio", item.get("price", 0))),
                    min_amount=float(item.get("minOrderAmt", item.get("minAmount", 0)) or 0),
                    max_amount=float(item.get("maxOrderAmt", item.get("maxAmount", 0)) or 0),
                    available_amount=float(item.get("currencyBalance", item.get("quantity", 0)) or 0),
                    completion_rate=completion,
                    total_orders=int(item.get("orderCount", item.get("completedOrders", item.get("totalOrders", 0))) or 0),
                    is_merchant=bool(item.get("isMerchant", False)),
                    payment_methods=pay_methods,
                    avg_release_time=avg_time,
                    is_online=bool(item.get("isOnline", item.get("online", False))),
                    nickname=item.get("nickName", item.get("name", item.get("uid", "Unknown"))),
                )
                if order.price > 0:
                    orders.append(order)
            except Exception as e:
                logger.debug(f"KuCoin P2P parse error: {e}")

        logger.info(f"KuCoin P2P {side}: fetched {len(orders)} orders")
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

            elif exchange == "KuCoin":
                url = "https://api.kucoin.com/api/v1/market/stats"
                data = await self._get(url, params={"symbol": "BTC-USDT"})
                if data and "data" in data:
                    d = data["data"]
                    price = SpotPrice(
                        exchange=exchange, symbol="BTC/USDT",
                        bid=float(d.get("buy", 0)),
                        ask=float(d.get("sell", 0)),
                        volume_24h=float(d.get("vol", 0)),
                        price_change_pct=float(d.get("changeRate", 0)) * 100,
                    )

            elif exchange == "Gate.io":
                url = "https://api.gateio.ws/api/v4/spot/tickers"
                data = await self._get(url, params={"currency_pair": "BTC_USDT"})
                if data and isinstance(data, list) and data:
                    item = data[0]
                    price = SpotPrice(
                        exchange=exchange, symbol="BTC/USDT",
                        bid=float(item.get("highest_bid", 0)),
                        ask=float(item.get("lowest_ask", 0)),
                        volume_24h=float(item.get("base_volume", 0)),
                        price_change_pct=float(item.get("change_percentage", 0)),
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
            "Bitget":  self.fetch_bitget_p2p,
            "MEXC":    self.fetch_mexc_p2p,
            "Gate.io": self.fetch_gate_p2p,
            "HTX":     self.fetch_htx_p2p,
            "KuCoin":  self.fetch_kucoin_p2p,
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
