import json
import os
from datetime import datetime
from models.types import ArbitrageOpportunity


ANALYTICS_FILE = "data/analytics.json"


def _load() -> dict:
    if os.path.exists(ANALYTICS_FILE):
        with open(ANALYTICS_FILE, "r") as f:
            try:
                return json.load(f)
            except Exception:
                pass
    return {
        "scans": 0,
        "total_opportunities": 0,
        "profits": [],
        "exchanges": {},
        "types": {},
        "best_profit": 0,
        "last_scan": None,
    }


def _save(data: dict):
    os.makedirs(os.path.dirname(ANALYTICS_FILE), exist_ok=True)
    with open(ANALYTICS_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)


def record_scan(opportunities: list[ArbitrageOpportunity]):
    data = _load()
    data["scans"] += 1
    data["last_scan"] = datetime.now().isoformat()
    data["total_opportunities"] += len(opportunities)

    for opp in opportunities:
        data["profits"].append(opp.profit_uah)
        if len(data["profits"]) > 1000:
            data["profits"] = data["profits"][-1000:]

        ex_key = f"{opp.buy_exchange}→{opp.sell_exchange}"
        data["exchanges"][ex_key] = data["exchanges"].get(ex_key, 0) + 1
        data["types"][opp.arb_type] = data["types"].get(opp.arb_type, 0) + 1
        if opp.profit_uah > data["best_profit"]:
            data["best_profit"] = opp.profit_uah

    _save(data)


def get_stats() -> dict:
    data = _load()
    profits = data["profits"]
    avg_profit = sum(profits) / len(profits) if profits else 0

    best_exchange = None
    if data["exchanges"]:
        best_exchange = max(data["exchanges"], key=data["exchanges"].get)

    best_type = None
    if data["types"]:
        best_type = max(data["types"], key=data["types"].get)

    return {
        "scans": data["scans"],
        "total_opportunities": data["total_opportunities"],
        "avg_profit": avg_profit,
        "best_profit": data["best_profit"],
        "best_exchange": best_exchange,
        "best_type": best_type,
        "last_scan": data["last_scan"],
    }


FAVORITES_FILE = "data/favorites.json"


def _load_favorites() -> list:
    if os.path.exists(FAVORITES_FILE):
        with open(FAVORITES_FILE, "r") as f:
            try:
                return json.load(f)
            except Exception:
                pass
    return []


def _save_favorites(data: list):
    os.makedirs(os.path.dirname(FAVORITES_FILE), exist_ok=True)
    with open(FAVORITES_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)


def save_favorite(opp: ArbitrageOpportunity):
    favs = _load_favorites()
    entry = {
        "saved_at": datetime.now().isoformat(),
        "type": opp.arb_type,
        "buy_exchange": opp.buy_exchange,
        "sell_exchange": opp.sell_exchange,
        "buy_price": opp.buy_price,
        "sell_price": opp.sell_price,
        "spread_pct": opp.spread_pct,
        "profit_uah": opp.profit_uah,
        "score": opp.score,
        "risk": opp.risk,
    }
    favs.insert(0, entry)
    if len(favs) > 50:
        favs = favs[:50]
    _save_favorites(favs)


def get_favorites() -> list:
    return _load_favorites()


PARTICIPANTS_FILE = "data/participants.json"


def _load_participants() -> dict:
    if os.path.exists(PARTICIPANTS_FILE):
        with open(PARTICIPANTS_FILE, "r") as f:
            try:
                return json.load(f)
            except Exception:
                pass
    return {}


def _save_participants(data: dict):
    os.makedirs(os.path.dirname(PARTICIPANTS_FILE), exist_ok=True)
    with open(PARTICIPANTS_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)


def get_participants(owner_id: int) -> list[dict]:
    data = _load_participants()
    return data.get(str(owner_id), [])


def add_participant(owner_id: int, participant_id: int, name: str) -> bool:
    data = _load_participants()
    key = str(owner_id)
    if key not in data:
        data[key] = []
    if any(p["user_id"] == participant_id for p in data[key]):
        return False
    data[key].append({
        "user_id": participant_id,
        "name": name,
        "added_at": datetime.now().isoformat(),
    })
    _save_participants(data)
    return True


def remove_participant(owner_id: int, participant_id: int) -> bool:
    data = _load_participants()
    key = str(owner_id)
    if key not in data:
        return False
    before = len(data[key])
    data[key] = [p for p in data[key] if p["user_id"] != participant_id]
    if len(data[key]) < before:
        _save_participants(data)
        return True
    return False
