from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


class ArbitrageType(str, Enum):
    P2P_TO_P2P = "P2P › P2P"
    CROSS_EXCHANGE = "Cross-Exchange"
    P2P_SPOT_P2P = "P2P › Spot › P2P"
    TRIANGULAR = "Triangular"


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class SpeedType(str, Enum):
    FAST = "⚡ Fast"
    MEDIUM = "🟡 Medium"
    SLOW = "🐢 Slow"


class ExchangeName(str, Enum):
    BINANCE = "Binance"
    BYBIT = "Bybit"
    OKX = "OKX"
    BITGET = "Bitget"
    MEXC = "MEXC"
    GATE = "Gate.io"
    HTX = "HTX"
    KUCOIN = "KuCoin"


@dataclass
class P2POrder:
    exchange: str
    order_id: str
    side: str
    price: float
    min_amount: float
    max_amount: float
    available_amount: float
    completion_rate: float
    total_orders: int
    is_merchant: bool
    payment_methods: list[str]
    avg_release_time: int
    is_online: bool
    nickname: str
    asset: str = "USDT"
    currency: str = "UAH"


@dataclass
class SpotPrice:
    exchange: str
    symbol: str
    bid: float
    ask: float
    volume_24h: float
    price_change_pct: float


@dataclass
class ArbitrageOpportunity:
    arb_type: ArbitrageType
    buy_exchange: str
    sell_exchange: str
    buy_price: float
    sell_price: float
    spread: float
    spread_pct: float
    profit_uah: float
    profit_pct: float
    amount_usdt: float
    buy_order: Optional[P2POrder]
    sell_order: Optional[P2POrder]
    payment_method: str
    execution_ease: str
    speed: SpeedType
    liquidity_ok: bool
    seller_completion_rate: float
    seller_total_orders: int
    risk: RiskLevel
    score: float
    trade_steps: list[str]
    fees_breakdown: dict
    volatility_ok: bool = True
    network: str = "TRC20"
    sell_payment_method: str = ""
    scanned_at: float = 0.0
    verified: bool = False
    verified_at: float = 0.0
    verified_buy_price: float = 0.0
    verified_sell_price: float = 0.0

    @property
    def is_viable(self) -> bool:
        return (
            self.profit_uah > 0
            and self.liquidity_ok
            and self.volatility_ok
        )


@dataclass
class UserSettings:
    amount_uah: float = 20000.0
    min_profit_uah: float = 50.0
    risk_level: str = "MEDIUM"
    exchanges: list[str] = field(default_factory=lambda: ["Binance", "Bybit", "OKX", "Bitget", "MEXC", "Gate.io", "HTX", "KuCoin"])
    buy_banks: list[str] = field(default_factory=lambda: ["PrivatBank", "Monobank"])
    sell_banks: list[str] = field(default_factory=lambda: ["PrivatBank", "Monobank"])
    network: str = "TRC20"
    scan_interval: int = 30
    notifications_enabled: bool = True
    trading_mode: str = "direct"
    min_completion_rate: float = 90.0
    bank_fee_uah: float = 0.0
    arb_types: list[str] = field(default_factory=lambda: ["p2p_same", "cross_exchange", "triangular"])
    main_msg_id: Optional[int] = None
