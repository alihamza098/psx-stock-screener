#!/usr/bin/env python3
"""
PSX Live Trading Signal Logger & Empirical Outcome Tracker
===========================================================
Stores discrete trading recommendations in SQLite (cache/live_trading.db)
with configurable deduplication/throttling to prevent polling floods.
Tracks multi-session performance outcomes (1-Day, 3-Day) against daily High/Low.
"""

import json
import sqlite3
import time
import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from live_backtest import compute_wilson_ci

DB_PATH = Path(__file__).parent / "cache" / "live_trading.db"
CONFIG_PATH = Path(__file__).parent / "config" / "live_trading.json"


def load_config() -> Dict[str, Any]:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "engine_version": "v1",
        "signal_logging": {
            "enabled": True,
            "throttle_minutes": 15,
            "score_band_change_threshold": 5.0
        },
        "outcome_tracker": {
            "check_horizons_trading_days": [1, 3],
            "ambiguous_count_as_loss": True
        }
    }


def get_db_connection(db_file: Optional[Path] = None) -> sqlite3.Connection:
    path = db_file or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_file: Optional[Path] = None):
    with get_db_connection(db_file) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS live_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                timestamp_utc TEXT NOT NULL,
                timestamp_pkt TEXT NOT NULL,
                price REAL NOT NULL,
                score REAL NOT NULL,
                recommendation TEXT NOT NULL,
                factor_breakdown TEXT,
                entry REAL NOT NULL,
                target REAL NOT NULL,
                stop_loss REAL NOT NULL,
                risk_level TEXT,
                market_status TEXT,
                data_freshness_sec REAL,
                engine_version TEXT NOT NULL,
                outcome_1d TEXT DEFAULT 'PENDING',
                outcome_3d TEXT DEFAULT 'PENDING',
                r_multiple REAL,
                days_to_resolution INTEGER,
                resolution_notes TEXT,
                created_at REAL NOT NULL
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_sym_time ON live_signals(symbol, created_at);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_rec ON live_signals(recommendation);")
        conn.commit()


def log_signal_if_eligible(signal_dict: Dict[str, Any], db_file: Optional[Path] = None) -> Optional[int]:
    """
    Conditionally logs a signal to SQLite.
    Throttled: logs only if recommendation changes, score changes by >= threshold,
    or >= throttle_minutes have elapsed since the last log for this symbol.
    """
    config = load_config()
    log_cfg = config.get("signal_logging", {})
    if not log_cfg.get("enabled", True):
        return None

    symbol = signal_dict.get("symbol", "").upper().strip()
    if not symbol:
        return None

    init_db(db_file)

    throttle_sec = float(log_cfg.get("throttle_minutes", 15)) * 60.0
    score_thresh = float(log_cfg.get("score_band_change_threshold", 5.0))
    now = time.time()

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_pkt = now_utc + datetime.timedelta(hours=5)
    ts_utc = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    ts_pkt = now_pkt.strftime("%Y-%m-%d %H:%M:%S PKT")

    rec = signal_dict.get("recommendation", "HOLD")
    score = float(signal_dict.get("score", signal_dict.get("confidence", 50.0)))
    price = float(signal_dict.get("price", 0.0))
    entry = float(signal_dict.get("suggestedEntry", price))
    target = float(signal_dict.get("targetPrice", price * 1.045))
    stop = float(signal_dict.get("stopLoss", price * 0.975))
    risk = signal_dict.get("riskLevel", "Medium")
    status = signal_dict.get("marketStatus", "Unknown")
    freshness = float(signal_dict.get("data_freshness_sec", 0.0))
    engine_ver = config.get("engine_version", "v1")
    factors = json.dumps(signal_dict.get("factors", []))

    with get_db_connection(db_file) as conn:
        row = conn.execute("""
            SELECT id, score, recommendation, created_at
            FROM live_signals
            WHERE symbol = ?
            ORDER BY created_at DESC
            LIMIT 1
        """, (symbol,)).fetchone()

        if row:
            last_rec = row["recommendation"]
            last_score = row["score"]
            last_time = row["created_at"]

            rec_changed = (rec != last_rec)
            score_shifted = (abs(score - last_score) >= score_thresh)
            time_elapsed = ((now - last_time) >= throttle_sec)

            # Throttle gate: if neither rec changed nor score shifted significantly,
            # and throttle interval hasn't elapsed, skip logging.
            if not rec_changed and not score_shifted and not time_elapsed:
                return None

        cursor = conn.execute("""
            INSERT INTO live_signals (
                symbol, timestamp_utc, timestamp_pkt, price, score, recommendation,
                factor_breakdown, entry, target, stop_loss, risk_level, market_status,
                data_freshness_sec, engine_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            symbol, ts_utc, ts_pkt, price, score, rec,
            factors, entry, target, stop, risk, status,
            freshness, engine_ver, now
        ))
        conn.commit()
        return cursor.lastrowid


def evaluate_single_outcome(
    recommendation: str,
    entry: float,
    target: float,
    stop_loss: float,
    bars: List[Dict[str, Any]]
) -> Tuple[str, Optional[float], Optional[int], str]:
    """
    Evaluates whether target or stop was hit across a series of completed daily bars.
    Returns: (outcome_status, r_multiple, days_to_resolution, notes)
    """
    if not bars:
        return "PENDING", None, None, "Insufficient forward bars"

    is_buy = "BUY" in recommendation.upper()
    is_sell = "SELL" in recommendation.upper()

    risk_per_share = abs(entry - stop_loss)
    if risk_per_share <= 0:
        risk_per_share = entry * 0.025

    for day_idx, bar in enumerate(bars, start=1):
        high = float(bar.get("high", bar.get("close", entry)))
        low = float(bar.get("low", bar.get("close", entry)))

        if is_buy:
            hit_target = (high >= target)
            hit_stop = (low <= stop_loss)
            if hit_target and hit_stop:
                # Ambiguous: both breached on same session -> conservative loss
                return "AMBIGUOUS_LOSS", -1.0, day_idx, f"Both target ({target}) & stop ({stop_loss}) touched on session {day_idx}"
            if hit_target:
                r = round((target - entry) / risk_per_share, 2)
                return "TARGET_HIT", r, day_idx, f"Target touched on session {day_idx} (+{r}R)"
            if hit_stop:
                return "STOP_HIT", -1.0, day_idx, f"Stop-loss touched on session {day_idx} (-1.0R)"

        elif is_sell:
            hit_target = (low <= target)
            hit_stop = (high >= stop_loss)
            if hit_target and hit_stop:
                return "AMBIGUOUS_LOSS", -1.0, day_idx, f"Both target ({target}) & stop ({stop_loss}) touched on session {day_idx}"
            if hit_target:
                r = round((entry - target) / risk_per_share, 2)
                return "TARGET_HIT", r, day_idx, f"Sell target touched on session {day_idx} (+{r}R)"
            if hit_stop:
                return "STOP_HIT", -1.0, day_idx, f"Sell stop touched on session {day_idx} (-1.0R)"

    # If neither hit after all bars
    last_close = float(bars[-1].get("close", entry))
    unrealized_r = round(((last_close - entry) if is_buy else (entry - last_close)) / risk_per_share, 2)
    return "OPEN", unrealized_r, len(bars), f"Active after {len(bars)} sessions"


def track_signal_outcomes(history_fetcher=None, db_file: Optional[Path] = None) -> int:
    """
    Background evaluation job: reads pending signals, evaluates against completed daily bars,
    and records 1D/3D outcome, R-multiple, and resolution days.
    """
    init_db(db_file)
    updated_count = 0

    with get_db_connection(db_file) as conn:
        pending = conn.execute("""
            SELECT id, symbol, recommendation, entry, target, stop_loss, created_at, outcome_1d, outcome_3d
            FROM live_signals
            WHERE outcome_1d = 'PENDING' OR outcome_3d = 'PENDING'
            ORDER BY created_at ASC
        """).fetchall()

        if not pending:
            return 0

        for sig in pending:
            sym = sig["symbol"]
            rec = sig["recommendation"]
            if rec == "HOLD":
                # Mark HOLD as resolved/neutral after 3 sessions
                conn.execute("""
                    UPDATE live_signals
                    SET outcome_1d = 'HOLD_NEUTRAL', outcome_3d = 'HOLD_NEUTRAL', r_multiple = 0.0, days_to_resolution = 1
                    WHERE id = ?
                """, (sig["id"],))
                updated_count += 1
                continue

            # Fetch completed bars following signal timestamp
            bars = []
            if history_fetcher:
                bars = history_fetcher(sym, sig["created_at"])

            if not bars:
                continue

            entry = sig["entry"]
            target = sig["target"]
            stop = sig["stop_loss"]

            # 1-day outcome (evaluating first forward session)
            out_1d, r_1d, days_1d, note_1d = evaluate_single_outcome(rec, entry, target, stop, bars[:1])
            
            # 3-day outcome (evaluating up to 3 forward sessions)
            out_3d, r_3d, days_3d, note_3d = evaluate_single_outcome(rec, entry, target, stop, bars[:3])

            final_r = r_3d if out_3d in ("TARGET_HIT", "STOP_HIT", "AMBIGUOUS_LOSS") else (r_1d if out_1d in ("TARGET_HIT", "STOP_HIT") else None)
            final_days = days_3d if out_3d in ("TARGET_HIT", "STOP_HIT") else days_1d
            notes = note_3d if len(bars) >= 3 else note_1d

            conn.execute("""
                UPDATE live_signals
                SET outcome_1d = ?, outcome_3d = ?, r_multiple = ?, days_to_resolution = ?, resolution_notes = ?
                WHERE id = ?
            """, (out_1d, out_3d, final_r, final_days, notes, sig["id"]))
            updated_count += 1

        conn.commit()

    return updated_count


def get_signal_statistics(db_file: Optional[Path] = None) -> Dict[str, Any]:
    """
    Computes empirical performance statistics across all logged signals:
    hit rate %, average R-multiple, count per recommendation tier.
    """
    init_db(db_file)
    with get_db_connection(db_file) as conn:
        all_signals = conn.execute("SELECT * FROM live_signals ORDER BY created_at DESC").fetchall()

    total_count = len(all_signals)
    tiers = ["STRONG BUY", "BUY", "HOLD", "SELL", "STRONG SELL"]
    tier_stats = {t: {"count": 0, "wins": 0, "losses": 0, "ambiguous": 0, "win_rate": 0.0, "avg_r": 0.0, "total_r": 0.0} for t in tiers}

    resolved_count = 0
    total_wins = 0
    total_r_sum = 0.0

    for s in all_signals:
        rec = s["recommendation"].upper()
        if rec not in tier_stats:
            rec = "HOLD"

        tier_stats[rec]["count"] += 1
        out = (s["outcome_3d"] or s["outcome_1d"] or "PENDING").upper()
        r = s["r_multiple"]

        if out in ("TARGET_HIT", "STOP_HIT", "AMBIGUOUS_LOSS"):
            resolved_count += 1
            if r is not None:
                total_r_sum += r
                tier_stats[rec]["total_r"] += r

            if out == "TARGET_HIT":
                total_wins += 1
                tier_stats[rec]["wins"] += 1
            elif out == "STOP_HIT":
                tier_stats[rec]["losses"] += 1
            elif out == "AMBIGUOUS_LOSS":
                tier_stats[rec]["ambiguous"] += 1
                tier_stats[rec]["losses"] += 1

    # Calculate percentages
    min_sample = 30
    for t in tiers:
        res_t = tier_stats[t]["wins"] + tier_stats[t]["losses"]
        if res_t > 0:
            tier_stats[t]["win_rate"] = round((tier_stats[t]["wins"] / res_t) * 100.0, 1)
            tier_stats[t]["avg_r"] = round(tier_stats[t]["total_r"] / res_t, 2)
        else:
            tier_stats[t]["win_rate"] = 0.0
            tier_stats[t]["avg_r"] = 0.0

        # Stage 6: Wilson CI and minimum sample size warning
        w_low, w_high = compute_wilson_ci(tier_stats[t]["wins"], res_t)
        has_warning = res_t < min_sample
        tier_stats[t]["wilson_ci_95"] = {"lower_pct": w_low, "upper_pct": w_high, "text": f"[{w_low}% – {w_high}%]" if res_t > 0 else "N/A"}
        tier_stats[t]["sample_size_warning"] = has_warning
        tier_stats[t]["display_win_rate_pct"] = tier_stats[t]["win_rate"] if not has_warning else None
        tier_stats[t]["warning_text"] = f"Sample size ({res_t}) is below threshold (30)" if has_warning else None

    overall_win_rate = round((total_wins / resolved_count * 100.0), 1) if resolved_count > 0 else 0.0
    overall_avg_r = round((total_r_sum / resolved_count), 2) if resolved_count > 0 else 0.0
    o_low, o_high = compute_wilson_ci(total_wins, resolved_count)
    o_has_warning = resolved_count < min_sample

    recent_signals = []
    for s in all_signals[:15]:
        recent_signals.append({
            "id": s["id"],
            "symbol": s["symbol"],
            "timestamp_pkt": s["timestamp_pkt"],
            "price": s["price"],
            "score": s["score"],
            "recommendation": s["recommendation"],
            "entry": s["entry"],
            "target": s["target"],
            "stop_loss": s["stop_loss"],
            "outcome_1d": s["outcome_1d"],
            "outcome_3d": s["outcome_3d"],
            "r_multiple": s["r_multiple"],
            "engine_version": s["engine_version"]
        })

    return {
        "success": True,
        "total_signals": total_count,
        "resolved_signals": resolved_count,
        "overall_win_rate_pct": overall_win_rate,
        "overall_display_win_rate_pct": overall_win_rate if not o_has_warning else None,
        "overall_sample_size_warning": o_has_warning,
        "overall_wilson_ci_95": {"lower_pct": o_low, "upper_pct": o_high, "text": f"[{o_low}% – {o_high}%]" if resolved_count > 0 else "N/A"},
        "overall_avg_r_multiple": overall_avg_r,
        "tier_breakdown": tier_stats,
        "recent_signals": recent_signals
    }


def get_symbol_signals(symbol: str, limit: int = 10, db_file: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Stage 8.4: Fetches the last N logged signals and outcomes specifically for a given symbol."""
    init_db(db_file)
    symbol = symbol.upper().strip()
    with get_db_connection(db_file) as conn:
        rows = conn.execute("""
            SELECT * FROM live_signals
            WHERE symbol = ?
            ORDER BY id DESC
            LIMIT ?
        """, (symbol, limit)).fetchall()

        signals = []
        for r in rows:
            signals.append({
                "id": r["id"],
                "symbol": r["symbol"],
                "timestamp_pkt": r["timestamp_pkt"],
                "price": r["price"],
                "score": r["score"],
                "recommendation": r["recommendation"],
                "entry": r["entry"],
                "target": r["target"],
                "stop_loss": r["stop_loss"],
                "risk_level": r["risk_level"],
                "outcome_1d": r["outcome_1d"],
                "outcome_3d": r["outcome_3d"],
                "r_multiple": r["r_multiple"],
                "days_to_resolution": r["days_to_resolution"],
                "engine_version": r["engine_version"]
            })
        return signals

