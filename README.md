# Crypto Arbitrage Telegram Bot

Advanced Telegram bot for crypto arbitrage (P2P + Spot) built with Python and aiogram 3.x.

## Features

- **4 Arbitrage Types:**
  - P2P → P2P (same exchange)
  - Cross-Exchange (buy on Binance, sell on Bybit)
  - P2P → Spot → P2P
  - Triangular (USDT → BTC → USDT)

- **5 Exchanges:** Binance, Bybit, OKX, Bitget, MEXC

- **Real Profit Calculation:**
  - Trading fees
  - Withdrawal fees
  - Network fees (TRC20/ERC20)
  - Slippage estimation

- **Anti-Scam System:**
  - Completion rate ≥ 95%
  - Total orders ≥ 100
  - Price anomaly detection
  - Online status check

- **Opportunity Score (0-100):**
  - Spread × 30
  - Speed × 25
  - Liquidity × 20
  - Seller trust × 15
  - Volatility × 10

- **Live Mode:** Auto-scan at configurable intervals with alerts

- **Analytics:** Track profits, best exchanges, scan history

- **Favorites:** Save and review best opportunities

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and set your bot token:
```
BOT_TOKEN=your_telegram_bot_token_here
DEFAULT_AMOUNT=20000
SCAN_INTERVAL=30
```

Get your bot token from [@BotFather](https://t.me/BotFather) on Telegram.

### 3. Run

```bash
python bot.py
```

## Project Structure

```
crypto-arb-bot/
├── bot.py                  # Entry point, Bot + Dispatcher setup
├── requirements.txt
├── .env.example
├── models/
│   └── types.py            # Data models (P2POrder, ArbitrageOpportunity, etc.)
├── services/
│   ├── exchange_api.py     # Async API clients for all 5 exchanges
│   ├── arbitrage_engine.py # Core arbitrage matching + profit calculation
│   └── analytics.py       # Scan history, favorites, stats
├── handlers/
│   ├── main_handler.py     # Telegram command/callback handlers
│   ├── keyboards.py        # All inline keyboards
│   └── states.py           # FSM states
├── utils/
│   └── formatters.py       # Message formatting
└── data/                   # Runtime data (analytics.json, favorites.json)
```

## Telegram Commands

- `/start` — Show main menu
- `/menu` — Return to main menu

## Main Menu

| Button | Action |
|---|---|
| 🔍 Scan | One-time market scan |
| ⚡ Live | Auto-scan mode |
| 💰 Amount | Set trading amount (UAH) |
| ⚙️ Settings | Configure all parameters |
| ⭐ Favorites | View saved opportunities |
| 📊 Analytics | Scan statistics |

## Settings

- **Amount** — Trading capital in UAH
- **Min Profit** — Minimum profit threshold in UAH
- **Risk Level** — LOW / MEDIUM / HIGH
- **Network** — TRC20 / ERC20 / BEP20 / SOL
- **Banks** — PrivatBank, Monobank, PUMB, etc.
- **Exchanges** — Select which exchanges to scan
- **Interval** — Live mode scan interval (10–300 sec)

## Risk Levels

- **LOW** — Only top-scored opportunities (score ≥ 70, speed: Fast, completion ≥ 97%)
- **MEDIUM** — Good opportunities (score ≥ 50, completion ≥ 95%)
- **HIGH** — All viable opportunities

## Notes

- P2P data is fetched from public APIs (no auth required)
- Results are cached for 8 seconds to reduce API load
- Anti-scam filters run on every order before matching
- Cross-exchange arbitrage accounts for withdrawal fees
- Live mode sends alerts only when opportunities are found
