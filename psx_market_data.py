#!/usr/bin/env python3
"""
PSX Market Data Store — real ticks, real bars, no synthesis
===========================================================
Single place where observed market data is persisted and turned into bars.

Why this exists: the public DPS EOD endpoint only returns (ts, close, volume, open),
so the app used to *invent* daily highs/lows and split days into fake 4H/1H/15M bars.
Every indicator, stop and backtest built on those numbers was unreliable.

This module only ever stores what was actually observed:

  * ticks       — one row per observed (symbol, time, price, traded-volume increment)
                  sources: "dps_int"  (DPS /timeseries/int intraday trades)
                           "mw"       (DPS market-watch snapshot, cumulative volume)
                           "screener" (DPS screener snapshot, price only)
  * daily_bars  — real session OHLCV per (symbol, date). Open/high/low come from
                  market-watch or company-page quotes, which publish the true day range.

Intraday bars (1m/5m/15m/1H/4H) are aggregated from ticks only. If we did not observe
a period, there is no bar for it — callers must handle missing data honestly.

Pure stdlib. Network access is injected (`fetch` callable) so everything is testable offline.
"""

import datetime
import json
import os
import sqlite3
import threading
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

PKT = datetime.timezone(datetime.timedelta(hours=5))

DATA_DIR = Path(os.environ.get("PSX_DATA_DIR") or (Path(__file__).parent / "cache"))
DB_PATH = DATA_DIR / "market_data.db"

DPS_BASE = "https://dps.psx.com.pk"
INTRADAY_URL = DPS_BASE + "/timeseries/int/{symbol}"
MARKET_WATCH_URL = DPS_BASE + "/market-watch"

TICK_RETENTION_DAYS = int(os.environ.get("PSX_TICK_RETENTION_DAYS", "60"))

# Session open (minutes after midnight PKT) used to align intraday buckets.
# Mirrors shared_trading_utils.get_session_schedule (Mon-Thu 09:32, Fri 09:17).
_SESSION_OPEN_MINS = {0: 572, 1: 572, 2: 572, 3: 572, 4: 557}

TIMEFRAME_MINUTES = {"1M": 1, "5M": 5, "15M": 15, "30M": 30, "1H": 60, "4H": 240}


def pkt_date(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, PKT).strftime("%Y-%m-%d")


def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("Rs.", "").replace("%", "")
    if s in ("", "-", "--", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Parsers (pure functions — no I/O)
# ─────────────────────────────────────────────────────────────────────────────

def parse_intraday_timeseries(payload: Any) -> List[Tuple[int, float, float]]:
    """Parse a DPS /timeseries/int response into [(ts, price, trade_volume)] oldest first.

    Accepts the decoded JSON or the raw string. Rows that are malformed, non-positive
    or duplicated are dropped. Returns [] on anything unexpected.
    """
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return []
    if not isinstance(payload, dict) or payload.get("status") != 1:
        return []
    rows = payload.get("data")
    if not isinstance(rows, list):
        return []
    out: Dict[int, Tuple[int, float, float]] = {}
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        ts = _to_float(row[0])
        price = _to_float(row[1])
        vol = _to_float(row[2]) if len(row) > 2 else 0.0
        if ts is None or price is None or price <= 0:
            continue
        if ts > 1e12:  # milliseconds
            ts = ts / 1000.0
        key = int(ts)
        # Several trades can share a second: keep the last price, sum the volume
        prev = out.get(key)
        vol = max(0.0, vol or 0.0)
        out[key] = (key, price, (prev[2] if prev else 0.0) + vol)
    return [out[k] for k in sorted(out)]


class _TableParser(HTMLParser):
    """Collects every <table> as a list of rows of cell text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: List[List[List[str]]] = []
        self._depth = 0
        self._row: Optional[List[str]] = None
        self._cell: Optional[List[str]] = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._depth += 1
            self.tables.append([])
        elif tag == "tr" and self._depth:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row and self.tables:
                self.tables[-1].append(self._row)
            self._row = None
        elif tag == "table" and self._depth:
            self._depth -= 1

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


_MW_COLUMNS = {
    "symbol": ("SYMBOL", "SCRIP"),
    "ldcp": ("LDCP",),
    "open": ("OPEN",),
    "high": ("HIGH",),
    "low": ("LOW",),
    "price": ("CURRENT", "CLOSE", "LAST"),
    "volume": ("VOLUME",),
}


def parse_market_watch_html(html: str) -> List[Dict[str, Any]]:
    """Parse a DPS market-watch style table into rows with real session OHLCV.

    The table is located by its header row (needs SYMBOL, OPEN, HIGH, LOW, CURRENT, VOLUME),
    so layout changes elsewhere on the page don't matter. Rows failing sanity checks
    (low <= price <= high, low <= open <= high) are dropped rather than "repaired".
    """
    if not html:
        return []
    p = _TableParser()
    try:
        p.feed(html)
    except Exception:
        return []
    for table in p.tables:
        if not table:
            continue
        header = [c.upper() for c in table[0]]
        idx: Dict[str, int] = {}
        for key, names in _MW_COLUMNS.items():
            for i, h in enumerate(header):
                if any(h == n or h.startswith(n) for n in names):
                    idx[key] = i
                    break
        if not all(k in idx for k in ("symbol", "open", "high", "low", "price", "volume")):
            continue
        rows = []
        for r in table[1:]:
            if len(r) <= max(idx.values()):
                continue
            sym = r[idx["symbol"]].split(" ")[0].strip().upper()
            o, h, l, c = (_to_float(r[idx[k]]) for k in ("open", "high", "low", "price"))
            v = _to_float(r[idx["volume"]])
            ldcp = _to_float(r[idx["ldcp"]]) if "ldcp" in idx else None
            if not sym or None in (o, h, l, c) or c <= 0 or l <= 0:
                continue
            if not (l <= c <= h and l <= o <= h):
                continue
            rows.append({"symbol": sym, "open": o, "high": h, "low": l, "price": c,
                         "volume": max(0.0, v or 0.0), "ldcp": ldcp})
        if rows:
            return rows
    return []


# ─────────────────────────────────────────────────────────────────────────────
# Store
# ─────────────────────────────────────────────────────────────────────────────

class MarketDataStore:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # last cumulative volume seen per (symbol, date, source) → to derive increments
        self._last_cum: Dict[Tuple[str, str, str], float] = {}
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self):
        with self._lock, self._connect() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("""
                CREATE TABLE IF NOT EXISTS ticks (
                    symbol TEXT NOT NULL,
                    ts INTEGER NOT NULL,
                    price REAL NOT NULL,
                    volume REAL NOT NULL DEFAULT 0,
                    source TEXT NOT NULL,
                    PRIMARY KEY (symbol, ts, source)
                )""")
            c.execute("""
                CREATE TABLE IF NOT EXISTS daily_bars (
                    symbol TEXT NOT NULL,
                    date TEXT NOT NULL,
                    open REAL, high REAL, low REAL, close REAL,
                    volume REAL, ldcp REAL,
                    source TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (symbol, date)
                )""")

    # ── writes ──────────────────────────────────────────────────────────────

    def record_intraday_trades(self, symbol: str, trades: Iterable[Tuple[int, float, float]]) -> int:
        rows = [(symbol.upper(), int(ts), float(p), float(v), "dps_int") for ts, p, v in trades]
        if not rows:
            return 0
        with self._lock, self._connect() as c:
            c.executemany("INSERT OR REPLACE INTO ticks VALUES (?,?,?,?,?)", rows)
        return len(rows)

    def record_snapshot(self, quotes: Iterable[Dict[str, Any]], ts: Optional[float] = None,
                        source: str = "screener", cumulative_volume: bool = False) -> int:
        """Record one polling snapshot (many symbols, same timestamp).

        If `cumulative_volume` is True, quote["volume"] is the session's cumulative volume and
        is converted to the increment since the previous snapshot. Otherwise volume is not
        known for this snapshot and is stored as 0 (price-only tick).
        """
        ts = int(ts or time.time())
        day = pkt_date(ts)
        rows = []
        with self._lock:
            for q in quotes:
                sym = (q.get("symbol") or "").upper().strip()
                price = _to_float(q.get("price"))
                if not sym or price is None or price <= 0:
                    continue
                inc = 0.0
                if cumulative_volume:
                    cum = _to_float(q.get("volume")) or 0.0
                    key = (sym, day, source)
                    prev = self._last_cum.get(key)
                    if prev is None:
                        prev = self._last_cumulative_from_db(sym, day, source)
                    inc = max(0.0, cum - prev) if prev is not None else cum
                    self._last_cum[key] = max(cum, prev or 0.0)
                rows.append((sym, ts, price, inc, source))
            if rows:
                with self._connect() as c:
                    c.executemany("INSERT OR REPLACE INTO ticks VALUES (?,?,?,?,?)", rows)
        return len(rows)

    def _last_cumulative_from_db(self, symbol: str, day: str, source: str) -> Optional[float]:
        start, end = _day_bounds(day)
        with self._connect() as c:
            r = c.execute("SELECT SUM(volume) FROM ticks WHERE symbol=? AND source=? AND ts>=? AND ts<?",
                          (symbol, source, start, end)).fetchone()
        return float(r[0]) if r and r[0] is not None else None

    def upsert_daily_observation(self, symbol: str, date: str, *, open_: Optional[float],
                                 high: Optional[float], low: Optional[float], close: Optional[float],
                                 volume: Optional[float] = None, ldcp: Optional[float] = None,
                                 source: str = "mw") -> None:
        """Merge a real intraday observation of the session into daily_bars.

        Open keeps the first value seen, high/low widen monotonically, close/volume take the latest.
        """
        if close is None or close <= 0:
            return
        sym = symbol.upper()
        with self._lock, self._connect() as c:
            cur = c.execute("SELECT * FROM daily_bars WHERE symbol=? AND date=?", (sym, date)).fetchone()
            if cur:
                o = cur["open"] if cur["open"] else open_
                hs = [x for x in (cur["high"], high, close) if x]
                ls = [x for x in (cur["low"], low, close) if x]
                v = max(cur["volume"] or 0.0, volume or 0.0)
                ld = cur["ldcp"] or ldcp
            else:
                o = open_
                hs = [x for x in (high, close) if x]
                ls = [x for x in (low, close) if x]
                v = volume or 0.0
                ld = ldcp
            c.execute("INSERT OR REPLACE INTO daily_bars VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (sym, date, o, max(hs), min(ls), close, v, ld, source, time.time()))

    def record_market_watch(self, rows: List[Dict[str, Any]], ts: Optional[float] = None) -> int:
        ts = int(ts or time.time())
        day = pkt_date(ts)
        n = self.record_snapshot(rows, ts=ts, source="mw", cumulative_volume=True)
        for r in rows:
            self.upsert_daily_observation(r["symbol"], day, open_=r.get("open"), high=r.get("high"),
                                          low=r.get("low"), close=r.get("price"),
                                          volume=r.get("volume"), ldcp=r.get("ldcp"), source="mw")
        return n

    def prune(self, retention_days: int = TICK_RETENTION_DAYS) -> int:
        cutoff = int(time.time() - retention_days * 86400)
        with self._lock, self._connect() as c:
            return c.execute("DELETE FROM ticks WHERE ts < ?", (cutoff,)).rowcount

    # ── reads ───────────────────────────────────────────────────────────────

    def get_ticks(self, symbol: str, start_ts: float = 0, end_ts: Optional[float] = None) -> List[Dict[str, Any]]:
        """Ticks oldest first. When several sources cover the same day, the richest source wins
        (dps_int > mw > screener) so the same trade is never counted twice."""
        end_ts = end_ts or time.time() + 1
        with self._connect() as c:
            rows = c.execute("SELECT ts, price, volume, source FROM ticks WHERE symbol=? AND ts>=? AND ts<? "
                             "ORDER BY ts", (symbol.upper(), int(start_ts), int(end_ts))).fetchall()
        by_day: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        for r in rows:
            by_day.setdefault(pkt_date(r["ts"]), {}).setdefault(r["source"], []).append(dict(r))
        out: List[Dict[str, Any]] = []
        for day in sorted(by_day):
            srcs = by_day[day]
            for pref in ("dps_int", "mw", "screener"):
                if pref in srcs:
                    out.extend(srcs[pref])
                    break
        return out

    def get_daily_bars(self, symbol: str) -> Dict[str, Dict[str, Any]]:
        with self._connect() as c:
            rows = c.execute("SELECT * FROM daily_bars WHERE symbol=? ORDER BY date", (symbol.upper(),)).fetchall()
        return {r["date"]: dict(r) for r in rows}

    def build_bars(self, symbol: str, timeframe: str = "15M", days: int = 10,
                   now: Optional[float] = None) -> List[Dict[str, Any]]:
        """Aggregate observed ticks into OHLCV bars aligned to each day's session open.

        Only periods with at least one observed tick produce a bar. Each bar carries
        `ticks` (number of observations) so consumers can judge its quality.
        """
        minutes = TIMEFRAME_MINUTES.get(timeframe.upper())
        if not minutes:
            raise ValueError(f"Unsupported intraday timeframe: {timeframe}")
        now = now or time.time()
        ticks = self.get_ticks(symbol, start_ts=now - days * 86400, end_ts=now + 1)
        return aggregate_ticks(ticks, minutes)


def _day_bounds(day: str) -> Tuple[int, int]:
    d = datetime.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=PKT)
    start = int(d.timestamp())
    return start, start + 86400


def aggregate_ticks(ticks: List[Dict[str, Any]], minutes: int) -> List[Dict[str, Any]]:
    bars: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for t in ticks:
        dt = datetime.datetime.fromtimestamp(t["ts"], PKT)
        open_mins = _SESSION_OPEN_MINS.get(dt.weekday(), 572)
        mins = dt.hour * 60 + dt.minute
        bucket = max(0, (mins - open_mins)) // minutes
        key = (dt.strftime("%Y-%m-%d"), bucket)
        p, v = float(t["price"]), float(t.get("volume") or 0.0)
        b = bars.get(key)
        if b is None:
            start_mins = open_mins + bucket * minutes
            start_dt = dt.replace(hour=start_mins // 60, minute=start_mins % 60, second=0, microsecond=0)
            bars[key] = {"timestamp": int(start_dt.timestamp()), "date": key[0],
                         "time": start_dt.strftime("%H:%M"),
                         "open": p, "high": p, "low": p, "close": p, "volume": v, "ticks": 1}
        else:
            b["high"] = max(b["high"], p)
            b["low"] = min(b["low"], p)
            b["close"] = p
            b["volume"] += v
            b["ticks"] += 1
    return [bars[k] for k in sorted(bars)]


# ─────────────────────────────────────────────────────────────────────────────
# Network helpers (fetch is injected: fetch(url, timeout=..., retries=...) -> str)
# ─────────────────────────────────────────────────────────────────────────────

_store: Optional[MarketDataStore] = None
_store_lock = threading.Lock()
_intraday_fetched_at: Dict[str, float] = {}
_mw_status: Dict[str, Any] = {"ok": None, "last_success": 0.0, "last_error": "", "rows": 0}


def get_store() -> MarketDataStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = MarketDataStore()
        return _store


def refresh_intraday(symbol: str, fetch: Callable[..., str], max_age_s: float = 60.0,
                     store: Optional[MarketDataStore] = None) -> int:
    """Pull today's intraday trades for one symbol from DPS (rate-limited per symbol)."""
    sym = symbol.upper()
    now = time.time()
    if now - _intraday_fetched_at.get(sym, 0) < max_age_s:
        return 0
    _intraday_fetched_at[sym] = now
    try:
        raw = fetch(INTRADAY_URL.format(symbol=sym), timeout=10, retries=1)
    except Exception as e:
        print(f"[MarketData] intraday fetch failed for {sym}: {e}")
        return 0
    trades = parse_intraday_timeseries(raw)
    return (store or get_store()).record_intraday_trades(sym, trades)


def poll_market_watch(fetch: Callable[..., str], store: Optional[MarketDataStore] = None) -> List[Dict[str, Any]]:
    """Fetch & record the all-symbol market-watch table. Returns parsed rows ([] on failure)."""
    try:
        html = fetch(MARKET_WATCH_URL, timeout=20, retries=1)
        rows = parse_market_watch_html(html)
    except Exception as e:
        _mw_status.update(ok=False, last_error=str(e))
        return []
    if not rows:
        _mw_status.update(ok=False, last_error="no market-watch table found")
        return []
    (store or get_store()).record_market_watch(rows)
    _mw_status.update(ok=True, last_success=time.time(), last_error="", rows=len(rows))
    return rows


def market_watch_status() -> Dict[str, Any]:
    return dict(_mw_status)
