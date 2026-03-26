# Deployed: 2026-03-26T05:51:25.879Z — banks submenu
import asyncio
import logging
import os
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

import shared
from services.exchange_api import ExchangeAPI
from services.arbitrage_engine import ArbitrageEngine
from handlers.main_handler import router

load_dotenv()

if os.getenv("BOT_DISABLED", "").lower() in ("true", "1", "yes"):
    print("BOT_DISABLED=true — бот вимкнено на цьому середовищі (запущений на Railway)")
    exit(0)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not set in environment variables!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
dp.include_router(router)

api = ExchangeAPI()
arb_engine = ArbitrageEngine(api)

shared.bot_instance = bot
shared.arb_engine = arb_engine


async def health_handler(request):
    return web.Response(text="OK — Crypto Arbitrage Bot is running 🚀")


async def start_health_server():
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()

    # Railway інжектує PORT автоматично; BOT_HEALTH_PORT — ручне перевизначення
    railway_port = os.getenv("PORT")
    override = os.getenv("BOT_HEALTH_PORT")
    if override:
        candidate_ports = [int(override), 8082, 8083, 8084, 8085]
    elif railway_port:
        candidate_ports = [int(railway_port), 8082, 8083, 8084, 8085]
    else:
        candidate_ports = [8082, 8083, 8084, 8085, 9000, 9001]

    for port in candidate_ports:
        try:
            site = web.TCPSite(runner, "0.0.0.0", port)
            await site.start()
            logger.info(f"Health server running on port {port}")
            return
        except OSError:
            logger.warning(f"Port {port} busy, trying next...")

    logger.warning("Health server skipped — no free ports found (bot still works normally)")


async def on_startup():
    logger.info("Bot started!")
    logger.info("Arbitrage engine initialized")


async def on_shutdown():
    logger.info("Bot shutting down...")
    await api.close()


async def main():
    await start_health_server()
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    logger.info("Starting polling...")
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
