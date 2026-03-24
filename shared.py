from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiogram import Bot
    from services.arbitrage_engine import ArbitrageEngine

bot_instance: "Bot | None" = None
arb_engine: "ArbitrageEngine | None" = None
