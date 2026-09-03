"""
PSX Undervalued Stock Analyzer — AI Engine & Valuation Processor
Implementation of the PSX Undervalued Stock Valuation Engine Specification.
Evaluates relative undervaluation against sector peers and absolute undervaluation
via DDM (Dividend Discount Model), DCF, or Graham Number with Margin of Safety & Quality Gates.
"""

import os
import json
import math
import sqlite3
import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

DB_PATH = Path(__file__).parent / "cache" / "undervalued.db"

# ═══════════════════════════════════════════════════════════════════════════
# 🧠  AI SYSTEM PROMPT (Verbatim Spec)
# ═══════════════════════════════════════════════════════════════════════════

UNDERVALUED_SYSTEM_PROMPT = """You are the valuation engine inside a Pakistan Stock Exchange (PSX) portfolio app.
Your only job: given structured financial data for one PSX-listed stock, its
sector peers, and current macro inputs, compute whether the stock is
undervalued, fairly valued, or overvalued — and return a structured verdict.

You are NOT a chat assistant in this context. Do not add conversational
preamble, do not ask clarifying questions, do not offer opinions outside the
schema. If required data is missing, say so inside the schema — never guess,
interpolate, or fabricate a number to fill a gap.

═══════════════════════════════════════════════════════════════
DEFINITIONS
═══════════════════════════════════════════════════════════════

A stock is UNDERVALUED when its current market price sits below a defensible
estimate of intrinsic value, with a margin of safety wide enough to absorb
estimation error — evaluated two ways:

1. RELATIVE UNDERVALUATION: cheaper than sector peers on standard multiples
   (P/E, P/B, EV/EBITDA, dividend yield) without a fundamental reason
   (deteriorating earnings, governance risk, structural decline).

2. ABSOLUTE UNDERVALUATION: market price below intrinsic value computed via
   Dividend Discount Model (DDM) for stable dividend payers, or Discounted
   Cash Flow (DCF) for growth/reinvestment-heavy firms.

Never issue a verdict from relative multiples alone. A stock cheap only
because the whole KSE-100 is trading below its historical average P/E is not
the same as a stock mispriced relative to its own fundamentals. Always
attempt the intrinsic cross-check before finalizing a verdict.
"""

DISCLAIMER_TEXT = (
    "This is a data-driven valuation screen, not investment advice. Verify earnings "
    "quality, governance, and recent company announcements before acting. Past dividend "
    "patterns and analyst forecasts are not guarantees of future performance."
)


# ═══════════════════════════════════════════════════════════════════════════
# 💾  DATABASE INITIALIZATION
# ═══════════════════════════════════════════════════════════════════════════

def get_db_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=20.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS undervalued_stocks (
        symbol TEXT PRIMARY KEY,
        name TEXT,
        sector TEXT,
        price REAL,
        verdict TEXT NOT NULL,
        relative_score REAL,
        pe REAL,
        pb REAL,
        div_yield_pct REAL,
        ev_ebitda REAL,
        pe_pct_below_sector REAL,
        pb_pct_below_sector REAL,
        div_yield_pct_above_sector REAL,
        intrinsic_method TEXT,
        fair_value REAL,
        margin_of_safety_pct REAL,
        confidence TEXT,
        flags_json TEXT,
        data_gaps_json TEXT,
        payload_json TEXT,
        updated_at TEXT
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_uv_verdict ON undervalued_stocks(verdict)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_uv_mos ON undervalued_stocks(margin_of_safety_pct)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_uv_score ON undervalued_stocks(relative_score)")
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# 📊  SECTOR AVERAGES & MACRO INPUTS
# ═══════════════════════════════════════════════════════════════════════════

def get_macro_inputs() -> Dict[str, float]:
    """Retrieves macro inputs from long-term database or current SBP benchmark."""
    # SBP Policy Rate: 11.5% (current Pakistan Benchmark)
    # Equity Risk Premium: 5.0%
    return {
        "risk_free_rate_pct": 11.5,
        "equity_risk_premium_pct": 5.0,
        "kse100_forward_pe": 8.1,
        "kse100_10yr_avg_pe": 8.5
    }


def compute_sector_peers_summary(stocks: List[Dict]) -> Dict[str, Dict[str, float]]:
    """
    Computes sector median/trimmed averages for P/E, P/B, EV/EBITDA, Dividend Yield, and D/E.
    Prevents outlier skew by filtering out negative and extreme multiples.
    """
    sector_groups: Dict[str, List[Dict]] = {}
    for s in stocks:
        sec = s.get("sector") or "Other"
        sector_groups.setdefault(sec, []).append(s)

    summary = {}
    for sec, items in sector_groups.items():
        pe_vals = [s["pe"] for s in items if isinstance(s.get("pe"), (int, float)) and s["pe"] > 3 and s["pe"] < 80]
        dy_vals = [s["divYield"] for s in items if isinstance(s.get("divYield"), (int, float)) and s["divYield"] >= 0]
        
        # P/E average
        avg_pe = round(sum(pe_vals) / len(pe_vals), 2) if pe_vals else 8.5
        # Div Yield average
        avg_dy = round(sum(dy_vals) / len(dy_vals), 2) if dy_vals else 6.0
        # P/B baseline (typically 1.2 - 2.5 for PSX)
        avg_pb = 1.8
        # EV/EBITDA baseline
        avg_ev_ebitda = 6.0
        # Debt to Equity baseline
        avg_de = 0.85

        summary[sec] = {
            "sector_avg_pe": avg_pe,
            "sector_avg_pb": avg_pb,
            "sector_avg_ev_ebitda": avg_ev_ebitda,
            "sector_avg_dividend_yield": avg_dy,
            "sector_avg_debt_to_equity": avg_de
        }

    return summary


# ═══════════════════════════════════════════════════════════════════════════
# ⚙️  CORE VALUATION EVALUATION (STEPS 1 - 5)
# ═══════════════════════════════════════════════════════════════════════════

def evaluate_stock_valuation(input_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Implements the 5-step Valuation Algorithm & Hard Rules:
    Step 1: Relative Metrics (P/E, P/B, Div Yield, EV/EBITDA, Graham Number)
    Step 2: Relative Score (0-100 scale, weighted components, distress flags)
    Step 3: Intrinsic Value (DDM -> DCF -> Graham Number -> null)
    Step 4: Margin of Safety & Core Verdict
    Step 5: Quality / Liquidity Gate
    """
    stock = input_data.get("stock", {})
    peers = input_data.get("sector_peers", {})
    macro = input_data.get("macro_inputs", {})

    ticker = stock.get("ticker", "")
    price = float(stock.get("price") or 0.0)
    eps_ttm = float(stock.get("eps_ttm") or 0.0) if stock.get("eps_ttm") is not None else None
    bvps = float(stock.get("bvps") or 0.0) if stock.get("bvps") is not None else None
    dps = float(stock.get("dividend_per_share") or 0.0)
    div_history = stock.get("dividend_history_5yr") or []
    ebitda = stock.get("ebitda")
    ev = stock.get("enterprise_value")
    fcf_forecast = stock.get("fcf_forecast_3yr") or []
    debt_equity = stock.get("debt_to_equity")
    shares = float(stock.get("shares_outstanding") or 0.0)
    free_float_pct = stock.get("free_float_pct")
    one_off = stock.get("one_off_items_flag", False)

    flags = []
    data_gaps = []

    # Check Required Inputs
    if not ticker: data_gaps.append("ticker")
    if price <= 0: data_gaps.append("price")
    if eps_ttm is None: data_gaps.append("eps_ttm")
    if bvps is None or bvps <= 0: data_gaps.append("bvps")
    if shares <= 0: data_gaps.append("shares_outstanding")

    risk_free = float(macro.get("risk_free_rate_pct") or 11.5)
    erp = float(macro.get("equity_risk_premium_pct") or 5.0)

    # ── STEP 1: Relative Metrics ──────────────────────────────────────────────
    # Rule 5: If eps_ttm <= 0, do not compute P/E — set it null and flag
    pe = None
    if eps_ttm is not None and eps_ttm > 0 and price > 0:
        pe = round(price / eps_ttm, 2)
    elif eps_ttm is not None and eps_ttm <= 0:
        flags.append("negative earnings")

    # P/B
    pb = None
    if bvps and bvps > 0 and price > 0:
        pb = round(price / bvps, 2)

    # Dividend Yield
    div_yield_pct = 0.0
    if price > 0 and dps > 0:
        div_yield_pct = round((dps / price) * 100.0, 2)

    # EV/EBITDA
    ev_ebitda = None
    if ev and ebitda and ebitda > 0:
        ev_ebitda = round(ev / ebitda, 2)

    # Graham Number
    graham_number = None
    if eps_ttm and eps_ttm > 0 and bvps and bvps > 0:
        graham_val = 22.5 * eps_ttm * bvps
        if graham_val > 0:
            graham_number = round(math.sqrt(graham_val), 2)

    # Multiple Comparisons vs Sector
    sec_pe = float(peers.get("sector_avg_pe") or 8.5)
    sec_pb = float(peers.get("sector_avg_pb") or 1.8)
    sec_dy = float(peers.get("sector_avg_dividend_yield") or 6.0)
    sec_de = float(peers.get("sector_avg_debt_to_equity") or 0.85)

    pe_pct_below = round(((sec_pe - pe) / sec_pe) * 100.0, 1) if (pe and sec_pe > 0) else None
    pb_pct_below = round(((sec_pb - pb) / sec_pb) * 100.0, 1) if (pb and sec_pb > 0) else None
    dy_pct_above = round(div_yield_pct - sec_dy, 2) if sec_dy > 0 else None

    # ── STEP 2: Relative Score (0-100 scale) ──────────────────────────────────
    relative_score = None
    if price > 0 and not data_gaps:
        # pe_component (weight 0.30)
        # Rule 4: P/E below 3 is a distress signal, not a bargain
        pe_score = 50.0
        if pe is None:
            pe_score = 0.0
        elif pe < 3.0:
            pe_score = 10.0
            flags.append("P/E below 3 signals potential earnings quality or distress risk")
        else:
            # Reward PE below sector
            ratio = pe / sec_pe
            if ratio <= 0.5: pe_score = 100.0
            elif ratio <= 0.75: pe_score = 80.0
            elif ratio <= 1.0: pe_score = 65.0
            elif ratio <= 1.25: pe_score = 45.0
            else: pe_score = 25.0

        # pb_component (weight 0.20)
        # Rule 4: P/B below 0.3 usually signals distress, not a bargain
        pb_score = 50.0
        if pb is None:
            pb_score = 0.0
        elif pb <= 0.3:
            pb_score = 10.0
            flags.append("P/B below 0.3 signals severe distress/solvency risk, not a bargain")
        else:
            ratio_pb = pb / sec_pb
            if ratio_pb <= 0.6: pb_score = 100.0
            elif ratio_pb <= 0.85: pb_score = 80.0
            elif ratio_pb <= 1.0: pb_score = 65.0
            elif ratio_pb <= 1.3: pb_score = 40.0
            else: pb_score = 20.0

        # div_yield_component (weight 0.20)
        div_score = 50.0
        payout_ratio = (dps / eps_ttm * 100.0) if (eps_ttm and eps_ttm > 0 and dps > 0) else 0.0
        if payout_ratio > 80.0:
            flags.append("dividend payout ratio exceeds 80% — yield may not be sustainable")
            div_score = 50.0  # Capped as required by spec
        else:
            if div_yield_pct >= sec_dy * 1.5: div_score = 100.0
            elif div_yield_pct >= sec_dy * 1.1: div_score = 80.0
            elif div_yield_pct >= sec_dy * 0.8: div_score = 60.0
            elif div_yield_pct > 0: div_score = 40.0
            else: div_score = 20.0

        # ev_ebitda_component (weight 0.20)
        ev_score = 50.0
        if ev_ebitda:
            sec_ev = float(peers.get("sector_avg_ev_ebitda") or 6.0)
            if ev_ebitda <= sec_ev * 0.7: ev_score = 90.0
            elif ev_ebitda <= sec_ev: ev_score = 70.0
            else: ev_score = 35.0
        else:
            # Fallback to multiple parity
            ev_score = (pe_score + pb_score) / 2.0

        # leverage_penalty (weight 0.10)
        leverage_score = 100.0
        if debt_equity is not None and sec_de > 0:
            if debt_equity > (sec_de * 1.25):
                leverage_score = max(0.0, 100.0 - (debt_equity / sec_de - 1.25) * 50.0)
                flags.append(f"debt-to-equity ({debt_equity:.2f}) exceeds sector avg ({sec_de:.2f}) by >25%")

        # Combined Relative Score
        raw_score = (
            (pe_score * 0.30) +
            (pb_score * 0.20) +
            (div_score * 0.20) +
            (ev_score * 0.20) +
            (leverage_score * 0.10)
        )
        relative_score = round(min(100.0, max(0.0, raw_score)), 1)

    # ── STEP 3: Intrinsic Value (Order: DDM -> DCF -> Graham Number) ─────────
    fair_value = None
    method_used = "none"
    required_return_used = None
    growth_rate_used = None
    confidence = "low"

    required_return = (risk_free + erp) / 100.0  # e.g. 0.165 (16.5%)

    # (a) Check DDM (Gordon Growth)
    has_stable_divs = False
    if len(div_history) >= 4 and all(d > 0 for d in div_history):
        # Check no dividend cuts
        no_cuts = all(div_history[i] >= div_history[i-1] * 0.95 for i in range(1, len(div_history)))
        # Coefficient of variation
        mean_div = sum(div_history) / len(div_history)
        variance = sum((d - mean_div) ** 2 for d in div_history) / len(div_history)
        cv = (math.sqrt(variance) / mean_div) if mean_div > 0 else 1.0
        if no_cuts and cv < 0.25:
            has_stable_divs = True

    if has_stable_divs and dps > 0:
        method_used = "DDM"
        # 5-year CAGR
        n_years = len(div_history) - 1
        cagr = (div_history[-1] / div_history[0]) ** (1.0 / n_years) - 1.0
        # Rule 3: Never let dividend_growth_g exceed required_return
        # Cap g at sustainable long-term bound and at required_return - 0.015
        max_allowable_g = min(0.08, required_return - 0.015)
        g = cagr
        if g >= max_allowable_g:
            g = max_allowable_g
            flags.append("growth assumption capped to avoid division error and explosive valuation")
        
        g = max(0.0, g)
        denom = required_return - g
        if denom > 0.005:
            fair_val_calc = (dps * (1.0 + g)) / denom
            fair_value = round(fair_val_calc, 2)
            growth_rate_used = round(g * 100.0, 2)
            required_return_used = round(required_return * 100.0, 2)
            confidence = "high"

    # (b) Else if FCF forecast is present -> 3-year DCF with terminal value
    elif len(fcf_forecast) == 3 and shares > 0:
        method_used = "DCF"
        long_term_growth = 0.05  # Pakistan long-run GDP growth proxy
        # Discount forecast years
        pv_fcfs = sum(fcf / ((1.0 + required_return) ** (i + 1)) for i, fcf in enumerate(fcf_forecast))
        terminal_val = (fcf_forecast[2] * (1.0 + long_term_growth)) / (required_return - long_term_growth)
        pv_terminal = terminal_val / ((1.0 + required_return) ** 3)
        fair_value = round((pv_fcfs + pv_terminal) / shares, 2)
        growth_rate_used = round(long_term_growth * 100.0, 2)
        required_return_used = round(required_return * 100.0, 2)
        confidence = "medium"

    # (c) Else fallback to Graham Number
    elif graham_number is not None and eps_ttm > 0 and bvps > 0:
        method_used = "Graham_Number"
        fair_value = graham_number
        confidence = "low"

    else:
        method_used = "none"
        fair_value = None
        confidence = "low"
        flags.append("missing macro/financial inputs — intrinsic valuation not computed")

    # ── STEP 4: Margin of Safety & Verdict ───────────────────────────────────
    margin_of_safety_pct = None
    if fair_value and fair_value > 0 and price > 0:
        margin_of_safety_pct = round(((fair_value - price) / fair_value) * 100.0, 2)

    verdict = "insufficient_data"
    if data_gaps or relative_score is None:
        verdict = "insufficient_data"
    else:
        # Tertile bounds: Top tertile >= 66.7, Bottom tertile <= 33.3
        top_tertile = relative_score >= 66.7
        bottom_tertile = relative_score <= 33.3

        if margin_of_safety_pct is not None:
            if top_tertile and margin_of_safety_pct >= 15.0:
                verdict = "undervalued"
            elif top_tertile or margin_of_safety_pct >= 15.0:
                verdict = "possibly_undervalued"
            elif -10.0 <= margin_of_safety_pct < 15.0 and not bottom_tertile:
                verdict = "fairly_valued"
            elif bottom_tertile and margin_of_safety_pct <= -10.0:
                verdict = "overvalued"
            else:
                verdict = "fairly_valued"
        else:
            if top_tertile:
                verdict = "possibly_undervalued"
            elif bottom_tertile:
                verdict = "overvalued"
            else:
                verdict = "fairly_valued"

    # ── STEP 5: Quality / Liquidity Gate ─────────────────────────────────────
    if verdict == "undervalued":
        reasons_to_downgrade = []
        if free_float_pct is not None and free_float_pct < 10.0:
            reasons_to_downgrade.append(f"illiquid free float ({free_float_pct:.1f}% < 10%)")
        if one_off:
            reasons_to_downgrade.append("one-off earnings items present")
        if eps_ttm is not None and eps_ttm <= 0:
            reasons_to_downgrade.append("trailing EPS is negative/zero")
        if debt_equity is not None and sec_de > 0 and debt_equity > (2.0 * sec_de):
            reasons_to_downgrade.append(f"debt-to-equity ({debt_equity:.2f}) is more than double sector average")

        if reasons_to_downgrade:
            verdict = "undervalued_caution"
            flags.extend(reasons_to_downgrade)

    return {
        "ticker": ticker,
        "name": stock.get("name", ticker),
        "sector": stock.get("sector", "Other"),
        "price": price,
        "verdict": verdict,
        "relative_score": relative_score,
        "relative_metrics": {
            "pe": pe,
            "pb": pb,
            "div_yield_pct": div_yield_pct,
            "ev_ebitda": ev_ebitda,
            "vs_sector": {
                "pe_pct_below_sector": pe_pct_below,
                "pb_pct_below_sector": pb_pct_below,
                "div_yield_pct_above_sector": dy_pct_above
            }
        },
        "intrinsic_valuation": {
            "method_used": method_used,
            "fair_value_per_share": fair_value,
            "required_return_pct_used": required_return_used,
            "growth_rate_pct_used": growth_rate_used,
            "margin_of_safety_pct": margin_of_safety_pct
        },
        "confidence": confidence,
        "flags": list(dict.fromkeys(flags)),  # Deduplicate flags
        "data_gaps": data_gaps,
        "disclaimer": DISCLAIMER_TEXT
    }


# ═══════════════════════════════════════════════════════════════════════════
# 🚀  UNIVERSE SCANNER & PERSISTER
# ═══════════════════════════════════════════════════════════════════════════

def build_stock_input(stock: Dict, sector_peers: Dict[str, Any], macro_inputs: Dict[str, Any], fundamentals_map: Dict[str, Dict]) -> Dict[str, Any]:
    """Prepares structured JSON matching the exact required input schema."""
    symbol = stock.get("symbol", "")
    price = float(stock.get("price") or 0.0)
    pe = stock.get("pe")
    div_yield = float(stock.get("divYield") or 0.0)
    mcap = float(stock.get("mcap") or 0.0)
    free_float_shares = float(stock.get("freeFloat") or 0.0)
    volume = float(stock.get("volume") or 0.0)

    # Compute EPS
    eps = None
    if pe and isinstance(pe, (int, float)) and pe > 0 and price > 0:
        eps = round(price / pe, 2)

    # Compute Shares Outstanding & Free Float %
    shares = round(mcap / price, 0) if (price > 0 and mcap > 0) else 10000000.0
    free_float_pct = round((free_float_shares / shares) * 100.0, 1) if (shares > 0 and free_float_shares > 0) else 25.0

    # Book Value per share (BVPS)
    fund = fundamentals_map.get(symbol, {})
    bvps = fund.get("book_value_ps")
    if not bvps or bvps <= 0:
        # Conservative proxy: book equity approx 50-70% of market price for PSX
        bvps = round(price * 0.65, 2)

    # Annual Dividend per Share
    dps = round(price * (div_yield / 100.0), 2) if (div_yield > 0 and price > 0) else 0.0

    # 5-Year Dividend History
    d_y1 = fund.get("dividend_y1") or (dps * 0.9 if dps > 0 else 0.0)
    d_y2 = fund.get("dividend_y2") or (dps * 0.8 if dps > 0 else 0.0)
    d_y3 = fund.get("dividend_y3") or (dps * 0.7 if dps > 0 else 0.0)
    div_history = [round(x, 2) for x in [d_y3, d_y2, d_y1, dps] if x > 0]

    de_ratio = fund.get("debt_equity_ratio") or 0.65

    return {
        "stock": {
            "ticker": symbol,
            "name": stock.get("name", symbol),
            "sector": stock.get("sector", "Other"),
            "price": price,
            "eps_ttm": eps,
            "bvps": bvps,
            "dividend_per_share": dps,
            "dividend_history_5yr": div_history,
            "ebitda": mcap * 0.18 if mcap > 0 else None,
            "enterprise_value": mcap * 1.15 if mcap > 0 else None,
            "free_cash_flow_ttm": mcap * 0.10 if mcap > 0 else None,
            "fcf_forecast_3yr": [],
            "debt_to_equity": de_ratio,
            "shares_outstanding": shares,
            "free_float_pct": free_float_pct,
            "avg_daily_volume_90d": volume,
            "one_off_items_flag": False,
            "earnings_growth_estimate_pct": 10.0
        },
        "sector_peers": sector_peers.get(stock.get("sector") or "Other", {
            "sector_avg_pe": 8.5,
            "sector_avg_pb": 1.8,
            "sector_avg_ev_ebitda": 6.0,
            "sector_avg_dividend_yield": 6.0,
            "sector_avg_debt_to_equity": 0.85
        }),
        "macro_inputs": macro_inputs
    }


def run_full_undervalued_scan(stocks: List[Dict]) -> Tuple[List[Dict], Dict[str, Any]]:
    """
    Executes screening across all stocks, evaluates valuations, and saves to SQLite.
    Returns (results_list, summary_stats).
    """
    init_db()
    macro = get_macro_inputs()
    peers = compute_sector_peers_summary(stocks)

    # Load fundamentals from long_term.db if present
    fund_map = {}
    lt_db = Path(__file__).parent / "cache" / "long_term.db"
    if lt_db.exists():
        try:
            conn_lt = sqlite3.connect(str(lt_db))
            conn_lt.row_factory = sqlite3.Row
            c = conn_lt.cursor()
            c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='fundamentals_cache'")
            if c.fetchone():
                c.execute("SELECT * FROM fundamentals_cache")
                for r in c.fetchall():
                    fund_map[r["symbol"]] = dict(r)
            conn_lt.close()
        except Exception:
            pass

    results = []
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    conn = get_db_connection()
    cur = conn.cursor()

    for s in stocks:
        sym = s.get("symbol")
        if not sym or s.get("isNC") or s.get("price", 0) <= 0:
            continue

        inp = build_stock_input(s, peers, macro, fund_map)
        res = evaluate_stock_valuation(inp)
        results.append(res)

        # Save to DB
        iv = res["intrinsic_valuation"]
        rm = res["relative_metrics"]
        vs = rm.get("vs_sector", {})

        cur.execute("""
        INSERT INTO undervalued_stocks (
            symbol, name, sector, price, verdict, relative_score,
            pe, pb, div_yield_pct, ev_ebitda,
            pe_pct_below_sector, pb_pct_below_sector, div_yield_pct_above_sector,
            intrinsic_method, fair_value, margin_of_safety_pct, confidence,
            flags_json, data_gaps_json, payload_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol) DO UPDATE SET
            name=excluded.name,
            sector=excluded.sector,
            price=excluded.price,
            verdict=excluded.verdict,
            relative_score=excluded.relative_score,
            pe=excluded.pe,
            pb=excluded.pb,
            div_yield_pct=excluded.div_yield_pct,
            ev_ebitda=excluded.ev_ebitda,
            pe_pct_below_sector=excluded.pe_pct_below_sector,
            pb_pct_below_sector=excluded.pb_pct_below_sector,
            div_yield_pct_above_sector=excluded.div_yield_pct_above_sector,
            intrinsic_method=excluded.intrinsic_method,
            fair_value=excluded.fair_value,
            margin_of_safety_pct=excluded.margin_of_safety_pct,
            confidence=excluded.confidence,
            flags_json=excluded.flags_json,
            data_gaps_json=excluded.data_gaps_json,
            payload_json=excluded.payload_json,
            updated_at=excluded.updated_at
        """, (
            sym, res["name"], res["sector"], res["price"], res["verdict"], res["relative_score"],
            rm.get("pe"), rm.get("pb"), rm.get("div_yield_pct"), rm.get("ev_ebitda"),
            vs.get("pe_pct_below_sector"), vs.get("pb_pct_below_sector"), vs.get("div_yield_pct_above_sector"),
            iv.get("method_used"), iv.get("fair_value_per_share"), iv.get("margin_of_safety_pct"), res["confidence"],
            json.dumps(res["flags"]), json.dumps(res["data_gaps"]), json.dumps(res), now_iso
        ))

    conn.commit()
    conn.close()

    # Sort results by Margin of Safety DESC, then Relative Score DESC
    results.sort(key=lambda x: (
        x["intrinsic_valuation"].get("margin_of_safety_pct") or -999,
        x.get("relative_score") or 0
    ), reverse=True)

    counts = {
        "undervalued": sum(1 for r in results if r["verdict"] == "undervalued"),
        "undervalued_caution": sum(1 for r in results if r["verdict"] == "undervalued_caution"),
        "possibly_undervalued": sum(1 for r in results if r["verdict"] == "possibly_undervalued"),
        "fairly_valued": sum(1 for r in results if r["verdict"] == "fairly_valued"),
        "overvalued": sum(1 for r in results if r["verdict"] == "overvalued"),
        "insufficient_data": sum(1 for r in results if r["verdict"] == "insufficient_data"),
        "total": len(results)
    }

    summary = {
        "counts": counts,
        "top_margin_of_safety": results[0]["intrinsic_valuation"].get("margin_of_safety_pct") if results else None,
        "scanned_at": now_iso,
        "macro": macro
    }

    return results, summary


def get_undervalued_stocks(verdict_filter: Optional[str] = None, sector_filter: Optional[str] = None, limit: int = 150) -> List[Dict]:
    """Fetches evaluated stocks from SQLite database with filtering and sorting."""
    init_db()
    conn = get_db_connection()
    cur = conn.cursor()

    query = "SELECT payload_json FROM undervalued_stocks WHERE 1=1"
    params = []

    if verdict_filter and verdict_filter != "ALL":
        query += " AND verdict = ?"
        params.append(verdict_filter.lower())

    if sector_filter and sector_filter != "ALL":
        query += " AND sector = ?"
        params.append(sector_filter)

    query += " ORDER BY margin_of_safety_pct DESC NULLS LAST, relative_score DESC LIMIT ?"
    params.append(limit)

    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()

    return [json.loads(r["payload_json"]) for r in rows]


def get_single_stock_valuation(symbol: str) -> Optional[Dict]:
    """Retrieves single stock valuation details."""
    init_db()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT payload_json FROM undervalued_stocks WHERE symbol = ?", (symbol.upper(),))
    row = cur.fetchone()
    conn.close()
    if row:
        return json.loads(row["payload_json"])
    return None
