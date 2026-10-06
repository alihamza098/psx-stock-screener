#!/usr/bin/env python3
"""
PSX Long-Term Model — point-in-time fundamentals + Quality/Value/Momentum (QVM) scoring
======================================================================================
Fundamentals: annual Sales and EPS scraped from DPS company pages (same table the
multibagger scanner uses: cache/multibagger.db › dps_financials_cache). Every scrape is kept,
so history grows over time beyond the ~4-6 years DPS shows at once.

Point in time: DPS labels results by fiscal year only (June and December year-ends both
exist), so annual results for fiscal year Y are treated as public only from
<availability_month_day> of year Y+1 (default 30 April). That is late for June year-ends
— deliberately: the model may use stale numbers but never future ones.

Score (config/longterm_model.json): each factor is a cross-sectional percentile 0-100.
  value_earnings_yield  latest EPS / price (vs sector peers when the sector is big enough)
  quality_consistency   share of the last 3 years with positive EPS
  quality_stability     1 - coefficient of variation of the last 3 years' EPS
  growth_sales          3-year sales CAGR (or longest available >= 2 years)
  growth_eps            latest EPS vs prior year
  momentum_12_1         12-month price return excluding the most recent month
Missing factors never earn points: their weight is dropped and the rest re-normalised;
stocks without the core data (EPS history, price momentum) are not ranked at all.
"""

import json
import math
import os
import re
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config" / "longterm_model.json"
FIN_DB_PATH = BASE_DIR / "cache" / "multibagger.db"
DATA_DIR = Path(os.environ.get("PSX_DATA_DIR") or (BASE_DIR / "cache"))
RANKINGS_PATH = DATA_DIR / "longterm_rankings.json"

_DEFAULTS: Dict[str, Any] = {
    "weights": {"value_earnings_yield": 0.30, "quality_consistency": 0.15, "quality_stability": 0.10,
                "growth_sales": 0.10, "growth_eps": 0.05, "momentum_12_1": 0.30},
    "availability_month_day": "04-30",
    "min_annual_years": 3,
    "max_results_age_years": 2,
    "min_price_pkr": 5.0,
    "min_avg_daily_value_pkr": 5_000_000,
    "sector_relative_value_min_members": 5,
    "portfolio": {"top_n": 10, "keep_n": 20, "max_per_sector": 3},
    "shariah_only": False,
}
FACTORS = list(_DEFAULTS["weights"])


def load_config() -> Dict[str, Any]:
    cfg = json.loads(json.dumps(_DEFAULTS))
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            elif not k.startswith("_"):
                cfg[k] = v
    except Exception:
        pass
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# Fundamentals store
# ─────────────────────────────────────────────────────────────────────────────

class FundamentalsStore:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else FIN_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache: Dict[str, List[Dict[str, Any]]] = {}
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS dps_financials_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, period TEXT NOT NULL,
                sales REAL, eps REAL, is_quarterly INTEGER DEFAULT 1, scraped_at TEXT NOT NULL,
                UNIQUE(symbol, period))""")

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self.db_path), timeout=15)
        c.row_factory = sqlite3.Row
        return c

    def save(self, symbol: str, rows: List[Dict[str, Any]]) -> int:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        n = 0
        with self._conn() as c:
            for r in rows:
                period = str(r.get("period") or "").strip()
                if not period or (r.get("sales") is None and r.get("eps") is None):
                    continue
                c.execute("""INSERT INTO dps_financials_cache (symbol, period, sales, eps, is_quarterly, scraped_at)
                             VALUES (?,?,?,?,?,?) ON CONFLICT(symbol, period) DO UPDATE SET
                             sales=excluded.sales, eps=excluded.eps, is_quarterly=excluded.is_quarterly,
                             scraped_at=excluded.scraped_at""",
                          (symbol.upper(), period, r.get("sales"), r.get("eps"),
                           1 if r.get("is_quarterly") else 0, now))
                n += 1
        self._cache.pop(symbol.upper(), None)
        return n

    def annual(self, symbol: str) -> List[Dict[str, Any]]:
        """All annual rows for a symbol, newest fiscal year first: [{fy, sales, eps}]."""
        sym = symbol.upper()
        if sym not in self._cache:
            with self._conn() as c:
                rows = c.execute("SELECT period, sales, eps FROM dps_financials_cache "
                                 "WHERE symbol=? AND is_quarterly=0", (sym,)).fetchall()
            out = []
            for r in rows:
                m = re.fullmatch(r"\s*(\d{4})\s*", r["period"] or "")
                if m:
                    out.append({"fy": int(m.group(1)), "sales": r["sales"], "eps": r["eps"]})
            self._cache[sym] = sorted(out, key=lambda x: -x["fy"])
        return self._cache[sym]

    def symbols(self) -> List[str]:
        with self._conn() as c:
            return [r[0] for r in c.execute("SELECT DISTINCT symbol FROM dps_financials_cache WHERE is_quarterly=0")]

    def as_of(self, symbol: str, date: str, availability_md: str = "04-30") -> List[Dict[str, Any]]:
        """Annual rows that were public on `date` (YYYY-MM-DD), newest first."""
        return [r for r in self.annual(symbol) if f"{r['fy'] + 1}-{availability_md}" <= date]


_store: Optional[FundamentalsStore] = None


def get_store() -> FundamentalsStore:
    global _store
    if _store is None:
        _store = FundamentalsStore()
    return _store


def refresh_fundamentals(symbols: List[str], fetch: Callable[..., str], pause_s: float = 1.0,
                         store: Optional[FundamentalsStore] = None,
                         progress: Optional[Callable[[str], None]] = None) -> Dict[str, int]:
    """Scrape annual + quarterly Sales/EPS from each DPS company page (polite: 1 request/second)."""
    from multibagger.financials_scraper import parse_financial_tables
    store = store or get_store()
    saved, failed = 0, 0
    for sym in symbols:
        try:
            html = fetch(f"https://dps.psx.com.pk/company/{sym}", timeout=15, retries=1)
            tables = parse_financial_tables(html)
            rows = tables.get("annual", []) + tables.get("quarterly", [])
            if rows:
                saved += store.save(sym, rows)
            else:
                failed += 1
        except Exception as e:
            failed += 1
            print(f"[Fundamentals] {sym}: {e}")
        if progress:
            progress(sym)
        time.sleep(pause_s)
    return {"symbols": len(symbols), "rows_saved": saved, "failed": failed}


# ─────────────────────────────────────────────────────────────────────────────
# Factor computation
# ─────────────────────────────────────────────────────────────────────────────

def raw_factors(annual: List[Dict[str, Any]], price: float, mom_12_1: Optional[float]) -> Dict[str, Optional[float]]:
    """Raw factor values from point-in-time annual rows (newest first)."""
    eps = [r["eps"] for r in annual if r.get("eps") is not None]
    sales = [r["sales"] for r in annual if r.get("sales") is not None]
    f: Dict[str, Optional[float]] = {k: None for k in FACTORS}
    if eps and price > 0:
        f["value_earnings_yield"] = eps[0] / price
    last3 = eps[:3]
    if len(last3) == 3:
        f["quality_consistency"] = sum(1 for e in last3 if e > 0) / 3.0
        mean = statistics.mean(last3)
        if mean > 0:
            cv = statistics.pstdev(last3) / mean
            f["quality_stability"] = 1.0 - min(1.0, cv)
    if len(eps) >= 2 and eps[0] > 0 and eps[1] > 0:  # growth across losses is meaningless
        f["growth_eps"] = max(-1.0, min(2.0, eps[0] / eps[1] - 1.0))
    span = min(3, len(sales) - 1)
    if span >= 2 and sales[0] > 0 and sales[span] > 0:
        f["growth_sales"] = (sales[0] / sales[span]) ** (1.0 / span) - 1.0
    f["momentum_12_1"] = mom_12_1
    return f


def _percentiles(values: Dict[str, float]) -> Dict[str, float]:
    """Average-rank percentile in [0, 100]; ties share a rank."""
    if not values:
        return {}
    items = sorted(values.items(), key=lambda kv: kv[1])
    n = len(items)
    out: Dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][1] == items[i][1]:
            j += 1
        pct = 100.0 * ((i + j) / 2.0) / (n - 1) if n > 1 else 50.0
        for k in range(i, j + 1):
            out[items[k][0]] = pct
        i = j + 1
    return out


def score_universe(snapshots: Dict[str, Dict[str, Any]], cfg: Optional[Dict[str, Any]] = None,
                   as_of: Optional[str] = None) -> List[Dict[str, Any]]:
    """Rank a universe at one point in time.

    snapshots: symbol -> {"price", "avg_daily_value", "mom_12_1", "sector", "annual" (point-in-time, newest first)}
    Returns eligible stocks sorted best first, each with factor values, percentiles and composite score.
    """
    cfg = cfg or load_config()
    weights = cfg["weights"]
    elig: Dict[str, Dict[str, Any]] = {}
    excluded: Dict[str, str] = {}
    shariah = None
    if cfg.get("shariah_only"):
        try:
            from psx_shariah import is_shariah_compliant as shariah
        except Exception:
            shariah = None
    for sym, s in snapshots.items():
        annual = s.get("annual") or []
        price = float(s.get("price") or 0)
        if shariah and not shariah(sym):
            excluded[sym] = "not Shariah compliant"
        elif price < float(cfg["min_price_pkr"]):
            excluded[sym] = "price below minimum"
        elif (s.get("avg_daily_value") or 0) < float(cfg["min_avg_daily_value_pkr"]):
            excluded[sym] = "too illiquid"
        elif len([r for r in annual if r.get("eps") is not None]) < int(cfg["min_annual_years"]):
            excluded[sym] = "not enough published annual results"
        elif as_of and annual[0]["fy"] < int(as_of[:4]) - int(cfg["max_results_age_years"]):
            excluded[sym] = f"latest results are stale (FY{annual[0]['fy']})"
        elif annual[0].get("eps") is None or annual[0]["eps"] <= 0:
            excluded[sym] = "latest annual EPS not positive"
        elif s.get("mom_12_1") is None:
            excluded[sym] = "less than 12 months of prices"
        else:
            elig[sym] = {"symbol": sym, "sector": s.get("sector") or "Other", "price": price,
                         "fiscal_year": annual[0]["fy"], "eps": annual[0]["eps"],
                         "raw": raw_factors(annual, price, s["mom_12_1"])}

    pct: Dict[str, Dict[str, float]] = {k: {} for k in FACTORS}
    for fac in FACTORS:
        vals = {sym: e["raw"][fac] for sym, e in elig.items() if e["raw"][fac] is not None}
        if fac == "value_earnings_yield":
            by_sector: Dict[str, Dict[str, float]] = {}
            for sym, v in vals.items():
                by_sector.setdefault(elig[sym]["sector"], {})[sym] = v
            big = {sec: v for sec, v in by_sector.items() if len(v) >= int(cfg["sector_relative_value_min_members"])}
            rest = {sym: v for sec, d in by_sector.items() if sec not in big for sym, v in d.items()}
            for d in big.values():
                pct[fac].update(_percentiles(d))
            pct[fac].update(_percentiles(rest))
        else:
            pct[fac] = _percentiles(vals)

    ranked = []
    for sym, e in elig.items():
        used = {f: w for f, w in weights.items() if sym in pct[f]}
        wsum = sum(used.values())
        if wsum <= 0:
            continue
        composite = sum(pct[f][sym] * w for f, w in used.items()) / wsum
        e.update(score=round(composite, 2),
                 percentiles={f: round(pct[f][sym], 1) for f in used},
                 coverage_pct=round(100 * wsum / sum(weights.values()), 0))
        ranked.append(e)
    ranked.sort(key=lambda x: (-x["score"], x["symbol"]))
    for i, e in enumerate(ranked):
        e["rank"] = i + 1
    return ranked


def momentum_12_1(closes: List[float], i: int) -> Optional[float]:
    if i < 252 or closes[i - 252] <= 0:
        return None
    return closes[i - 21] / closes[i - 252] - 1.0


def avg_daily_value(bars: List[Dict[str, Any]], i: int, n: int = 20) -> float:
    lo = max(0, i - n + 1)
    window = bars[lo:i + 1]
    return sum(b["close"] * b["volume"] for b in window) / len(window) if window else 0.0


def snapshots_from_bars(data: Dict[str, List[Dict[str, Any]]], date: str, sectors: Dict[str, str],
                        store: Optional[FundamentalsStore] = None, cfg: Optional[Dict[str, Any]] = None
                        ) -> Dict[str, Dict[str, Any]]:
    """Point-in-time inputs for score_universe on `date` using only bars up to that date."""
    store = store or get_store()
    cfg = cfg or load_config()
    out = {}
    for sym, bars in data.items():
        # last bar on or before date
        lo, hi = 0, len(bars) - 1
        i = -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if bars[mid]["date"] <= date:
                i, lo = mid, mid + 1
            else:
                hi = mid - 1
        if i < 0:
            continue
        closes = [b["close"] for b in bars[:i + 1]]
        out[sym] = {"price": bars[i]["close"], "avg_daily_value": avg_daily_value(bars, i),
                    "mom_12_1": momentum_12_1(closes, i), "sector": sectors.get(sym, "Other"),
                    "annual": store.as_of(sym, date, cfg["availability_month_day"])}
    return out


def sector_map(stocks: Optional[List[Dict[str, Any]]] = None) -> Dict[str, str]:
    if stocks is None:
        stocks = []
        for p in (BASE_DIR / "cache" / "stocks_cache.json", BASE_DIR / "data_snapshot.json"):
            try:
                stocks = json.loads(p.read_text()).get("data") or []
                if stocks:
                    break
            except Exception:
                continue
    return {s["symbol"].upper(): s.get("sector") or "Other" for s in stocks if s.get("symbol")}


def build_rankings(data: Dict[str, List[Dict[str, Any]]], stocks: Optional[List[Dict[str, Any]]] = None,
                   store: Optional[FundamentalsStore] = None, path: Path = RANKINGS_PATH) -> Dict[str, Any]:
    """Today's QVM ranking for the universe (saved for the /rankings page)."""
    cfg = load_config()
    if not data:
        return {"success": False, "error": "no price history"}
    as_of = max(b[-1]["date"] for b in data.values() if b)
    snaps = snapshots_from_bars(data, as_of, sector_map(stocks), store, cfg)
    ranked = score_universe(snaps, cfg, as_of)
    names = {s["symbol"].upper(): s.get("name", "") for s in (stocks or []) if s.get("symbol")}
    for r in ranked:
        r["name"] = names.get(r["symbol"], "")
        r["raw"] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in r["raw"].items()}
    top = select_portfolio(ranked, cfg)
    out = {"success": True, "as_of": as_of, "universe": len(snaps), "ranked": len(ranked),
           "excluded": len(snaps) - len(ranked), "weights": cfg["weights"],
           "portfolio_rules": cfg["portfolio"], "shariah_only": cfg.get("shariah_only", False),
           "availability_rule": f"FY results used from {cfg['availability_month_day']} of the following year",
           "model_portfolio": [r["symbol"] for r in top], "rankings": ranked}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=1, default=str))
    tmp.replace(path)
    return out


def select_portfolio(ranked: List[Dict[str, Any]], cfg: Optional[Dict[str, Any]] = None,
                     holdings: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Top N by score with a per-sector cap."""
    cfg = cfg or load_config()
    p = cfg["portfolio"]
    picked, per_sector = [], {}
    for r in ranked:
        if len(picked) >= int(p["top_n"]):
            break
        if per_sector.get(r["sector"], 0) >= int(p["max_per_sector"]):
            continue
        picked.append(r)
        per_sector[r["sector"]] = per_sector.get(r["sector"], 0) + 1
    return picked


def load_rankings(path: Path = RANKINGS_PATH) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None
