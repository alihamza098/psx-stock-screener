#!/usr/bin/env python3
"""
PSX Live Trading Scoring Engine (v2)
=====================================
Deterministic Technical Scoring Engine implementing:
1. Unified Capped Flow Group (combines Momentum, Order Pressure, and Volume Spikes into [-20, +20])
2. Market Regime Detection (ADX14 + EMA20/EMA50 slope -> Trending vs Ranging)
3. Dynamic Regime-Dependent Factor Weights (Trend-following vs Mean-reversion)
4. Multi-Session Volume Baseline & Narrative Tags
5. Strict Liquidity Gate (capping illiquid symbols at HOLD)
6. Factor Agreement & Alignment Counter
"""

import json
import math
import datetime
import re
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from order_book_engine import analyze_order_book

CONFIG_PATH = Path(__file__).parent / "config" / "live_trading.json"


def load_config() -> Dict[str, Any]:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "engine_version": "v2",
        "scoring_engine_v2": {
            "flow_group_cap": 20.0,
            "adx_trending_threshold": 25.0,
            "ema_trend_threshold_pct": 0.75,
            "volume_tags": {
                "drying_up_ratio": 0.50,
                "weak_participation_ratio": 0.70,
                "confirmation_ratio": 1.25
            },
            "score_thresholds": {
                "strong_buy": 75,
                "buy": 60,
                "sell": 42,
                "strong_sell": 30
            }
        },
        "liquidity_gate": {
            "min_avg_volume_21d": 25000,
            "min_avg_turnover_21d_pkr": 500000
        }
    }


def compute_order_pressure(change_pct: float) -> Tuple[int, int]:
    """Simulates top-of-book order pressure balance from price change."""
    if change_pct > 3.0:
        buy_ratio = 72
    elif change_pct > 1.0:
        buy_ratio = 62
    elif change_pct > 0.0:
        buy_ratio = 54
    elif change_pct < -3.0:
        buy_ratio = 28
    elif change_pct < -1.0:
        buy_ratio = 38
    elif change_pct < 0.0:
        buy_ratio = 46
    else:
        buy_ratio = 50
    return buy_ratio, 100 - buy_ratio


def evaluate_regime(
    adx: float,
    ema20: float,
    ema50: float,
    adx_threshold: float = 25.0,
    ema_threshold_pct: float = 0.75
) -> Tuple[str, str]:
    """
    Classifies market regime as 'Trending' or 'Ranging'.
    Returns (regime, trend_direction).
    """
    if ema50 > 0:
        spread_pct = ((ema20 - ema50) / ema50) * 100.0
    else:
        spread_pct = 0.0

    if adx >= adx_threshold and abs(spread_pct) >= ema_threshold_pct:
        direction = "Bullish" if ema20 >= ema50 else "Bearish"
        return "Trending", direction
    return "Ranging", "Neutral"


from shared_trading_utils import (
    calculate_position_size as _shared_calc_pos_size,
    compute_trade_brackets as _shared_compute_brackets
)


def calculate_position_size(capital: float, risk_pct: float, entry: float, stop: float) -> Dict[str, Any]:
    """
    Computes position size based on capital and account risk percentage.
    Delegates to shared_trading_utils to prevent logic drift.
    """
    return _shared_calc_pos_size(capital, risk_pct, entry, stop)


def compute_trade_brackets(
    entry: float,
    recommendation: str,
    regime: str,
    atr: float,
    pivots: Optional[Dict[str, float]] = None,
    stock: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Stage 3 Smarter Trade Brackets:
    Delegates to shared_trading_utils to prevent logic drift.
    """
    return _shared_compute_brackets(
        entry=entry,
        recommendation=recommendation,
        regime=regime,
        atr=atr,
        pivots=pivots,
        stock=stock,
        config=config
    )


def analyze_timeframe_candles(candles: List[Dict[str, Any]]) -> Dict[str, Any]:

    """
    Analyzes candle series for a timeframe using EMA alignment and MACD state.
    Returns direction: 'up', 'down', or 'neutral'.
    """
    from technical_indicators import compute_ema_series, compute_macd_series

    if not candles or len(candles) < 5:
        return {
            "direction": "n/a",
            "status": "insufficient_data",
            "details": "Insufficient candle data (<5 bars)",
            "ema_trend": "neutral",
            "macd_trend": "neutral"
        }

    closes = [float(c.get("close", 0.0)) for c in candles]
    n = len(closes)
    last_price = closes[-1]

    # EMA Alignment
    ema20_s = compute_ema_series(closes, min(20, n))
    ema20 = ema20_s[-1] if ema20_s and ema20_s[-1] is not None else last_price

    if n >= 50:
        ema50_s = compute_ema_series(closes, 50)
        ema50 = ema50_s[-1] if ema50_s and ema50_s[-1] is not None else ema20
    else:
        ema50 = sum(closes) / n

    ema_score = 0.0
    if last_price > ema20 >= ema50:
        ema_score = 1.0
        ema_trend = "bullish"
    elif last_price < ema20 <= ema50:
        ema_score = -1.0
        ema_trend = "bearish"
    elif last_price > ema20:
        ema_score = 0.5
        ema_trend = "mild_bullish"
    elif last_price < ema20:
        ema_score = -0.5
        ema_trend = "mild_bearish"
    else:
        ema_trend = "neutral"

    # MACD state
    macd_score = 0.0
    macd_trend = "neutral"
    if n >= 26:
        macd_data = compute_macd_series(closes, 12, 26, 9)
        hist = macd_data["histogram"][-1] if macd_data["histogram"] and macd_data["histogram"][-1] is not None else 0.0
        line = macd_data["macd_line"][-1] if macd_data["macd_line"] and macd_data["macd_line"][-1] is not None else 0.0
        sig = macd_data["signal_line"][-1] if macd_data["signal_line"] and macd_data["signal_line"][-1] is not None else 0.0
        if line > 0 and hist >= -0.001:
            macd_score = 1.0
            macd_trend = "bullish"
        elif line < 0 and hist <= 0.001:
            macd_score = -1.0
            macd_trend = "bearish"
        elif line > sig:
            macd_score = 0.5
            macd_trend = "mild_bullish"
        elif line < sig:
            macd_score = -0.5
            macd_trend = "mild_bearish"

    total = ema_score + macd_score
    if total >= 1.0:
        direction = "up"
        details = "Bullish alignment (Price > EMA, MACD expanding)"
    elif total <= -1.0:
        direction = "down"
        details = "Bearish alignment (Price < EMA, MACD declining)"
    else:
        direction = "neutral"
        details = "Neutral / Mixed momentum"

    return {
        "direction": direction,
        "status": "ok",
        "details": details,
        "last_price": round(last_price, 2),
        "ema20": round(ema20, 2),
        "ema50": round(ema50, 2),
        "ema_trend": ema_trend,
        "macd_trend": macd_trend
    }


def evaluate_multi_timeframe_confluence(
    tf_map: Dict[str, Dict[str, Any]],
    current_rec: str,
    config: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Stage 4 Confluence Evaluation:
    - Roles: 1D (Macro Bias), 4H (Swing Bias), 1H (Setup), 15M (Trigger)
    - 4.2 Rule A: STRONG BUY / STRONG SELL only if higher timeframes agree (1D & 4H).
    - 4.2 Rule B: 15M signal against 4H trend is capped at HOLD with a 'Counter-trend' tag.
    """
    d_1d = tf_map.get("1D", {}).get("direction", "n/a")
    d_4h = tf_map.get("4H", {}).get("direction", "n/a")
    d_1h = tf_map.get("1H", {}).get("direction", "n/a")
    d_15m = tf_map.get("15M", {}).get("direction", "n/a")

    adjusted_rec = current_rec
    downgrade_reason = None
    counter_trend = False
    counter_trend_tag = None

    # Rule B: Counter-trend check (15M vs 4H)
    if d_4h == "down" and current_rec in ("STRONG BUY", "BUY"):
        adjusted_rec = "HOLD"
        counter_trend = True
        counter_trend_tag = "Counter-trend (4H Bearish Bias)"
    elif d_4h == "up" and current_rec in ("STRONG SELL", "SELL"):
        adjusted_rec = "HOLD"
        counter_trend = True
        counter_trend_tag = "Counter-trend (4H Bullish Bias)"

    # Rule A: Higher timeframes agreement for STRONG BUY / STRONG SELL
    if not counter_trend:
        if current_rec == "STRONG BUY" and not (d_1d == "up" and d_4h == "up"):
            adjusted_rec = "BUY"
            downgrade_reason = "Higher timeframes (1D / 4H) not fully aligned bullish — Downgraded to BUY"
        elif current_rec == "STRONG SELL" and not (d_1d == "down" and d_4h == "down"):
            adjusted_rec = "SELL"
            downgrade_reason = "Higher timeframes (1D / 4H) not fully aligned bearish — Downgraded to SELL"

    # Count alignments
    bullish_count = sum(1 for res in tf_map.values() if res.get("direction") == "up")
    bearish_count = sum(1 for res in tf_map.values() if res.get("direction") == "down")
    neutral_count = sum(1 for res in tf_map.values() if res.get("direction") in ("neutral", "n/a"))

    return {
        "timeframes": tf_map,
        "original_recommendation": current_rec,
        "adjusted_recommendation": adjusted_rec,
        "downgrade_reason": downgrade_reason,
        "counter_trend": counter_trend,
        "counter_trend_tag": counter_trend_tag,
        "bullish_count": bullish_count,
        "bearish_count": bearish_count,
        "neutral_count": neutral_count,
        "alignment_summary": f"{bullish_count if 'BUY' in adjusted_rec else (bearish_count if 'SELL' in adjusted_rec else neutral_count)} of {len(tf_map)} timeframes aligned"
    }


def parse_psx_date_str(d_str: Any) -> Optional[datetime.date]:
    """Resilient date parser for PSX dates (e.g. '01/09/2026', 'Sep 4, 2026', 'August 20, 2026 3:50 PM')."""
    if not d_str or not isinstance(d_str, str):
        return None
    d_str = d_str.strip()
    fmts = ['%d/%m/%Y', '%Y-%m-%d', '%b %d, %Y', '%B %d, %Y', '%d-%m-%Y']
    for fmt in fmts:
        try:
            return datetime.datetime.strptime(d_str.split()[0], fmt).date()
        except Exception:
            pass
    m = re.search(r'([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})', d_str)
    if m:
        month_str, day, year = m.groups()
        for m_fmt in ['%b', '%B']:
            try:
                return datetime.datetime.strptime(f'{month_str[:3]} {day} {year}', '%b %d %Y').date()
            except Exception:
                pass
    return None


def evaluate_psx_intelligence(
    stock: Dict[str, Any],
    company_data: Optional[Dict[str, Any]] = None,
    all_stocks: Optional[List[Dict[str, Any]]] = None,
    index_data: Optional[Dict[str, Any]] = None,
    dividend_calendar: Optional[List[Dict[str, Any]]] = None,
    config: Optional[Dict[str, Any]] = None,
    current_date: Optional[datetime.date] = None
) -> Dict[str, Any]:
    """
    Stage 5: PSX-Specific Intelligence
    5.1 Event-risk flag: Detect board meetings, results, book closures within next 1-2 trading days.
    5.2 Ex-dividend / book-closure adjustment: Neutralize negative momentum penalty on ex-date.
    5.3 Index filter: Downgrade BUY signals when KSE-100 is in strong downtrend.
    5.4 Sector context: Relative performance vs sector peers (Alpha vs Beta).
    """
    cfg = config or load_config()
    intel_cfg = cfg.get("psx_intelligence", {})
    symbol = (stock.get("symbol") or "").upper()
    stock_price = float(stock.get("price", 0.0))
    stock_change = float(stock.get("change", 0.0))
    stock_sector = (stock.get("sector") or "Other").strip()

    # Current date in PKT
    today = current_date or (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5)).date()

    # 5.1 Event-Risk Flag (Lookahead 1-2 trading days)
    ev_cfg = intel_cfg.get("event_risk", {})
    ev_enabled = ev_cfg.get("enabled", True)
    lookahead_days = int(ev_cfg.get("lookahead_days", 2))
    keywords = ev_cfg.get("keywords", ["BOARD MEETING", "FINANCIAL RESULTS", "BOOK CLOSURE", "CLOSED PERIOD", "EGM", "AGM"])

    has_event_risk = False
    event_type = None
    event_title = None
    event_date_str = None
    event_days_diff = None

    if ev_enabled:
        announcements = (company_data or {}).get("announcements", [])
        for ann in announcements:
            title = (ann.get("title") or "").upper()
            if any(kw in title for kw in keywords):
                ann_date = parse_psx_date_str(ann.get("date", ""))
                if ann_date:
                    diff = (ann_date - today).days
                    if -1 <= diff <= lookahead_days:
                        has_event_risk = True
                        event_type = "Board Meeting / Results" if ("BOARD" in title or "RESULTS" in title) else "Corporate Action"
                        event_title = ann.get("title")
                        event_date_str = ann.get("date")
                        event_days_diff = diff
                        break

        if not has_event_risk and dividend_calendar:
            for div in dividend_calendar:
                if (div.get("symbol") or "").upper() == symbol:
                    bc_str = div.get("bookClosure") or div.get("recordDate") or ""
                    start_str = bc_str.split("-")[0].strip() if "-" in bc_str else bc_str.strip()
                    bc_date = parse_psx_date_str(start_str)
                    if bc_date:
                        diff = (bc_date - today).days
                        if -1 <= diff <= lookahead_days:
                            has_event_risk = True
                            event_type = "Book Closure"
                            event_title = f"Book Closure: {div.get('dividendAmount', '')}"
                            event_date_str = start_str
                            event_days_diff = diff
                            break

    # 5.2 Ex-Dividend / Book-Closure Adjustment
    ex_cfg = intel_cfg.get("ex_dividend", {})
    ex_enabled = ex_cfg.get("enabled", True)
    is_ex_dividend = False
    dividend_pkr = None
    dividend_raw = None
    ex_tag = None

    if ex_enabled and dividend_calendar:
        for div in dividend_calendar:
            if (div.get("symbol") or "").upper() == symbol:
                ex_date_str = div.get("exDividendDate") or ""
                ex_date = parse_psx_date_str(ex_date_str)
                if ex_date:
                    diff = (ex_date - today).days
                    if -1 <= diff <= 0:
                        is_ex_dividend = True
                        dividend_raw = div.get("dividendAmount", "")
                        m_pct = re.search(r'(\d+(?:\.\d+)?)%', dividend_raw)
                        if m_pct:
                            dividend_pkr = round(float(m_pct.group(1)) * 0.10, 2)
                        else:
                            m_rs = re.search(r'Rs\.?\s*(\d+(?:\.\d+)?)', dividend_raw, re.I)
                            if m_rs:
                                dividend_pkr = float(m_rs.group(1))
                        ex_tag = f"Ex-Dividend Session ({dividend_raw}) on {ex_date_str}"
                        break

    # 5.3 Index Filter (KSE-100 Downtrend Protection)
    idx_cfg = intel_cfg.get("index_filter", {})
    idx_enabled = idx_cfg.get("enabled", True)
    idx_sym = idx_cfg.get("index_symbol", "KSE100")
    downtrend_thresh = float(idx_cfg.get("downtrend_threshold_pct", -0.75))

    kse_data = None
    kse_status = "unavailable"
    kse_change_pct = 0.0
    is_index_downtrend = False

    if idx_enabled and index_data:
        indices = index_data.get("indices", [])
        for idx in indices:
            if (idx.get("name") or "").upper() == idx_sym.upper():
                kse_data = idx
                kse_status = "ok"
                kse_change_pct = float(idx.get("changePercent", idx.get("percentChange", 0.0)))
                is_index_downtrend = (kse_change_pct <= downtrend_thresh)
                break

    # 5.4 Sector Context (Stock vs Sector Peers)
    sec_cfg = intel_cfg.get("sector_context", {})
    sec_enabled = sec_cfg.get("enabled", True)
    rs_thresh = float(sec_cfg.get("relative_strength_threshold", 1.0))

    sector_avg_change = 0.0
    relative_strength = 0.0
    peer_count = 0
    sector_tag = None

    if sec_enabled and all_stocks and stock_sector and stock_sector != "Other":
        peers = [s for s in all_stocks if (s.get("sector") or "").strip() == stock_sector and s.get("price")]
        peer_count = len(peers)
        if peers:
            sector_avg_change = round(sum(float(s.get("change", 0.0)) for s in peers) / peer_count, 2)
            relative_strength = round(stock_change - sector_avg_change, 2)
            if relative_strength >= rs_thresh:
                sector_tag = f"Sector Alpha: +{relative_strength:.2f}% vs {stock_sector} ({sector_avg_change:+0.2f}%)"
            elif relative_strength <= -rs_thresh:
                sector_tag = f"Sector Lag: {relative_strength:.2f}% vs {stock_sector} ({sector_avg_change:+0.2f}%)"
            else:
                sector_tag = f"Sector In-Line: {relative_strength:+.2f}% vs {stock_sector} ({sector_avg_change:+0.2f}%)"
        else:
            sector_tag = f"Sector: {stock_sector} (No active peer quotes)"
    else:
        sector_tag = f"Sector: {stock_sector}"

    return {
        "event_risk": {
            "has_risk": has_event_risk,
            "event_type": event_type,
            "event_title": event_title,
            "event_date": event_date_str,
            "days_diff": event_days_diff,
            "warning": f"Event Risk Alert: {event_title} ({event_date_str}) within 1-2 days — High conviction suppressed" if has_event_risk else None,
            "suppress_strong": has_event_risk and ev_cfg.get("suppress_strong_signals", True)
        },
        "ex_dividend": {
            "is_ex_date": is_ex_dividend,
            "dividend_raw": dividend_raw,
            "dividend_pkr": dividend_pkr,
            "tag": ex_tag,
            "neutralize_negative_momentum": is_ex_dividend and ex_cfg.get("neutralize_negative_momentum", True)
        },
        "index_filter": {
            "status": kse_status,
            "name": idx_sym,
            "value": kse_data.get("value") if kse_data else None,
            "change_pct": kse_change_pct,
            "is_downtrend": is_index_downtrend,
            "downgrade_buy": is_index_downtrend and idx_cfg.get("downgrade_buy_signals", True),
            "tag": f"Index Headwind: {idx_sym} in Strong Downtrend ({kse_change_pct:+0.2f}%)" if is_index_downtrend else (f"{idx_sym}: {kse_change_pct:+0.2f}%" if kse_status == "ok" else "Index context unavailable")
        },
        "sector_context": {
            "sector": stock_sector,
            "sector_avg_change": sector_avg_change,
            "relative_strength": relative_strength,
            "peer_count": peer_count,
            "tag": sector_tag
        }
    }


def compute_v2_recommendation(
    stock: Dict[str, Any],
    history: List[Dict[str, Any]],
    market_status: Optional[Dict[str, Any]] = None,
    volume_history: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    psx_intelligence: Optional[Dict[str, Any]] = None,
    mtf_confluence: Optional[Dict[str, Any]] = None,
    real_depth: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Generates v2 Deterministic Trading Signal and Factor Breakdown.
    """
    cfg = config or load_config()
    v2_cfg = cfg.get("scoring_engine_v2", {})
    liq_cfg = cfg.get("liquidity_gate", {})
    flow_cap = float(v2_cfg.get("flow_group_cap", 20.0))
    adx_thresh = float(v2_cfg.get("adx_trending_threshold", 25.0))
    ema_thresh = float(v2_cfg.get("ema_trend_threshold_pct", 0.75))

    price = float(stock.get("price", 0.0))
    change = float(stock.get("change", 0.0))
    volume = float(stock.get("volume", 0.0))

    # Closes series from history (history is newest first)
    closes = [b.get("close", price) for b in history] if history else [price]
    if not closes:
        closes = [price]

    from technical_indicators import (
        compute_rsi_series, compute_macd_series, compute_bollinger_bands,
        compute_vwap, compute_classical_pivots, compute_ema_series, compute_adx_series,
        compute_atr_series
    )

    # 1. Compute deterministic indicators
    # Need oldest to newest for indicator math
    rev_closes = list(reversed(closes))
    rsi_s = compute_rsi_series(rev_closes, 14)
    rsi = rsi_s[-1] if rsi_s and rsi_s[-1] is not None else 50.0

    macd_data = compute_macd_series(rev_closes, 12, 26, 9)
    n_macd = len(macd_data["histogram"])
    last_hist = macd_data["histogram"][-1] if n_macd and macd_data["histogram"][-1] is not None else 0.0
    last_macd = macd_data["macd_line"][-1] if n_macd and macd_data["macd_line"][-1] is not None else 0.0
    last_sig = macd_data["signal_line"][-1] if n_macd and macd_data["signal_line"][-1] is not None else 0.0
    macd_res = {"macd": last_macd, "signal": last_sig, "histogram": last_hist}

    bb_res = compute_bollinger_bands(rev_closes, 20, 2.0)
    bb_res["percentB"] = bb_res.get("percent_b", 50.0)

    # vwap
    hist_prices = [b.get("close", price) for b in history[:5]] if history else [price]
    hist_vols = [b.get("volume", volume) for b in history[:5]] if history else [volume]
    vwap = compute_vwap(hist_prices, hist_vols)

    # pivots
    if history:
        h5 = [b.get("high", b.get("close", price)*1.02) for b in history[:5]]
        l5 = [b.get("low", b.get("close", price)*0.98) for b in history[:5]]
        sr = compute_classical_pivots(max(h5), min(l5), history[0].get("close", price))
    else:
        sr = compute_classical_pivots(price*1.02, price*0.98, price)

    # ema
    ema20_s = compute_ema_series(rev_closes, 20)
    ema20 = ema20_s[-1] if ema20_s and ema20_s[-1] is not None else price
    ema50_s = compute_ema_series(rev_closes, 50)
    ema50 = ema50_s[-1] if ema50_s and ema50_s[-1] is not None else price

    # highs, lows, closes for ATR and ADX
    rev_h = list(reversed([b.get("high", b.get("close", price)*1.01) for b in history])) if history else [price*1.01]
    rev_l = list(reversed([b.get("low", b.get("close", price)*0.99) for b in history])) if history else [price*0.99]
    rev_c = rev_closes

    # atr (Wilder's 14-period)
    atr_val = price * 0.025
    if len(history) >= 14:
        atr_s = compute_atr_series(rev_h, rev_l, rev_c, 14)
        if atr_s and atr_s[-1] is not None:
            atr_val = atr_s[-1]

    # adx
    adx_val = 20.0
    if len(history) >= 28:
        adx_dict = compute_adx_series(rev_h, rev_l, rev_c, 14)
        if adx_dict["adx"] and adx_dict["adx"][-1] is not None:
            adx_val = adx_dict["adx"][-1]
    adx_res = {"adx": adx_val}

    # 2. Volume Baselines from volume_history if available
    avg_21d = 25000.0
    avg_5d = 25000.0
    if volume_history and "windows" in volume_history:
        w1m = volume_history["windows"].get("1M", {})
        w1w = volume_history["windows"].get("1W", {})
        if w1m.get("status") == "ok" and w1m.get("avg_daily_volume"):
            avg_21d = float(w1m["avg_daily_volume"])
        if w1w.get("status") == "ok" and w1w.get("avg_daily_volume"):
            avg_5d = float(w1w["avg_daily_volume"])
    elif stock.get("avgVolume"):
        avg_21d = float(stock["avgVolume"])
        avg_5d = avg_21d

    # Volume ratio
    vol_ratio = volume / max(1.0, avg_21d)

    # 3. Regime Detection
    regime, trend_direction = evaluate_regime(adx_res["adx"], ema20, ema50, adx_thresh, ema_thresh)

    # 4. Factor scoring
    score = 50.0
    factors = []

    # ── FACTOR 1: Unified Flow Group (Momentum + Pressure + Volume Spike) ──
    # Capped at [-flow_cap, +flow_cap] to eliminate double-counting
    mom_pts = 0.0
    ex_div = (psx_intelligence or {}).get("ex_dividend", {})
    is_ex_div_active = bool(ex_div.get("is_ex_date") and ex_div.get("neutralize_negative_momentum") and change < 0)

    if is_ex_div_active:
        # Ex-dividend session: do not penalize dividend-related price drop as bearish momentum (Rule 5.2)
        mom_pts = 0.0
    elif change >= 3.0:
        mom_pts = 12.0
    elif change >= 1.0:
        mom_pts = 7.0
    elif change >= 0.2:
        mom_pts = 3.0
    elif change <= -3.0:
        mom_pts = -12.0
    elif change <= -1.0:
        mom_pts = -7.0
    elif change <= -0.2:
        mom_pts = -3.0

    symbol_name = stock.get("symbol", "")
    ob_analysis = analyze_order_book(symbol_name, real_depth=real_depth, price_change_pct=change)
    buy_ratio = ob_analysis["buy_ratio"]
    sell_ratio = ob_analysis["sell_ratio"]
    press_pts = 0.0
    if not is_ex_div_active:
        if buy_ratio >= 65:
            press_pts = 8.0
        elif buy_ratio >= 55:
            press_pts = 4.0
        elif buy_ratio <= 35:
            press_pts = -8.0
        elif buy_ratio <= 45:
            press_pts = -4.0

    spike_pts = 0.0
    if not is_ex_div_active:
        if vol_ratio >= 1.5:
            spike_pts = 8.0 if change >= 0 else -8.0
        elif vol_ratio >= 1.2:
            spike_pts = 4.0 if change >= 0 else -4.0

    raw_flow = mom_pts + press_pts + spike_pts
    capped_flow = max(-flow_cap, min(flow_cap, raw_flow))
    score += capped_flow

    if is_ex_div_active:
        factors.append({
            "icon": "💰",
            "name": "Ex-Dividend Adjustment",
            "text": f"Ex-dividend session ({ex_div.get('dividend_raw') or 'Payout'}) — Negative price drop neutralized (not treated as bearish momentum)",
            "weight": "Med",
            "points": 0.0,
            "direction": "NEUTRAL"
        })

    flow_direction = "BULLISH" if capped_flow > 0 else ("BEARISH" if capped_flow < 0 else "NEUTRAL")
    factors.append({
        "icon": "🌊",
        "name": "Order Flow & Momentum",
        "text": f"Unified Flow & Pressure ({'+' if raw_flow >= 0 else ''}{raw_flow:.0f} pts raw, capped at {'+' if capped_flow >= 0 else ''}{capped_flow:.0f})",
        "weight": "High",
        "points": capped_flow,
        "direction": flow_direction
    })

    # ── FACTOR 2: Moving Average Alignment & Trend ──
    ma_pts = 0.0
    ma_dir = "NEUTRAL"
    if regime == "Trending":
        if price > ema20 and ema20 > ema50:
            ma_pts = 12.0
            ma_dir = "BULLISH"
            factors.append({
                "icon": "📈",
                "name": "Moving Average Alignment",
                "text": f"Strong Bullish Moving Average Alignment (Price > EMA20 > EMA50)",
                "weight": "High",
                "points": ma_pts,
                "direction": ma_dir
            })
        elif price < ema20 and ema20 < ema50:
            ma_pts = -12.0
            ma_dir = "BEARISH"
            factors.append({
                "icon": "📉",
                "name": "Moving Average Alignment",
                "text": f"Strong Bearish Moving Average Alignment (Price < EMA20 < EMA50)",
                "weight": "High",
                "points": ma_pts,
                "direction": ma_dir
            })
        else:
            factors.append({
                "icon": "➡️",
                "name": "Moving Average Alignment",
                "text": "Moving Averages Mixed in Trend",
                "weight": "Low",
                "points": 0.0,
                "direction": "NEUTRAL"
            })
    else:
        # In Ranging market, MA alignment has lower weight
        if price > ema20:
            ma_pts = 4.0
            ma_dir = "BULLISH"
            factors.append({
                "icon": "🔼",
                "name": "EMA 20",
                "text": f"Trading above short-term EMA 20 (₨{ema20:.2f})",
                "weight": "Low",
                "points": ma_pts,
                "direction": ma_dir
            })
        else:
            ma_pts = -4.0
            ma_dir = "BEARISH"
            factors.append({
                "icon": "🔽",
                "name": "EMA 20",
                "text": f"Trading below short-term EMA 20 (₨{ema20:.2f})",
                "weight": "Low",
                "points": ma_pts,
                "direction": ma_dir
            })
    score += ma_pts

    # ── FACTOR 3: VWAP Support / Resistance ──
    vwap_pts = 0.0
    vwap_dir = "BULLISH" if price >= vwap else "BEARISH"
    if regime == "Trending":
        vwap_pts = 10.0 if price >= vwap else -10.0
        factors.append({
            "icon": "⚡",
            "name": "VWAP Crossover",
            "text": f"Price {'holding ABOVE' if price >= vwap else 'below'} VWAP (₨{vwap:.2f}) in Trend",
            "weight": "High",
            "points": vwap_pts,
            "direction": vwap_dir
        })
    else:
        vwap_pts = 6.0 if price >= vwap else -6.0
        factors.append({
            "icon": "⚡",
            "name": "VWAP Crossover",
            "text": f"Price {'above' if price >= vwap else 'below'} Volume Weighted Avg Price (₨{vwap:.2f})",
            "weight": "Med",
            "points": vwap_pts,
            "direction": vwap_dir
        })
    score += vwap_pts

    # ── FACTOR 4: RSI Indicator (Regime Adaptive) ──
    rsi_pts = 0.0
    rsi_dir = "NEUTRAL"
    if regime == "Trending":
        if trend_direction == "Bullish":
            if 70.0 <= rsi < 85.0:
                rsi_pts = 6.0
                rsi_dir = "BULLISH"
                factors.append({
                    "icon": "🔥",
                    "name": "RSI Trend Momentum",
                    "text": f"RSI in Powerful Bullish Continuation Band ({rsi:.1f}) — Not overbought in strong trend",
                    "weight": "Med",
                    "points": rsi_pts,
                    "direction": rsi_dir
                })
            elif rsi >= 85.0:
                rsi_pts = -8.0
                rsi_dir = "BEARISH"
                factors.append({
                    "icon": "🔴",
                    "name": "RSI Exhaustion",
                    "text": f"RSI Parabolic Blow-off (>85) at {rsi:.1f} — Pullback risk",
                    "weight": "Med",
                    "points": rsi_pts,
                    "direction": rsi_dir
                })
            elif 50.0 <= rsi < 70.0:
                rsi_pts = 8.0
                rsi_dir = "BULLISH"
                factors.append({
                    "icon": "✅",
                    "name": "RSI Bullish Zone",
                    "text": f"RSI in Healthy Bullish Trend Zone ({rsi:.1f})",
                    "weight": "Med",
                    "points": rsi_pts,
                    "direction": rsi_dir
                })
            else:
                rsi_pts = -6.0
                rsi_dir = "BEARISH"
                factors.append({
                    "icon": "⚠️",
                    "name": "RSI Trend Decay",
                    "text": f"RSI Weakening below 50 ({rsi:.1f}) in Uptrend",
                    "weight": "Low",
                    "points": rsi_pts,
                    "direction": rsi_dir
                })
        else: # Bearish trend
            if 18.0 <= rsi <= 30.0:
                rsi_pts = -6.0
                rsi_dir = "BEARISH"
                factors.append({
                    "icon": "📉",
                    "name": "RSI Bearish Trend",
                    "text": f"RSI Depressed ({rsi:.1f}) — Trend continuation downwards",
                    "weight": "Med",
                    "points": rsi_pts,
                    "direction": rsi_dir
                })
            elif rsi < 18.0:
                rsi_pts = 8.0
                rsi_dir = "BULLISH"
                factors.append({
                    "icon": "🟢",
                    "name": "RSI Capitulation",
                    "text": f"RSI Extreme Capitulation ({rsi:.1f}) — Rebound expected",
                    "weight": "High",
                    "points": rsi_pts,
                    "direction": rsi_dir
                })
            else:
                rsi_pts = -4.0
                rsi_dir = "BEARISH"
                factors.append({
                    "icon": "➡️",
                    "name": "RSI Neutral Bearish",
                    "text": f"RSI Neutral in Downtrend ({rsi:.1f})",
                    "weight": "Low",
                    "points": rsi_pts,
                    "direction": rsi_dir
                })
    else: # Ranging market -> Mean reversion is king
        if rsi < 30.0:
            rsi_pts = 15.0
            rsi_dir = "BULLISH"
            factors.append({
                "icon": "🟢",
                "name": "RSI Mean Reversion",
                "text": f"RSI Oversold in Ranging Market ({rsi:.1f}) — High-Probability Mean Reversion Long",
                "weight": "High",
                "points": rsi_pts,
                "direction": rsi_dir
            })
        elif rsi > 70.0:
            rsi_pts = -15.0
            rsi_dir = "BEARISH"
            factors.append({
                "icon": "🔴",
                "name": "RSI Mean Reversion",
                "text": f"RSI Overbought at Range Ceiling ({rsi:.1f}) — Pullback to range mean",
                "weight": "High",
                "points": rsi_pts,
                "direction": rsi_dir
            })
        elif 45.0 <= rsi <= 55.0:
            factors.append({
                "icon": "⚖️",
                "name": "RSI Equilibrium",
                "text": f"RSI Equilibrium at Centerline ({rsi:.1f})",
                "weight": "Low",
                "points": 0.0,
                "direction": "NEUTRAL"
            })
        else:
            rsi_pts = 4.0 if rsi < 45.0 else -4.0
            rsi_dir = "BULLISH" if rsi < 45.0 else "BEARISH"
            factors.append({
                "icon": "➡️",
                "name": "RSI Oscillator",
                "text": f"RSI Oscillating ({rsi:.1f})",
                "weight": "Low",
                "points": rsi_pts,
                "direction": rsi_dir
            })
    score += rsi_pts

    # ── FACTOR 5: MACD Histogram ──
    macd_pts = 0.0
    macd_dir = "BULLISH" if macd_res["histogram"] > 0 else "BEARISH"
    if regime == "Trending":
        macd_pts = 10.0 if macd_res["histogram"] > 0 else -10.0
    else:
        macd_pts = 6.0 if macd_res["histogram"] > 0 else -6.0
    score += macd_pts
    factors.append({
        "icon": "📊",
        "name": "MACD Histogram",
        "text": f"MACD Histogram {'Positive (+{:.2f})'.format(macd_res['histogram']) if macd_res['histogram'] >= 0 else 'Negative ({:.2f})'.format(macd_res['histogram'])}",
        "weight": "High" if regime == "Trending" else "Med",
        "points": macd_pts,
        "direction": macd_dir
    })

    # ── FACTOR 6: Bollinger %B & Range Pivots ──
    bb_pts = 0.0
    bb_dir = "NEUTRAL"
    if regime == "Ranging":
        if bb_res["percentB"] <= 15.0:
            bb_pts = 10.0
            bb_dir = "BULLISH"
            factors.append({
                "icon": "🎯",
                "name": "Bollinger Bands",
                "text": f"Price at Lower Bollinger Band (%B {bb_res['percentB']:.0f}%) — Range Floor Support",
                "weight": "High",
                "points": bb_pts,
                "direction": bb_dir
            })
        elif bb_res["percentB"] >= 85.0:
            bb_pts = -10.0
            bb_dir = "BEARISH"
            factors.append({
                "icon": "🎯",
                "name": "Bollinger Bands",
                "text": f"Price at Upper Bollinger Band (%B {bb_res['percentB']:.0f}%) — Range Ceiling Resistance",
                "weight": "High",
                "points": bb_pts,
                "direction": bb_dir
            })
        else:
            factors.append({
                "icon": "🎯",
                "name": "Bollinger Bands",
                "text": f"Bollinger %B Mid-band ({bb_res['percentB']:.0f}%)",
                "weight": "Low",
                "points": 0.0,
                "direction": "NEUTRAL"
            })
    else:
        # In trending regime, walking the bands is strong
        if bb_res["percentB"] >= 80.0 and trend_direction == "Bullish":
            bb_pts = 6.0
            bb_dir = "BULLISH"
            factors.append({
                "icon": "🚀",
                "name": "Bollinger Band Walk",
                "text": f"Price Expanding Along Upper Band (%B {bb_res['percentB']:.0f}%)",
                "weight": "Med",
                "points": bb_pts,
                "direction": bb_dir
            })
        elif bb_res["percentB"] <= 20.0 and trend_direction == "Bearish":
            bb_pts = -6.0
            bb_dir = "BEARISH"
            factors.append({
                "icon": "⚠️",
                "name": "Bollinger Band Walk",
                "text": f"Price Expanding Along Lower Band (%B {bb_res['percentB']:.0f}%)",
                "weight": "Med",
                "points": bb_pts,
                "direction": bb_dir
            })
    score += bb_pts

    # 5. Volume Tags (Confidence Modifiers, NOT raw points)
    volume_tags = []
    conf_mod = 0.0

    if avg_21d > 0 and avg_5d < 0.50 * avg_21d:
        volume_tags.append("Volume drying up")
        conf_mod -= 5.0
        factors.append({
            "icon": "🏜️",
            "name": "Volume Tag",
            "text": "Volume drying up (5-day avg < 50% of 21-day average)",
            "weight": "Low",
            "points": 0.0,
            "direction": "BEARISH"
        })

    if change > 0.5 and vol_ratio < 0.70:
        volume_tags.append("Weak participation")
        conf_mod -= 8.0
        factors.append({
            "icon": "⚠️",
            "name": "Volume Tag",
            "text": "Weak participation (Price gain on thin volume divergence)",
            "weight": "Med",
            "points": 0.0,
            "direction": "BEARISH"
        })
    elif change > 0.5 and vol_ratio >= 1.25:
        volume_tags.append("Volume confirmation")
        conf_mod += 5.0
        factors.append({
            "icon": "🔥",
            "name": "Volume Tag",
            "text": "Volume confirmation (Price breakout backed by expanding volume)",
            "weight": "Med",
            "points": 0.0,
            "direction": "BULLISH"
        })

    # ── FACTOR 7: Sector Context (Stage 5.4 Alpha vs Beta) ──
    sec_info = (psx_intelligence or {}).get("sector_context", {})
    if sec_info and sec_info.get("peer_count", 0) > 0:
        rel = float(sec_info.get("relative_strength", 0.0))
        sec_pts = 0.0
        sec_dir = "NEUTRAL"
        if rel >= 1.0:
            sec_pts = 4.0
            sec_dir = "BULLISH"
        elif rel <= -1.0:
            sec_pts = -4.0
            sec_dir = "BEARISH"
        score += sec_pts
        factors.append({
            "icon": "🏢",
            "name": "Sector Context",
            "text": sec_info.get("tag", f"Sector Relative Strength: {rel:+.2f}%"),
            "weight": "Med" if abs(rel) >= 1.0 else "Low",
            "points": sec_pts,
            "direction": sec_dir
        })

    # Clamp raw score to [5, 95]
    final_score = max(5.0, min(95.0, round(score, 1)))

    # Compute Factor Alignment
    bullish_count = sum(1 for f in factors if f.get("direction") == "BULLISH")
    bearish_count = sum(1 for f in factors if f.get("direction") == "BEARISH")
    active_factors_count = bullish_count + bearish_count

    # 6. Recommendation determination
    rec = "HOLD"
    sig_class = "signal-hold"
    sig_color = "#fbbf24"

    thresh = v2_cfg.get("score_thresholds", {"strong_buy": 75, "buy": 60, "sell": 42, "strong_sell": 30})
    if final_score >= thresh.get("strong_buy", 75):
        rec = "STRONG BUY"
        sig_class = "signal-strong-buy"
        sig_color = "#22c55e"
    elif final_score >= thresh.get("buy", 60):
        rec = "BUY"
        sig_class = "signal-buy"
        sig_color = "#4ade80"
    elif final_score <= thresh.get("strong_sell", 30):
        rec = "STRONG SELL"
        sig_class = "signal-strong-sell"
        sig_color = "#ef4444"
    elif final_score <= thresh.get("sell", 42):
        rec = "SELL"
        sig_class = "signal-sell"
        sig_color = "#f87171"

    # 7. Liquidity Gate
    min_vol = float(liq_cfg.get("min_avg_volume_21d", 25000))
    min_turnover = float(liq_cfg.get("min_avg_turnover_21d_pkr", 500000))
    turnover = avg_21d * price

    is_liquid = True
    if avg_21d < min_vol or turnover < min_turnover:
        is_liquid = False
        rec = "HOLD"
        sig_class = "signal-hold signal-illiquid"
        sig_color = "#fbbf24"
        factors.insert(0, {
            "icon": "⚠️",
            "name": "Liquidity Gate",
            "text": f"Low liquidity: 21-day average volume ({avg_21d:,.0f} shrs) or turnover (PKR {turnover:,.0f}) below threshold — Signal capped at HOLD",
            "weight": "Critical",
            "points": 0.0,
            "direction": "NEUTRAL"
        })

    # Alignment text
    aligned_text = f"{bullish_count if rec in ('STRONG BUY', 'BUY') else bearish_count} of {active_factors_count} aligned"

    # 8. Smarter Trade Brackets (Stage 3: ATR brackets, circuits, pivots, R:R)
    brackets = compute_trade_brackets(
        entry=price,
        recommendation=rec,
        regime=regime,
        atr=atr_val,
        pivots=sr,
        stock=stock,
        config=cfg
    )

    # Downgrade signal if poor R:R (< 1.5)
    if brackets.get("is_poor_rr") and rec != "HOLD":
        rec = brackets["adjusted_recommendation"]
        if rec == "BUY":
            sig_class = "signal-buy"
            sig_color = "#4ade80"
        elif rec == "HOLD":
            sig_class = "signal-hold"
            sig_color = "#fbbf24"
        elif rec == "SELL":
            sig_class = "signal-sell"
            sig_color = "#f87171"
        factors.append({
            "icon": "⚠️",
            "name": "Risk/Reward",
            "text": f"Poor risk/reward ({brackets['gross_rr']}:1 < 1.5:1) — Conviction downgraded",
            "weight": "Med",
            "points": 0.0,
            "direction": "BEARISH"
        })

    if brackets.get("circuit_warning"):
        factors.append({
            "icon": "⚡",
            "name": "Circuit Warning",
            "text": f"{brackets['circuit_warning']} (Band: ₨{brackets['circuit_lower']} - ₨{brackets['circuit_upper']})",
            "weight": "High",
            "points": 0.0,
            "direction": "BEARISH"
        })

    # Update risk level to incorporate ATR volatility (Requirement 3.6)
    vol_level = brackets.get("volatility_level", "Normal")
    if vol_level == "High" or abs(change) >= 5.0 or rsi > 75.0 or rsi < 25.0:
        risk_level = "High"
    elif vol_level == "Normal" or abs(change) >= 2.0:
        risk_level = "Medium"
    else:
        risk_level = "Low"

    # Default position sizing for reference (500k capital, 1.5% risk)
    pos_sizing = calculate_position_size(
        capital=500000.0,
        risk_pct=1.5,
        entry=price,
        stop=brackets["stop"]
    )

    # 9. PSX-Specific Intelligence Rules (Stage 5)
    orig_rec = rec
    if psx_intelligence:
        # Rule 5.1: Event Risk Flag — Suppress STRONG signals
        ev_risk = psx_intelligence.get("event_risk", {})
        if ev_risk.get("suppress_strong") and ev_risk.get("has_risk"):
            if rec == "STRONG BUY":
                rec = "BUY"
                sig_class = "signal-buy"
                sig_color = "#4ade80"
            elif rec == "STRONG SELL":
                rec = "SELL"
                sig_class = "signal-sell"
                sig_color = "#f87171"
            factors.insert(0, {
                "icon": "📅",
                "name": "Event Risk Flag",
                "text": ev_risk.get("warning") or "Upcoming Board Meeting / Corporate Event — Strong signal suppressed",
                "weight": "High",
                "points": 0.0,
                "direction": "NEUTRAL"
            })

        # Rule 5.3: Index Filter — Downgrade BUY signals when KSE-100 in strong downtrend
        idx_filt = psx_intelligence.get("index_filter", {})
        if idx_filt.get("downgrade_buy") and idx_filt.get("is_downtrend"):
            if rec == "STRONG BUY":
                rec = "BUY"
                sig_class = "signal-buy"
                sig_color = "#4ade80"
            elif rec == "BUY":
                rec = "HOLD"
                sig_class = "signal-hold"
                sig_color = "#fbbf24"
            factors.insert(0, {
                "icon": "📉",
                "name": "Index Filter",
                "text": idx_filt.get("tag") or f"KSE-100 in Strong Downtrend ({idx_filt.get('change_pct', 0.0):+0.2f}%) — BUY signal downgraded one tier",
                "weight": "High",
                "points": 0.0,
                "direction": "NEUTRAL"
            })

    # 10. Multi-Timeframe Confluence Adjustment (Stage 4)
    if mtf_confluence:
        if mtf_confluence.get("counter_trend"):
            rec = "HOLD"
            sig_class = "signal-hold"
            sig_color = "#fbbf24"
            factors.insert(0, {
                "icon": "⚠️",
                "name": "MTF Confluence",
                "text": f"{mtf_confluence.get('counter_trend_tag')}: Signal capped at HOLD",
                "weight": "High",
                "points": 0.0,
                "direction": "NEUTRAL"
            })
        elif mtf_confluence.get("downgrade_reason"):
            if rec == "STRONG BUY":
                rec = "BUY"
                sig_class = "signal-buy"
                sig_color = "#4ade80"
            elif rec == "STRONG SELL":
                rec = "SELL"
                sig_class = "signal-sell"
                sig_color = "#f87171"
            factors.insert(0, {
                "icon": "⏱️",
                "name": "MTF Confluence",
                "text": mtf_confluence.get("downgrade_reason"),
                "weight": "Med",
                "points": 0.0,
                "direction": "NEUTRAL"
            })

    return {
        "engine_version": "v2",
        "original_recommendation": orig_rec,
        "recommendation": rec,
        "signalClass": sig_class,
        "signalColor": sig_color,
        "confidence": final_score,
        "regime": regime,
        "trend_direction": trend_direction,
        "is_liquid": is_liquid,
        "volume_tags": volume_tags,
        "psx_intelligence": psx_intelligence,
        "mtf_confluence": mtf_confluence,
        "aligned_factors_text": aligned_text,
        "bullish_factors": bullish_count,
        "bearish_factors": bearish_count,
        "total_active_factors": active_factors_count,
        "factors": factors,
        "buyRatio": buy_ratio,
        "sellRatio": sell_ratio,
        "order_book": ob_analysis,
        "riskLevel": risk_level,
        "suggestedEntry": brackets["entry"],
        "targetPrice": brackets["target"],
        "stopLoss": brackets["stop"],
        "trade_brackets": brackets,
        "position_sizing": pos_sizing,
        "indicators": {
            "rsi": rsi,
            "macd": macd_res,
            "bb": bb_res,
            "vwap": vwap,
            "sr": sr,
            "ema20": ema20,
            "ema50": ema50,
            "adx": adx_res["adx"],
            "atr": atr_val
        }
    }
