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

## Deploy to Render
[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy)

## Tech Stack
- **Backend**: Python 3 (zero dependencies — stdlib only!)
- **Frontend**: Vanilla HTML/CSS/JS
- **Data Source**: PSX Data Portal (dps.psx.com.pk)
