"""
Persistent storage for user settings — saves/loads JSON from disk.
Railway volumes are ephemeral on redeploy but survive restarts/crashes.
"""
import json
import logging
import os
from dataclasses import asdict
from models.types import UserSettings

logger = logging.getLogger(__name__)

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
_SETTINGS_FILE = os.path.join(_DATA_DIR, "user_settings.json")

_ALL_DEFAULTS = {
    "amount_uah": 20000.0,
    "min_profit_uah": 50.0,
    "risk_level": "MEDIUM",
    "exchanges": ["Binance", "Bybit", "OKX", "Bitget", "MEXC", "Gate.io", "HTX", "KuCoin"],
    "banks": ["PrivatBank", "Monobank", "PUMB", "A-Bank"],
    "network": "TRC20",
    "scan_interval": 30,
    "notifications_enabled": True,
    "trading_mode": "direct",
    "min_completion_rate": 90.0,
    "bank_fee_uah": 0.0,
    "arb_types": ["p2p_same", "cross_exchange", "triangular"],
}


def _ensure_dir():
    os.makedirs(_DATA_DIR, exist_ok=True)


def _raw_to_settings(raw: dict) -> UserSettings:
    """Convert raw dict → UserSettings, applying defaults for missing keys."""
    merged = {**_ALL_DEFAULTS, **raw}
    return UserSettings(
        amount_uah=float(merged["amount_uah"]),
        min_profit_uah=float(merged["min_profit_uah"]),
        risk_level=str(merged["risk_level"]),
        exchanges=list(merged["exchanges"]),
        banks=list(merged["banks"]),
        network=str(merged["network"]),
        scan_interval=int(merged["scan_interval"]),
        notifications_enabled=bool(merged["notifications_enabled"]),
        trading_mode=str(merged["trading_mode"]),
        min_completion_rate=float(merged["min_completion_rate"]),
        bank_fee_uah=float(merged["bank_fee_uah"]),
        arb_types=list(merged.get("arb_types", ["p2p_same", "cross_exchange", "triangular"])),
    )


def load_all() -> dict[int, UserSettings]:
    """Load all user settings from disk. Returns empty dict on first run."""
    _ensure_dir()
    if not os.path.exists(_SETTINGS_FILE):
        return {}
    try:
        with open(_SETTINGS_FILE, "r", encoding="utf-8") as f:
            raw_all: dict[str, dict] = json.load(f)
        result = {}
        for uid_str, raw in raw_all.items():
            try:
                result[int(uid_str)] = _raw_to_settings(raw)
            except Exception as e:
                logger.warning(f"Could not load settings for user {uid_str}: {e}")
        logger.info(f"Settings loaded for {len(result)} users from disk")
        return result
    except Exception as e:
        logger.error(f"Failed to load settings file: {e}")
        return {}


def save_all(user_settings: dict[int, UserSettings]):
    """Save all users' settings to disk atomically."""
    _ensure_dir()
    raw_all = {}
    for uid, s in user_settings.items():
        try:
            raw_all[str(uid)] = asdict(s)
        except Exception as e:
            logger.warning(f"Could not serialize settings for user {uid}: {e}")
    tmp = _SETTINGS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(raw_all, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _SETTINGS_FILE)
    except Exception as e:
        logger.error(f"Failed to save settings: {e}")
