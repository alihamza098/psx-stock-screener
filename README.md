# PSX Stock Screener 📊

Live Pakistan Stock Exchange screener with real-time data from [dps.psx.com.pk](https://dps.psx.com.pk).

## Features
- 🔴 **Live Data** — 730+ stocks fetched directly from PSX Data Portal
- 🔍 Search by symbol or company name
- 📊 Filter by sector, index (KSE-100/30, KMI-30), P/E, dividend yield, market cap
- 🏆 Investment scoring system (0-100)
- 📈 Table, Card, and Scorecard views
- ⭐ Watchlist with local storage
- 📥 Export to CSV
- 🔄 Refresh button for latest data

## Run Locally
```bash
python3 server.py
```
Open http://localhost:3000

## Configuration (environment variables)
| Variable | Purpose |
|---|---|
| `ADMIN_SECRET` | Enables the admin panel (`/admin`) and Strategy Lab runs. Admin is disabled when unset. |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Telegram alerts. |
| `PSX_DATA_DIR` | Where recorded ticks / session ranges are stored (default `cache/`). |

Trading costs (KTrade commission, SST, SECP levy, CGT, slippage) and PSX rules (circuit limits,
T+2, short-eligible symbols) live in `config/costs.json` — check them against your contract notes.

## Strategy Lab (`/research`)
Backtests every strategy on PSX daily history with real costs and PSX rules, judges it on the most
recent 30% of the period (out of sample) and compares it with buy & hold. Runs every Saturday, or:
```bash
python3 psx_backtester.py                 # KSE-100 universe, all strategies
python3 psx_backtester.py --symbols OGDC,PPL,LUCK --strategies breakout_20d
```
Only strategies with a **PASS** verdict should drive alerts.

## Deploy to Render
[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy)

## Tech Stack
- **Backend**: Python 3 (zero dependencies — stdlib only!)
- **Frontend**: Vanilla HTML/CSS/JS
- **Data Source**: PSX Data Portal (dps.psx.com.pk)
