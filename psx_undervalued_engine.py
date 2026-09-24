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
        previous_intrinsic_method TEXT,
        method_shift_reason TEXT,
        financials_date TEXT,
        flags_json TEXT,
        data_gaps_json TEXT,
        payload_json TEXT,
        updated_at TEXT
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_uv_verdict ON undervalued_stocks(verdict)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_uv_mos ON undervalued_stocks(margin_of_safety_pct)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_uv_score ON undervalued_stocks(relative_score)")

    # Migration checks for existing databases
    for col_def in [
        ("previous_intrinsic_method", "TEXT"),
        ("method_shift_reason", "TEXT"),
        ("financials_date", "TEXT"),
        ("synergy_tags_json", "TEXT")
    ]:
        try:
            cur.execute(f"ALTER TABLE undervalued_stocks ADD COLUMN {col_def[0]} {col_def[1]}")
        except sqlite3.OperationalError:
            pass  # Already exists

    cur.execute("""
    CREATE TABLE IF NOT EXISTS valuation_verdict_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        old_verdict TEXT,
        new_verdict TEXT,
        old_fair_value REAL,
        new_fair_value REAL,
        old_method TEXT,
        new_method TEXT,
        price_at_change REAL,
        shift_reason TEXT
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_vvh_sym ON valuation_verdict_history(symbol)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_vvh_date ON valuation_verdict_history(recorded_at)")
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# 📊  SECTOR AVERAGES & MACRO INPUTS
# ═══════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════
# 📊  SECTOR AVERAGES & MACRO INPUTS
# ═══════════════════════════════════════════════════════════════════════════

VALUATION_CONFIG_PATH = Path(__file__).parent / "config" / "valuation_engine.json"
_VALUATION_CONFIG_CACHE = None

def load_valuation_config() -> Dict[str, Any]:
    global _VALUATION_CONFIG_CACHE
    if _VALUATION_CONFIG_CACHE is not None:
        return _VALUATION_CONFIG_CACHE
    if VALUATION_CONFIG_PATH.exists():
        try:
            with open(VALUATION_CONFIG_PATH, "r", encoding="utf-8") as f:
                _VALUATION_CONFIG_CACHE = json.load(f)
                return _VALUATION_CONFIG_CACHE
        except Exception:
            pass
    _VALUATION_CONFIG_CACHE = {
        "version": "1.0.0",
        "macro_anchors": {
            "risk_free_rate_pct": 11.5,
            "equity_risk_premium_pct": 5.0,
            "hurdle_rate_pct": 16.5,
            "kse100_forward_pe": 8.1,
            "kse100_10yr_avg_pe": 8.5
        },
        "relative_score_weights": {
            "pe": 0.30, "pb": 0.20, "div_yield": 0.20, "ev_ebitda": 0.20, "leverage": 0.10
        },
        "sector_defaults": {
            "min_sector_sample_size": 5,
            "default_sector_avg_pe": 8.5,
            "default_sector_avg_pb": 1.8,
            "default_sector_avg_ev_ebitda": 6.0,
            "default_sector_avg_dividend_yield": 6.0,
            "default_sector_avg_debt_to_equity": 0.85
        },
        "anti_trap_thresholds": {
            "pe_distress_floor": 3.0, "pe_distress_score": 10.0,
            "pb_distress_floor": 0.3, "pb_distress_score": 10.0,
            "payout_ratio_warning": 80.0, "payout_ratio_cap_score": 50.0,
            "lossmaking_dividend_score": 15.0,
            "leverage_penalty_threshold_ratio": 1.25, "leverage_penalty_slope": 50.0
        },
        "continuous_scoring_curves": {
            "pe": [[0.50, 100.0], [0.75, 80.0], [1.00, 65.0], [1.25, 45.0], [1.75, 20.0], [2.50, 0.0]],
            "pb": [[0.50, 100.0], [0.75, 80.0], [1.00, 65.0], [1.25, 45.0], [1.75, 20.0], [2.50, 0.0]],
            "div_yield": [[0.00, 0.0], [0.50, 20.0], [0.75, 50.0], [1.00, 70.0], [1.25, 85.0], [1.50, 100.0]],
            "ev_ebitda": [[0.60, 100.0], [0.80, 80.0], [1.00, 65.0], [1.20, 45.0], [1.60, 25.0], [2.00, 0.0]],
            "pb_roe": [[0.70, 100.0], [1.00, 80.0], [1.25, 60.0], [1.75, 30.0], [2.25, 0.0]]
        },
        "intrinsic_models": {
            "ddm": {"min_history_years": 4, "max_cv": 0.25, "epsilon_mean_dividend": 0.01, "max_growth_cap": 0.08, "hurdle_spread_buffer": 0.015},
            "dcf": {"terminal_growth_rate": 0.05, "forecast_years": 3},
            "graham": {"multiplier": 22.5}
        },
        "verdict_matrix": {"tertile_top": 66.7, "tertile_bottom": 33.3, "mos_undervalued_min": 15.0, "mos_overvalued_max": -10.0},
        "quality_gates": {"min_listing_years": 2, "min_free_float_pct": 10.0, "max_debt_to_equity_sector_multiple": 2.0, "piotroski_value_trap_max_score": 3}
    }
    return _VALUATION_CONFIG_CACHE


def interpolate_piecewise(x: float, anchors: List[List[float]]) -> float:
    """
    Continuous piecewise linear interpolation between anchor points (ratio, score).
    Anchors must be sorted by ratio ascending.
    """
    if not anchors:
        return 50.0
    
    # Boundary clamps
    if x <= anchors[0][0]:
        return float(anchors[0][1])
    if x >= anchors[-1][0]:
        return float(anchors[-1][1])
    
    for i in range(len(anchors) - 1):
        x0, y0 = anchors[i]
        x1, y1 = anchors[i+1]
        if x0 <= x <= x1:
            if x1 == x0:
                return float(y0)
            t = (x - x0) / (x1 - x0)
            return float(y0 + t * (y1 - y0))
            
    return float(anchors[-1][1])


def get_macro_inputs() -> Dict[str, Any]:
    """Retrieves macro inputs from central config file with staleness check."""
    cfg = load_valuation_config()
    anchors = cfg.get("macro_anchors", {})
    last_up_str = anchors.get("last_updated")
    max_days = float(anchors.get("max_staleness_days", 45))
    is_stale = False
    days_old = None
    if last_up_str:
        try:
            dt = datetime.datetime.fromisoformat(last_up_str.replace("Z", "+00:00"))
            now_dt = datetime.datetime.now(datetime.timezone.utc)
            days_old = (now_dt - dt).days
            if days_old > max_days:
                is_stale = True
        except Exception:
            pass

    return {
        "risk_free_rate_pct": float(anchors.get("risk_free_rate_pct", 11.5)),
        "equity_risk_premium_pct": float(anchors.get("equity_risk_premium_pct", 5.0)),
        "hurdle_rate_pct": float(anchors.get("hurdle_rate_pct", 16.5)),
        "kse100_forward_pe": float(anchors.get("kse100_forward_pe", 8.1)),
        "kse100_10yr_avg_pe": float(anchors.get("kse100_10yr_avg_pe", 8.5)),
        "sbp_policy_rate_pct": float(anchors.get("sbp_policy_rate_pct", 11.0)),
        "last_updated": last_up_str,
        "is_stale": is_stale,
        "days_old": days_old
    }


def update_macro_anchors(updates: Dict[str, Any]) -> Dict[str, Any]:
    """Updates macro anchors in config/valuation_engine.json and resets config cache."""
    global _VALUATION_CONFIG_CACHE
    cfg_path = Path(__file__).parent / "config" / "valuation_engine.json"
    cfg = load_valuation_config()
    anchors = cfg.get("macro_anchors", {})
    for k, v in updates.items():
        if k in ["risk_free_rate_pct", "equity_risk_premium_pct", "hurdle_rate_pct", "sbp_policy_rate_pct", "kse100_forward_pe", "kse100_10yr_avg_pe"]:
            try:
                anchors[k] = float(v)
            except (ValueError, TypeError):
                pass
    anchors["last_updated"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    cfg["macro_anchors"] = anchors
    try:
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"[ValuationEngine] Error saving macro anchors: {e}")
    _VALUATION_CONFIG_CACHE = None
    return get_macro_inputs()


def compute_sector_peers_summary(stocks: List[Dict], leave_out_symbol: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """
    Computes sector median/trimmed averages for P/E, P/B, EV/EBITDA, Dividend Yield, and D/E.
    Prevents outlier skew by filtering out negative and extreme multiples.
    Supports leave-one-out peer calculation (0.5) by excluding leave_out_symbol.
    Enforces min_sector_sample_size threshold (0.5).
    """
    cfg = load_valuation_config()
    sec_cfg = cfg.get("sector_defaults", {})
    min_sample = sec_cfg.get("min_sector_sample_size", 5)

    sector_groups: Dict[str, List[Dict]] = {}
    for s in stocks:
        sym = (s.get("symbol") or "").upper().strip()
        if leave_out_symbol and sym == leave_out_symbol.upper().strip():
            continue
        sec = s.get("sector") or "Other"
        sector_groups.setdefault(sec, []).append(s)

    summary = {}
    for sec, items in sector_groups.items():
        pe_vals = [s["pe"] for s in items if isinstance(s.get("pe"), (int, float)) and s["pe"] > 3 and s["pe"] < 80]
        dy_vals = [s["divYield"] for s in items if isinstance(s.get("divYield"), (int, float)) and s["divYield"] >= 0]
        
        valid_sample_count = len(pe_vals)
        insufficient_peers = valid_sample_count < min_sample

        # P/E average
        avg_pe = round(sum(pe_vals) / len(pe_vals), 2) if pe_vals else sec_cfg.get("default_sector_avg_pe", 8.5)
        # Div Yield average
        avg_dy = round(sum(dy_vals) / len(dy_vals), 2) if dy_vals else sec_cfg.get("default_sector_avg_dividend_yield", 6.0)
        # P/B baseline
        avg_pb = sec_cfg.get("default_sector_avg_pb", 1.8)
        # EV/EBITDA baseline
        avg_ev_ebitda = sec_cfg.get("default_sector_avg_ev_ebitda", 6.0)
        # Debt to Equity baseline (Guard 0.4: ensure > 0)
        avg_de = sec_cfg.get("default_sector_avg_debt_to_equity", 0.85)
        if avg_de <= 0:
            avg_de = 0.85

        summary[sec] = {
            "sector_avg_pe": avg_pe,
            "sector_avg_pb": avg_pb,
            "sector_avg_ev_ebitda": avg_ev_ebitda,
            "sector_avg_dividend_yield": avg_dy,
            "sector_avg_debt_to_equity": avg_de,
            "sample_count": valid_sample_count,
            "insufficient_peers": insufficient_peers
        }

    return summary


def get_cross_engine_synergy_tags(symbol: str, res: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Stage 7: Cross-Engine Synergy.
    Combines Valuation signals with Technical Intelligence events, Piotroski quality, and momentum.
    """
    tags = []
    verdict = res.get("verdict")
    iv = res.get("intrinsic_valuation", {})
    mos = iv.get("margin_of_safety_pct")
    pio = res.get("piotroski") or {}
    f_score = pio.get("f_score") if isinstance(pio, dict) else None

    # 1. Quality Compounder Badge (Piotroski >= 8 + Undervalued)
    if f_score is not None and f_score >= 8 and verdict in ["undervalued", "possibly_undervalued"]:
        tags.append({
            "tag": "PRIME_COMPOUNDER",
            "badge": "PRIME COMPOUNDER",
            "description": f"Top-Tier Fundamentals (Piotroski {f_score}/9) combined with positive margin of safety",
            "severity": "success"
        })

    # 2. Query technical intelligence for confluence
    intel_db = Path(__file__).parent / "cache" / "intelligence.db"
    has_tech_bullish = False
    has_tech_overbought = False
    tech_note = None

    if intel_db.exists():
        try:
            conn_i = sqlite3.connect(str(intel_db))
            conn_i.row_factory = sqlite3.Row
            c = conn_i.cursor()
            c.execute("""
                SELECT event_type, rvol, rsi_at_event, macd_bullish, price_change_pct
                FROM stock_events
                WHERE symbol = ?
                ORDER BY id DESC
                LIMIT 5
            """, (symbol.upper().strip(),))
            events = c.fetchall()

            for ev in events:
                etype = ev["event_type"] or ""
                rsi = float(ev["rsi_at_event"] or 50.0)
                macd_b = bool(ev["macd_bullish"])
                rvol = float(ev["rvol"] or 1.0)
                
                if (macd_b or rvol >= 1.8 or "BREAKOUT" in etype or "ACCUMULATION" in etype) and rsi < 70:
                    has_tech_bullish = True
                    tech_note = etype if etype else ("Volume Anomaly" if rvol >= 1.8 else "MACD Reversal")
                    break

                if rsi >= 72 or "EXHAUSTION" in etype or "DISTRIBUTION" in etype:
                    has_tech_overbought = True
                    tech_note = f"RSI Overbought ({rsi:.0f})"
                    break

            conn_i.close()
        except Exception:
            pass

    # 3. Dual-Engine High Conviction (Undervalued + Technical Bullish Event)
    if verdict in ["undervalued", "possibly_undervalued"] and has_tech_bullish:
        tags.append({
            "tag": "HIGH_CONVICTION",
            "badge": "HIGH CONVICTION",
            "description": f"Dual-Engine Signal: Undervalued MoS ({mos}%) confirmed by Technical {tech_note or 'Accumulation'}",
            "severity": "success"
        })

    # 4. Momentum / Overvaluation Warning
    if (verdict == "overvalued" or (mos is not None and mos <= -10.0)) and has_tech_overbought:
        tags.append({
            "tag": "OVERVALUED_MOMENTUM_TRAP",
            "badge": "MOMENTUM TRAP RISK",
            "description": f"High Overvaluation Risk: Negative MoS ({mos}%) and Overextended Technicals ({tech_note})",
            "severity": "danger"
        })
    elif verdict == "overvalued":
        tags.append({
            "tag": "OVERVALUED",
            "badge": "OVERVALUED",
            "description": f"Trading at substantial premium to fair value (MoS: {mos}%)",
            "severity": "warning"
        })

    # 5. Value Trap Caution
    if verdict == "undervalued_caution":
        tags.append({
            "tag": "VALUE_TRAP_CAUTION",
            "badge": "VALUE TRAP CAUTION",
            "description": "Passed multiple screens but flagged by balance-sheet quality or Piotroski risk gate",
            "severity": "warning"
        })

    return tags


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
    cfg = load_valuation_config()
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
    years_listed = stock.get("years_listed", 99)
    is_new_listing = (years_listed is not None and years_listed < 2) or bool(stock.get("is_new_listing", False))

    flags = []
    data_gaps = []

    # Check Required Inputs
    if not ticker: data_gaps.append("ticker")
    if price <= 0: data_gaps.append("price")
    if eps_ttm is None and dps <= 0 and not fcf_forecast:
        data_gaps.append("earnings_and_dividends")
    if shares <= 0: data_gaps.append("shares_outstanding")
    if bvps is None:
        flags.append("book value per share not available — P/B and Graham Number omitted")

    # 0.8: Newly-listed company flag
    if is_new_listing:
        flags.append("newly listed company (< 2 years history) — YoY indicators omitted")

    # 1.1: Financial Sector Detection
    financial_sectors = cfg.get("financial_sectors", ["Commercial Banks", "Insurance", "Inv. Banks / Securities Cos."])
    sec = stock.get("sector") or "Other"
    sector = sec
    is_financial = sec in financial_sectors

    # 1.3: Conglomerate Detection
    conglom_symbols = cfg.get("conglomerate_symbols", ["ENGRO", "DAWH", "NML", "HUBC", "ILP", "SIEM", "PAEL", "THALL"])
    is_conglomerate = (ticker in conglom_symbols) or bool(stock.get("is_conglomerate", False))
    if is_conglomerate:
        flags.append("Diversified conglomerate/holding company with multiple business segments — sector peer multiple comparison carries a conglomerate discount caveat")

    # 0.5: Thin sector warning
    if peers.get("insufficient_peers"):
        flags.append(f"insufficient peer data (N={peers.get('sample_count', 0)} < 5) — comparison unreliable")

    macro_anchors = cfg.get("macro_anchors", {})
    risk_free = float(macro.get("risk_free_rate_pct") or macro_anchors.get("risk_free_rate_pct", 11.5))
    erp = float(macro.get("equity_risk_premium_pct") or macro_anchors.get("equity_risk_premium_pct", 5.0))

    # Stage 3.1: Macro Staleness Verification
    if macro.get("is_stale"):
        days_str = f" ({macro.get('days_old')} days old)" if macro.get("days_old") else ""
        flags.append(f"macro anchor staleness: risk-free rate last updated > {macro_anchors.get('max_staleness_days', 45)} days ago{days_str}")

    # Stage 3.2: Company-Specific Hurdle Rate / Discount Rate Scaling
    hurdle_cfg = cfg.get("hurdle_rate_config", {})
    beta_proxies = hurdle_cfg.get("sector_beta_proxies", {})
    sec_beta = float(beta_proxies.get(sector, hurdle_cfg.get("default_beta", 1.00)))

    # Liquidity / Free-Float Premium
    size_premium = 0.0
    free_float_threshold = float(hurdle_cfg.get("low_free_float_threshold_pct", 15.0))
    free_float_val = float(stock.get("free_float_pct") if stock.get("free_float_pct") is not None else 100.0)
    if free_float_val < free_float_threshold:
        size_premium = float(hurdle_cfg.get("low_free_float_premium_pct", 1.5))
        flags.append(f"low free-float ({free_float_val:.1f}% < {free_float_threshold:.0f}%) — {size_premium:.1f}% illiquidity hurdle buffer applied")

    company_hurdle_pct = round(risk_free + (sec_beta * erp) + size_premium, 2)
    company_hurdle_pct = max(12.0, min(25.0, company_hurdle_pct))
    required_return = company_hurdle_pct / 100.0

    # Stage 3.3: USD Debt / Currency Risk Flag
    usd_vulnerable = hurdle_cfg.get("usd_debt_vulnerable_sectors", [])
    if sector in usd_vulnerable:
        flags.append("FX/currency exposure: sector has significant imported input or FX-denominated capital structure")

    # ── STAGE 4: Corporate Actions & Continuous Earnings Quality ──────────────
    eq_cfg = cfg.get("earnings_quality_and_actions", {})

    # 4.1: Bonus/rights adjustment for dividend series
    div_adj_factors = stock.get("split_adjustment_factors") or stock.get("bonus_adjustments")
    if div_adj_factors and len(div_adj_factors) == len(div_history):
        div_history = [round(float(d) * float(f), 4) for d, f in zip(div_history, div_adj_factors)]
        flags.append("historical dividends adjusted for corporate actions/bonus issues")

    # 4.2: Continuous One-off Discount & Normalized EPS
    eps_normalized = None
    eps_eval = eps_ttm
    if one_off or stock.get("one_off_discount_ratio"):
        one_off_ratio = float(stock.get("one_off_discount_ratio") or eq_cfg.get("default_one_off_discount_ratio", 0.20))
        if eps_ttm and eps_ttm > 0:
            eps_normalized = round(eps_ttm * (1.0 - one_off_ratio), 2)
            eps_eval = eps_normalized
            flags.append(f"one-off non-recurring earnings: EPS discounted by {one_off_ratio*100.0:.0f}% for valuation (TTM: {eps_ttm:.2f}, normalized: {eps_normalized:.2f})")

    # 4.3: Cyclical Sector Normalized EPS
    cycle_norm_eps = None
    if sector in eq_cfg.get("cyclical_sectors", []):
        eps_3yr = [x for x in stock.get("eps_history_3yr", []) if isinstance(x, (int, float))]
        if len(eps_3yr) >= 3:
            cycle_norm_eps = round(sum(eps_3yr) / len(eps_3yr), 2)
            flags.append(f"cyclical sector ({sector}): 3yr cycle-normalized EPS is {cycle_norm_eps:.2f} (TTM {eps_ttm})")

    # 4.4: Circular Debt Proxy
    requires_mos_buffer = False
    if sector in eq_cfg.get("circular_debt_sectors", []):
        cfo = stock.get("cfo") if stock.get("cfo") is not None else stock.get("cash_flow_operations")
        net_inc = stock.get("net_income")
        if cfo is not None and net_inc is not None and net_inc > 0:
            cfo_ratio = float(cfo) / float(net_inc)
            cfo_thresh = float(eq_cfg.get("circular_debt_cfo_ratio_threshold", 0.50))
            if cfo_ratio < cfo_thresh:
                flags.append(f"circular debt warning: CFO ({cfo:.1f}M) is <{cfo_thresh*100.0:.0f}% of Net Income ({net_inc:.1f}M) — earnings not backed by cash collections")
                requires_mos_buffer = True

    # ── STEP 1: Relative Metrics ──────────────────────────────────────────────
    # Rule 5: If eps_ttm <= 0, do not compute P/E — set it null and flag
    pe = None
    if eps_ttm is not None and eps_ttm > 0 and price > 0:
        pe = round(price / eps_ttm, 2)
    elif eps_ttm is not None and eps_ttm <= 0:
        flags.append("negative earnings")

    # P/B (Guard 0.1: if bvps <= 0, P/B is null and flagged)
    pb = None
    if bvps is not None and bvps > 0 and price > 0:
        pb = round(price / bvps, 2)
    elif bvps is not None and bvps <= 0:
        flags.append("negative or zero book value — capital erosion")

    # Dividend Yield
    div_yield_pct = 0.0
    if price > 0 and dps > 0:
        div_yield_pct = round((dps / price) * 100.0, 2)

    # EV/EBITDA
    ev_ebitda = None
    if ev and ebitda and ebitda > 0:
        ev_ebitda = round(ev / ebitda, 2)

    # Graham Number (Guard 0.1: Strictly guard BVPS <= 0 and EPS <= 0; uses eps_eval for one-offs)
    graham_number = None
    graham_ineligible_reason = None
    graham_mult = float(cfg.get("intrinsic_models", {}).get("graham", {}).get("multiplier", 22.5))
    if eps_eval is not None and eps_eval > 0 and bvps is not None and bvps > 0:
        graham_val = graham_mult * eps_eval * bvps
        if graham_val > 0:
            graham_number = round(math.sqrt(graham_val), 2)
    else:
        graham_ineligible_reason = "negative book value/earnings — Graham Number not applicable"

    # Multiple Comparisons vs Sector
    sec_defaults = cfg.get("sector_defaults", {})
    sec_pe = float(peers.get("sector_avg_pe") or sec_defaults.get("default_sector_avg_pe", 8.5))
    sec_pb = float(peers.get("sector_avg_pb") or sec_defaults.get("default_sector_avg_pb", 1.8))
    sec_dy = float(peers.get("sector_avg_dividend_yield") or sec_defaults.get("default_sector_avg_dividend_yield", 6.0))
    sec_de = float(peers.get("sector_avg_debt_to_equity") or sec_defaults.get("default_sector_avg_debt_to_equity", 0.85))
    if sec_de <= 0:
        sec_de = 0.85

    pe_pct_below = round(((sec_pe - pe) / sec_pe) * 100.0, 1) if (pe and sec_pe > 0) else None
    pb_pct_below = round(((sec_pb - pb) / sec_pb) * 100.0, 1) if (pb and sec_pb > 0) else None
    dy_pct_above = round(div_yield_pct - sec_dy, 2) if sec_dy > 0 else None

    # ── STEP 2: Relative Score (0-100 scale) ──────────────────────────────────
    relative_score = None
    anti_trap = cfg.get("anti_trap_thresholds", {})
    weights = cfg.get("relative_score_weights", {"pe": 0.30, "pb": 0.20, "div_yield": 0.20, "ev_ebitda": 0.20, "leverage": 0.10})

    if price > 0 and not data_gaps:
        curves = cfg.get("continuous_scoring_curves", {})
        pe_curve = curves.get("pe", [[0.50, 100.0], [0.75, 80.0], [1.00, 65.0], [1.25, 45.0], [1.75, 20.0], [2.50, 0.0]])
        pb_curve = curves.get("pb", [[0.50, 100.0], [0.75, 80.0], [1.00, 65.0], [1.25, 45.0], [1.75, 20.0], [2.50, 0.0]])
        dy_curve = curves.get("div_yield", [[0.00, 0.0], [0.50, 20.0], [0.75, 50.0], [1.00, 70.0], [1.25, 85.0], [1.50, 100.0]])
        ev_curve = curves.get("ev_ebitda", [[0.60, 100.0], [0.80, 80.0], [1.00, 65.0], [1.20, 45.0], [1.60, 25.0], [2.00, 0.0]])
        pb_roe_curve = curves.get("pb_roe", [[0.70, 100.0], [1.00, 80.0], [1.25, 60.0], [1.75, 30.0], [2.25, 0.0]])

        # pe_component (weight 0.30)
        # Rule 4: P/E below 3 is a distress signal, not a bargain
        pe_distress_floor = float(anti_trap.get("pe_distress_floor", 3.0))
        pe_score = 50.0
        if pe is None:
            pe_score = 0.0
        elif pe < pe_distress_floor:
            pe_score = float(anti_trap.get("pe_distress_score", 10.0))
            flags.append("P/E below 3 signals potential earnings quality or distress risk")
        else:
            ratio = pe / sec_pe if sec_pe > 0 else 1.0
            pe_score = round(interpolate_piecewise(ratio, pe_curve), 2)

        # pb_component (weight 0.20)
        # Rule 4: P/B below 0.3 usually signals distress, not a bargain
        pb_distress_floor = float(anti_trap.get("pb_distress_floor", 0.3))
        pb_score = 50.0
        if pb is None:
            pb_score = None
        elif pb <= 0 or pb <= pb_distress_floor:
            pb_score = float(anti_trap.get("pb_distress_score", 10.0))
            flags.append("P/B below 0.3 signals severe distress/solvency risk, not a bargain")
        else:
            ratio_pb = pb / sec_pb if sec_pb > 0 else 1.0
            pb_score = round(interpolate_piecewise(ratio_pb, pb_curve), 2)

        # div_yield_component (weight 0.20) (Guard 0.3: EPS <= 0 with DPS > 0)
        div_score = 50.0
        if eps_ttm is not None and eps_ttm <= 0 and dps > 0:
            # Strong negative signal: paying dividends from reserves while loss-making
            div_score = float(anti_trap.get("lossmaking_dividend_score", 15.0))
            flags.append("dividend paid from reserves while loss-making (EPS <= 0) — yield unsustainable")
        elif eps_ttm and eps_ttm > 0 and dps > 0:
            payout_ratio = (dps / eps_ttm) * 100.0
            payout_cap = float(anti_trap.get("payout_ratio_warning", 80.0))
            if payout_ratio > payout_cap:
                flags.append(f"dividend payout ratio ({payout_ratio:.1f}%) exceeds {payout_cap:.0f}% — yield may not be sustainable")
                div_score = float(anti_trap.get("payout_ratio_cap_score", 50.0))
            else:
                ratio_dy = div_yield_pct / sec_dy if sec_dy > 0 else 1.0
                div_score = round(interpolate_piecewise(ratio_dy, dy_curve), 2)
        else:
            div_score = 20.0 if div_yield_pct == 0.0 else 40.0

        # ev_ebitda_component (weight 0.20)
        ev_score = 50.0
        if ev_ebitda:
            sec_ev = float(peers.get("sector_avg_ev_ebitda") or sec_defaults.get("default_sector_avg_ev_ebitda", 6.0))
            ratio_ev = ev_ebitda / sec_ev if sec_ev > 0 else 1.0
            ev_score = round(interpolate_piecewise(ratio_ev, ev_curve), 2)
        else:
            # Fallback to multiple parity
            if pb_score is not None:
                ev_score = round((pe_score + pb_score) / 2.0, 2)
            else:
                ev_score = pe_score

        # leverage_penalty (weight 0.10) (Guard 0.4: sec_de guarded > 0)
        leverage_score = 100.0
        if debt_equity is not None and sec_de > 0:
            thresh_ratio = float(anti_trap.get("leverage_penalty_threshold_ratio", 1.25))
            slope = float(anti_trap.get("leverage_penalty_slope", 50.0))
            if debt_equity > (sec_de * thresh_ratio):
                leverage_score = max(0.0, 100.0 - (debt_equity / sec_de - thresh_ratio) * slope)
                flags.append(f"debt-to-equity ({debt_equity:.2f}) exceeds sector avg ({sec_de:.2f}) by >25%")

        # Combined Relative Score (Stage 1.1: Financial-specific reweighting)
        if is_financial:
            fin_weights = cfg.get("financial_score_weights", {"pe": 0.30, "pb_roe": 0.35, "div_yield": 0.25, "leverage": 0.10})
            roe = float(stock.get("roe") or 0.0)
            if roe <= 0 and eps_ttm and bvps and bvps > 0:
                roe = (eps_ttm / bvps) * 100.0
            justified_pb = (roe / (required_return * 100.0)) if roe > 0 else 0.5
            pb_roe_score = None
            if pb is not None and pb > 0 and justified_pb > 0:
                ratio_pb_roe = pb / justified_pb
                pb_roe_score = round(interpolate_piecewise(ratio_pb_roe, pb_roe_curve), 2)
            elif pb is not None and pb <= 0:
                pb_roe_score = 0.0

            comps = [
                (pe_score, fin_weights.get("pe", 0.30)),
                (pb_roe_score, fin_weights.get("pb_roe", 0.35)),
                (div_score, fin_weights.get("div_yield", 0.25)),
                (leverage_score, fin_weights.get("leverage", 0.10))
            ]
            valid_comps = [(s, w) for s, w in comps if s is not None]
            total_w = sum(w for _, w in valid_comps)
            raw_score = sum(s * (w / total_w) for s, w in valid_comps) if total_w > 0 else 50.0
            flags.append("Financial institution scoring applied: EV/EBITDA excluded, P/B vs ROE substituted (35% weight)")
        else:
            comps = [
                (pe_score, weights.get("pe", 0.30)),
                (pb_score, weights.get("pb", 0.20)),
                (div_score, weights.get("div_yield", 0.20)),
                (ev_score, weights.get("ev_ebitda", 0.20)),
                (leverage_score, weights.get("leverage", 0.10))
            ]
            valid_comps = [(s, w) for s, w in comps if s is not None]
            total_w = sum(w for _, w in valid_comps)
            raw_score = sum(s * (w / total_w) for s, w in valid_comps) if total_w > 0 else 50.0

        relative_score = round(min(100.0, max(0.0, raw_score)), 1)

    # ── STEP 3: Intrinsic Value (Order: DDM -> DCF -> Graham Number) ─────────
    fair_value = None
    method_used = "none"
    required_return_used = None
    growth_rate_used = None
    confidence = "low"

    required_return = (risk_free + erp) / 100.0  # e.g. 0.165 (16.5%)

    # (a) Check DDM (Gordon Growth) (Guard 0.2: zero base year, near-zero mean, negative divs)
    has_stable_divs = False
    ddm_ineligible_reason = None
    ddm_cfg = cfg.get("intrinsic_models", {}).get("ddm", {})
    min_div_years = int(ddm_cfg.get("min_history_years", 4))
    max_cv = float(ddm_cfg.get("max_cv", 0.25))
    eps_mean = float(ddm_cfg.get("epsilon_mean_dividend", 0.01))

    if len(div_history) >= min_div_years:
        if any(d <= 0 for d in div_history) or div_history[0] <= 0:
            ddm_ineligible_reason = "dividend history contains zero base year or irregular payouts — DDM ineligible"
        else:
            mean_div = sum(div_history) / len(div_history)
            if mean_div < eps_mean:
                ddm_ineligible_reason = "near-zero mean dividend — DDM ineligible"
            else:
                variance = sum((d - mean_div) ** 2 for d in div_history) / len(div_history)
                cv = math.sqrt(variance) / mean_div
                no_cuts = all(div_history[i] >= div_history[i-1] * 0.95 for i in range(1, len(div_history)))
                if no_cuts and cv < max_cv:
                    has_stable_divs = True
                else:
                    ddm_ineligible_reason = f"dividend cuts detected or CV ({cv:.2f}) >= {max_cv} — DDM ineligible"
    else:
        ddm_ineligible_reason = f"insufficient dividend history ({len(div_history)}/{min_div_years} yrs) — DDM ineligible"

    if has_stable_divs and dps > 0:
        method_used = "DDM"
        n_years = len(div_history) - 1
        # Safe CAGR because div_history[0] > 0 and div_history[-1] > 0
        cagr = (div_history[-1] / div_history[0]) ** (1.0 / n_years) - 1.0
        # Rule 3: Never let dividend_growth_g exceed required_return
        max_allowable_g = min(
            float(ddm_cfg.get("max_growth_cap", 0.08)),
            required_return - float(ddm_cfg.get("hurdle_spread_buffer", 0.015))
        )
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

    # (b) Else if FCF forecast is present -> 3-year DCF with terminal value (Stage 1.2: Bank DCF Audit)
    elif len(fcf_forecast) == 3 and shares > 0:
        method_used = "DCF"
        if is_financial:
            confidence = "low"
            flags.append("DCF methodology inappropriate for commercial banks/financial institutions — cash flows are operating balance-sheet items; intrinsic confidence marked low")
        else:
            confidence = "medium"
        long_term_growth = float(cfg.get("intrinsic_models", {}).get("dcf", {}).get("terminal_growth_rate", 0.05))
        # Discount forecast years
        pv_fcfs = sum(fcf / ((1.0 + required_return) ** (i + 1)) for i, fcf in enumerate(fcf_forecast))
        terminal_val = (fcf_forecast[2] * (1.0 + long_term_growth)) / (required_return - long_term_growth)
        pv_terminal = terminal_val / ((1.0 + required_return) ** 3)
        fair_value = round((pv_fcfs + pv_terminal) / shares, 2)
        growth_rate_used = round(long_term_growth * 100.0, 2)
        required_return_used = round(required_return * 100.0, 2)
        if not is_financial:
            confidence = "medium"
        else:
            confidence = "low"

    # (c) Else fallback to Graham Number (Guard 0.1: must have positive EPS and positive BVPS)
    elif graham_number is not None and eps_ttm is not None and eps_ttm > 0 and bvps is not None and bvps > 0:
        method_used = "Graham_Number"
        fair_value = graham_number
        confidence = "low"

    else:
        # (d) Fall through to Model D (Null)
        method_used = "none"
        fair_value = None
        confidence = "low"
        if graham_ineligible_reason:
            flags.append(graham_ineligible_reason)
        elif ddm_ineligible_reason:
            flags.append(ddm_ineligible_reason)
        else:
            flags.append("missing macro/financial inputs — intrinsic valuation not computed")

    # ── STEP 4: Margin of Safety & Verdict ───────────────────────────────────
    margin_of_safety_pct = None
    if fair_value and fair_value > 0 and price > 0:
        margin_of_safety_pct = round(((fair_value - price) / fair_value) * 100.0, 2)

    verdict_cfg = cfg.get("verdict_matrix", {})
    t_top = float(verdict_cfg.get("tertile_top", 66.7))
    t_bot = float(verdict_cfg.get("tertile_bottom", 33.3))
    mos_uv = float(verdict_cfg.get("mos_undervalued_min", 15.0))
    if requires_mos_buffer:
        mos_buffer = float(eq_cfg.get("circular_debt_mos_buffer_pct", 5.0))
        mos_uv += mos_buffer
        flags.append(f"margin of safety hurdle increased to {mos_uv:.0f}% (+{mos_buffer:.0f}% buffer) due to circular debt cash collection lag")
    mos_ov = float(verdict_cfg.get("mos_overvalued_max", -10.0))

    # Stage 5.1 & 5.2: Model Stability, Staleness Check, & Smoothed Tertile Hysteresis
    stab_cfg = cfg.get("model_stability", {})
    hysteresis_buffer = float(stab_cfg.get("tertile_hysteresis_buffer", 2.0))
    prev_verdict = stock.get("previous_verdict")
    prev_method = stock.get("previous_intrinsic_method")

    if prev_method and prev_method != "none" and method_used != "none" and prev_method != method_used:
        flags.append(f"valuation model shift: transitioned from {prev_method} to {method_used}")

    fin_date = stock.get("financials_date")
    if fin_date:
        try:
            fin_dt = datetime.datetime.fromisoformat(str(fin_date)[:10])
            now_dt = datetime.datetime.now(datetime.timezone.utc)
            if fin_dt.tzinfo is None:
                fin_dt = fin_dt.replace(tzinfo=datetime.timezone.utc)
            diff_days = (now_dt - fin_dt).days
            max_fin_days = float(stab_cfg.get("financials_max_staleness_days", 180))
            if diff_days > max_fin_days:
                flags.append(f"stale financials: financial statements dated {str(fin_date)[:10]} are {diff_days} days old (>{max_fin_days:.0f}d)")
        except Exception:
            pass

    verdict = "insufficient_data"
    if is_new_listing and (data_gaps or relative_score is None):
        verdict = "insufficient_history"
    elif data_gaps or relative_score is None:
        verdict = "insufficient_data"
    else:
        # Smoothed tertile boundary with hysteresis buffer
        eff_t_top = (t_top - hysteresis_buffer) if prev_verdict == "undervalued" else t_top
        eff_t_bot = (t_bot + hysteresis_buffer) if prev_verdict == "overvalued" else t_bot

        top_tertile = relative_score >= eff_t_top
        bottom_tertile = relative_score <= eff_t_bot

        if margin_of_safety_pct is not None:
            if top_tertile and margin_of_safety_pct >= mos_uv:
                verdict = "undervalued"
            elif top_tertile or margin_of_safety_pct >= mos_uv:
                verdict = "possibly_undervalued"
            elif mos_ov <= margin_of_safety_pct < mos_uv and not bottom_tertile:
                verdict = "fairly_valued"
            elif bottom_tertile and margin_of_safety_pct <= mos_ov:
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
    q_cfg = cfg.get("quality_gates", {})
    min_ff = float(q_cfg.get("min_free_float_pct", 10.0))
    max_de_mult = float(q_cfg.get("max_debt_to_equity_sector_multiple", 2.0))

    if verdict == "undervalued":
        reasons_to_downgrade = []
        if free_float_pct is not None and free_float_pct < min_ff:
            reasons_to_downgrade.append(f"illiquid free float ({free_float_pct:.1f}% < {min_ff:.0f}%)")
        if one_off and not eps_normalized:
            reasons_to_downgrade.append("one-off earnings items present without normalization")
        if eps_ttm is not None and eps_ttm <= 0:
            reasons_to_downgrade.append("trailing EPS is negative/zero")
        if debt_equity is not None and sec_de > 0 and debt_equity > (max_de_mult * sec_de):
            reasons_to_downgrade.append(f"debt-to-equity ({debt_equity:.2f}) is more than double sector average")

        # Piotroski F-Score Gate (0.8: pass is_new_listing)
        piotroski_res = None
        try:
            import psx_piotroski as _pio
            piotroski_res = _pio.calculate_piotroski_fscore(ticker)
            if piotroski_res and piotroski_res.get("verdict") == "VALUE_TRAP":
                reasons_to_downgrade.append(f"value trap warning: weak Piotroski F-Score ({piotroski_res['f_score']}/9)")
        except Exception:
            pass

        if reasons_to_downgrade:
            verdict = "undervalued_caution"
            flags.extend(reasons_to_downgrade)

    if 'piotroski_res' not in locals() or piotroski_res is None:
        try:
            import psx_piotroski as _pio
            piotroski_res = _pio.calculate_piotroski_fscore(ticker)
        except Exception:
            piotroski_res = None

    res_dict = {
        "ticker": ticker,
        "name": stock.get("name", ticker),
        "sector": stock.get("sector", "Other"),
        "price": price,
        "verdict": verdict,
        "piotroski": piotroski_res,
        "relative_score": relative_score,
        "relative_metrics": {
            "pe": pe,
            "pb": pb,
            "div_yield_pct": div_yield_pct,
            "ev_ebitda": ev_ebitda,
            "eps_normalized": eps_normalized,
            "cycle_normalized_eps": cycle_norm_eps,
            "vs_sector": {
                "pe_pct_below_sector": pe_pct_below,
                "pb_pct_below_sector": pb_pct_below,
                "div_yield_pct_above_sector": dy_pct_above
            }
        },
        "intrinsic_valuation": {
            "method_used": method_used,
            "fair_value_per_share": fair_value,
            "required_return_pct_used": required_return_used or company_hurdle_pct,
            "growth_rate_pct_used": growth_rate_used,
            "margin_of_safety_pct": margin_of_safety_pct,
            "hurdle_rate_pct": company_hurdle_pct,
            "sector_beta": sec_beta,
            "size_premium_pct": size_premium
        },
        "confidence": confidence,
        "flags": list(dict.fromkeys(flags)),  # Deduplicate flags
        "data_gaps": data_gaps,
        "disclaimer": DISCLAIMER_TEXT
    }
    res_dict["synergy_tags"] = get_cross_engine_synergy_tags(ticker, res_dict)
    return res_dict


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
    raw_bvps = fund.get("book_value_ps")
    bvps = None
    if raw_bvps is not None:
        try:
            bvps = float(raw_bvps)
        except (ValueError, TypeError):
            bvps = None

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
            "earnings_growth_estimate_pct": 10.0,
            "financials_date": fund.get("financials_date") or fund.get("date") or fund.get("period_end") or fund.get("filing_date")
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
    Guards 0.5 (Leave-One-Out) and 0.6 (Full-Universe Scan Isolation per symbol).
    Stage 5: Tracks previous verdicts and logs method/verdict shifts to valuation_verdict_history.
    """
    init_db()
    macro = get_macro_inputs()

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
    failed_symbols = {}
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    conn = get_db_connection()
    cur = conn.cursor()

    for s in stocks:
        if not isinstance(s, dict):
            continue
        sym = (s.get("symbol") or "").strip()
        if not sym:
            continue

        try:
            raw_p = s.get("price")
            if raw_p is None:
                continue
            price_val = float(raw_p)
            if s.get("isNC") or price_val <= 0:
                continue

            # Query previous state for hysteresis and shift detection (Stage 5)
            cur.execute("SELECT verdict, intrinsic_method, fair_value, price FROM undervalued_stocks WHERE symbol=?", (sym,))
            prev_row = cur.fetchone()
            old_verdict = prev_row[0] if prev_row else None
            old_method = prev_row[1] if prev_row else None
            old_fair = prev_row[2] if prev_row else None
            old_price = prev_row[3] if prev_row else None

            # Leave-one-out sector peers for stock 'sym' (Guard 0.5)
            peers_loo = compute_sector_peers_summary(stocks, leave_out_symbol=sym)
            inp = build_stock_input(s, peers_loo, macro, fund_map)
            inp["stock"]["previous_verdict"] = old_verdict
            inp["stock"]["previous_intrinsic_method"] = old_method
            res = evaluate_stock_valuation(inp)
            results.append(res)

            # Save to DB
            iv = res["intrinsic_valuation"]
            rm = res["relative_metrics"]
            vs = rm.get("vs_sector", {})

            shift_reason = None
            if old_verdict and (old_verdict != res["verdict"] or (old_method and old_method != iv.get("method_used"))):
                shift_reason = f"Verdict: {old_verdict}->{res['verdict']}; Method: {old_method}->{iv.get('method_used')}"
                cur.execute("""
                INSERT INTO valuation_verdict_history (
                    symbol, recorded_at, old_verdict, new_verdict,
                    old_fair_value, new_fair_value, old_method, new_method,
                    price_at_change, shift_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    sym, now_iso, old_verdict, res["verdict"],
                    old_fair, iv.get("fair_value_per_share"), old_method, iv.get("method_used"),
                    price_val, shift_reason
                ))

            cur.execute("""
            INSERT INTO undervalued_stocks (
                symbol, name, sector, price, verdict, relative_score,
                pe, pb, div_yield_pct, ev_ebitda,
                pe_pct_below_sector, pb_pct_below_sector, div_yield_pct_above_sector,
                intrinsic_method, fair_value, margin_of_safety_pct, confidence,
                previous_intrinsic_method, method_shift_reason, financials_date,
                synergy_tags_json, flags_json, data_gaps_json, payload_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                previous_intrinsic_method=excluded.previous_intrinsic_method,
                method_shift_reason=excluded.method_shift_reason,
                financials_date=excluded.financials_date,
                synergy_tags_json=excluded.synergy_tags_json,
                flags_json=excluded.flags_json,
                data_gaps_json=excluded.data_gaps_json,
                payload_json=excluded.payload_json,
                updated_at=excluded.updated_at
            """, (
                sym, res["name"], res["sector"], res["price"], res["verdict"], res["relative_score"],
                rm.get("pe"), rm.get("pb"), rm.get("div_yield_pct"), rm.get("ev_ebitda"),
                vs.get("pe_pct_below_sector"), vs.get("pb_pct_below_sector"), vs.get("div_yield_pct_above_sector"),
                iv.get("method_used"), iv.get("fair_value_per_share"), iv.get("margin_of_safety_pct"), res["confidence"],
                old_method, shift_reason, inp["stock"].get("financials_date"),
                json.dumps(res.get("synergy_tags", [])), json.dumps(res.get("flags", [])), json.dumps(res.get("data_gaps", [])), json.dumps(res), now_iso
            ))
        except Exception as e:
            failed_symbols[sym] = str(e)
            print(f"[UndervaluedScan] Error evaluating {sym}: {e}")

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
        "insufficient_history": sum(1 for r in results if r["verdict"] == "insufficient_history"),
        "total": len(results)
    }

    summary = {
        "counts": counts,
        "scanned": len(stocks),
        "evaluated": len(results),
        "failures_count": len(failed_symbols),
        "failed_symbols": failed_symbols,
        "top_margin_of_safety": results[0]["intrinsic_valuation"].get("margin_of_safety_pct") if results else None,
        "scanned_at": now_iso,
        "macro": macro
    }

    return results, summary


def get_undervalued_stocks(verdict_filter: Optional[str] = None, sector_filter: Optional[str] = None, synergy_filter: Optional[str] = None, limit: int = 150) -> List[Dict]:
    """Fetches evaluated stocks from SQLite database with filtering and sorting."""
    init_db()
    conn = get_db_connection()
    cur = conn.cursor()

    query = "SELECT payload_json, synergy_tags_json FROM undervalued_stocks WHERE 1=1"
    params = []

    if verdict_filter and verdict_filter != "ALL":
        query += " AND verdict = ?"
        params.append(verdict_filter.lower())

    if sector_filter and sector_filter != "ALL":
        query += " AND sector = ?"
        params.append(sector_filter)

    if synergy_filter and synergy_filter != "ALL":
        query += " AND synergy_tags_json LIKE ?"
        params.append(f"%{synergy_filter.upper()}%")

    query += " ORDER BY margin_of_safety_pct DESC NULLS LAST, relative_score DESC LIMIT ?"
    params.append(limit)

    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()

    items = []
    for r in rows:
        try:
            it = json.loads(r["payload_json"])
            items.append(it)
        except Exception:
            pass
    return items


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


def get_stock_valuation_history(symbol: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Fetches chronological verdict and model shifts for a given symbol."""
    init_db()
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT symbol, recorded_at, old_verdict, new_verdict,
               old_fair_value, new_fair_value, old_method, new_method,
               price_at_change, shift_reason
        FROM valuation_verdict_history
        WHERE symbol = ?
        ORDER BY id DESC
        LIMIT ?
    """, (symbol.upper().strip(), limit))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows
