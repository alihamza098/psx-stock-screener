#!/usr/bin/env python3
"""
PSX Today's Opportunities — Same-Day Trade Scanner Engine
=========================================================
Continuous scanner for same-day intraday trading setups on PSX:
- Tiers: Steady (~3%), Active (~5%), Momentum (~8-10%)
- Setups: ORB, Gap-and-Go, VWAP Reclaim, Circuit Runner, Oversold Bounce
- Circuit-awareness: Target clamping & minimum headroom gate
- Lifecycle: WATCHING -> TRIGGERED -> TARGET_HIT / STOPPED_OUT / TIME_EXIT -> CLOSED
- Mandatory time-exit: 15 minutes before close (Mon-Thu 15:15, Fri 16:15 PKT)
- Friday Jummah-break hold configuration
- 100% Deterministic, SQLite persistence, startup reconciliation
"""

import json
import math
import time
import sqlite3
import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import psx_calendar
from technical_indicators import (
    compute_rsi_series,
    compute_vwap,
    compute_classical_pivots,
    compute_atr_series,
    compute_ema_series
)
from shared_trading_utils import (
    calculate_position_size,
    compute_trade_brackets,
    check_liquidity_gate,
    compute_rvol_today,
    compute_circuit_room,
    get_session_schedule
)

CONFIG_PATH = Path(__file__).parent / "config" / "daily_opportunities.json"
CACHE_DIR = Path(__file__).parent / "cache"
DB_PATH = CACHE_DIR / "daily_opportunities.db"


def load_daily_config() -> Dict[str, Any]:
    """Load configuration from config/daily_opportunities.json with reliable fallbacks."""
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "enabled": True,
        "tiers": {
            "steady": {"name": "Steady", "target_pct": 3.0, "rvol_min": 1.0, "rvol_max": 1.5, "atr_pct_max": 2.5, "stop_atr_mult": 1.2},
            "active": {"name": "Active", "target_pct": 5.0, "rvol_min": 1.5, "rvol_max": 3.0, "atr_pct_min": 2.5, "stop_atr_mult": 1.5},
            "momentum": {"name": "Momentum", "target_pct": 8.0, "rvol_min": 3.0, "stop_atr_mult": 2.0, "circuit_room_min_pct": 1.5}
        },
        "circuit_gate": {
            "circuit_limit_default_pct": 7.5,
            "safety_buffer_pct": 0.3,
            "min_worth_it_room_pct": 1.5,
            "tighten_trailing_room_pct": 0.5
        },
        "lifecycle": {
            "watch_expiry_mon_thu": "13:30",
            "watch_expiry_friday_s1": "11:30",
            "watch_expiry_friday_s2": "15:30",
            "time_exit_minutes_before_close": 15,
            "allow_hold_through_jummah_break": True,
            "db_path": "cache/daily_opportunities.db"
        },
        "risk_and_sizing": {
            "account_capital_pkr": 500000.0,
            "max_risk_per_trade_pct": 1.0,
            "max_concurrent_triggered": 4,
            "max_concurrent_watching": 10,
            "regime_bear_risk_multiplier": 0.5
        }
    }


# ── SQLite Persistence ─────────────────────────────────────────────────────────

class DailyOpportunitiesDB:
    """Thread-safe SQLite storage for candidate lifecycle, transitions, and history."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_tables()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_tables(self):
        with self._get_conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS daily_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    setup_type TEXT NOT NULL,
                    state TEXT NOT NULL,
                    trigger_condition TEXT,
                    detected_at TEXT NOT NULL,
                    watch_expires_at TEXT,
                    entry_price REAL,
                    target_price REAL,
                    stop_loss REAL,
                    current_price REAL,
                    exit_price REAL,
                    exit_type TEXT,
                    exit_time TEXT,
                    exit_notes TEXT,
                    pnl_pct REAL DEFAULT 0.0,
                    pnl_pkr REAL DEFAULT 0.0,
                    shares INTEGER DEFAULT 0,
                    outlay_pkr REAL DEFAULT 0.0,
                    risk_pkr REAL DEFAULT 0.0,
                    duration_minutes INTEGER DEFAULT 0,
                    circuit_room_pct REAL DEFAULT 7.5,
                    execution_risk_note TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tier_transitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    from_tier TEXT NOT NULL,
                    to_tier TEXT NOT NULL,
                    rvol REAL,
                    atr_pct REAL,
                    circuit_room_pct REAL,
                    timestamp TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS near_miss_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    setup_type TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_candidates_date_sym ON daily_candidates (date, symbol);
                CREATE INDEX IF NOT EXISTS idx_candidates_state ON daily_candidates (state);
            """)

    def save_candidate(self, cand: Dict[str, Any]) -> int:
        now_str = psx_calendar.get_current_pkt_datetime().strftime("%Y-%m-%d %H:%M:%S")
        date_str = cand.get("date") or psx_calendar.get_current_pkt_datetime().strftime("%Y-%m-%d")
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO daily_candidates (
                    date, symbol, tier, setup_type, state, trigger_condition,
                    detected_at, watch_expires_at, entry_price, target_price,
                    stop_loss, current_price, exit_price, exit_type, exit_time,
                    exit_notes, pnl_pct, pnl_pkr, shares, outlay_pkr, risk_pkr,
                    duration_minutes, circuit_room_pct, execution_risk_note,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                date_str,
                cand["symbol"],
                cand["tier"],
                cand["setup_type"],
                cand.get("state", "WATCHING"),
                cand.get("trigger_condition", ""),
                cand.get("detected_at", now_str),
                cand.get("watch_expires_at"),
                cand.get("entry_price", 0.0),
                cand.get("target_price", 0.0),
                cand.get("stop_loss", 0.0),
                cand.get("current_price", cand.get("entry_price", 0.0)),
                cand.get("exit_price"),
                cand.get("exit_type"),
                cand.get("exit_time"),
                cand.get("exit_notes"),
                cand.get("pnl_pct", 0.0),
                cand.get("pnl_pkr", 0.0),
                cand.get("shares", 0),
                cand.get("outlay_pkr", 0.0),
                cand.get("risk_pkr", 0.0),
                cand.get("duration_minutes", 0),
                cand.get("circuit_room_pct", 7.5),
                cand.get("execution_risk_note"),
                now_str,
                now_str
            ))
            return cur.lastrowid

    def update_candidate(self, cand_id: int, updates: Dict[str, Any]):
        now_str = psx_calendar.get_current_pkt_datetime().strftime("%Y-%m-%d %H:%M:%S")
        fields = list(updates.keys())
        values = list(updates.values())
        fields.append("updated_at")
        values.append(now_str)
        set_clause = ", ".join([f"{f} = ?" for f in fields])
        values.append(cand_id)
        with self._get_conn() as conn:
            conn.execute(f"UPDATE daily_candidates SET {set_clause} WHERE id = ?", values)

    def get_open_candidates(self, date_str: Optional[str] = None) -> List[Dict[str, Any]]:
        if not date_str:
            date_str = psx_calendar.get_current_pkt_datetime().strftime("%Y-%m-%d")
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM daily_candidates
                WHERE date = ? AND state IN ('WATCHING', 'TRIGGERED')
                ORDER BY id DESC
            """, (date_str,)).fetchall()
            return [dict(r) for r in rows]

    def get_all_today(self, date_str: Optional[str] = None) -> List[Dict[str, Any]]:
        if not date_str:
            date_str = psx_calendar.get_current_pkt_datetime().strftime("%Y-%m-%d")
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM daily_candidates
                WHERE date = ?
                ORDER BY id DESC
            """, (date_str,)).fetchall()
            return [dict(r) for r in rows]

    def log_tier_transition(self, symbol: str, from_tier: str, to_tier: str, rvol: float, atr_pct: float, circuit_room: float):
        now = psx_calendar.get_current_pkt_datetime()
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO tier_transitions (date, symbol, from_tier, to_tier, rvol, atr_pct, circuit_room_pct, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                now.strftime("%Y-%m-%d"),
                symbol,
                from_tier,
                to_tier,
                rvol,
                atr_pct,
                circuit_room,
                now.strftime("%Y-%m-%d %H:%M:%S")
            ))

    def log_near_miss(self, symbol: str, setup_type: str, reason: str):
        now = psx_calendar.get_current_pkt_datetime()
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO near_miss_logs (date, symbol, setup_type, reason, timestamp)
                VALUES (?, ?, ?, ?, ?)
            """, (
                now.strftime("%Y-%m-%d"),
                symbol,
                setup_type,
                reason,
                now.strftime("%Y-%m-%d %H:%M:%S")
            ))


# ── Stage 1: Volatility / Momentum Tier Classifier ─────────────────────────────

class TierClassifier:
    """
    Classifies symbols into Steady / Active / Momentum tiers using intraday RVOL, ATR%,
    circuit headroom, and active upper-lock anomaly detection.
    """

    def __init__(self, db: DailyOpportunitiesDB, config: Optional[Dict[str, Any]] = None):
        self.db = db
        self.config = config or load_daily_config()
        self._last_known_tiers: Dict[str, str] = {}

    def classify_tier(
        self,
        symbol: str,
        current_price: float,
        change_pct: float,
        today_volume: float,
        avg_volume_21d: float,
        atr14: float,
        has_circuit_anomaly: bool = False,
        schedule: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        cfg = self.config
        tiers_cfg = cfg.get("tiers", {})

        # Compute ATR%
        atr_pct = (atr14 / current_price * 100.0) if current_price > 0 else 2.5

        # Compute circuit room
        c_room = compute_circuit_room(current_price, change_pct)
        room_pct = c_room["room_to_upper_pct"]

        # Compute RVOL so far today
        sched = schedule or get_session_schedule()
        elapsed = sched.get("elapsed_minutes", 60)
        total_mins = sched.get("total_session_minutes", 358)
        rvol_today = compute_rvol_today(today_volume, avg_volume_21d, elapsed, total_mins)

        # Thresholds
        mom_rvol_min = float(tiers_cfg.get("momentum", {}).get("rvol_min", 3.0))
        mom_min_room = float(tiers_cfg.get("momentum", {}).get("circuit_room_min_pct", 1.5))
        act_rvol_min = float(tiers_cfg.get("active", {}).get("rvol_min", 1.5))
        act_atr_min = float(tiers_cfg.get("active", {}).get("atr_pct_min", 2.5))

        # Classification Rule
        is_momentum = False
        is_active = False

        if (rvol_today >= mom_rvol_min and room_pct >= mom_min_room) or (has_circuit_anomaly and room_pct >= 0.5):
            tier = "Momentum"
            is_momentum = True
        elif rvol_today >= act_rvol_min or atr_pct >= act_atr_min:
            tier = "Active"
            is_active = True
        else:
            tier = "Steady"

        # Log tier transition if changed
        prev_tier = self._last_known_tiers.get(symbol)
        if prev_tier and prev_tier != tier:
            self.db.log_tier_transition(symbol, prev_tier, tier, rvol_today, atr_pct, room_pct)
        self._last_known_tiers[symbol] = tier

        tier_info = tiers_cfg.get(tier.lower(), {})
        target_pct = float(tier_info.get("target_pct", 3.0))

        return {
            "tier": tier,
            "atr_pct": round(atr_pct, 2),
            "rvol_today": rvol_today,
            "circuit_room_pct": room_pct,
            "target_pct": target_pct,
            "execution_risk_note": tier_info.get("execution_risk_note") if is_momentum else None,
            "has_circuit_anomaly": has_circuit_anomaly
        }


# ── Stage 2: Setup Detectors ───────────────────────────────────────────────────

class SetupDetectors:
    """Deterministic setup detectors for same-day opportunities."""

    def __init__(self, db: DailyOpportunitiesDB, config: Optional[Dict[str, Any]] = None):
        self.db = db
        self.config = config or load_daily_config()

    def detect_orb(
        self,
        symbol: str,
        tier_data: Dict[str, Any],
        current_price: float,
        change_pct: float,
        intraday_candles: List[Dict[str, Any]],
        atr14: float,
        schedule: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Opening Range Breakout (first 15m)."""
        cfg = self.config.get("setups", {}).get("orb", {})
        if not cfg.get("enabled", True):
            return None

        elapsed = schedule.get("elapsed_minutes", 0)
        or_mins = int(cfg.get("opening_range_minutes", 15))
        if elapsed < or_mins:
            return None  # Opening range still forming

        if not intraday_candles or len(intraday_candles) < 1:
            return None

        # Determine OR High/Low from first candles
        or_candles = intraday_candles[:max(1, or_mins // 5)]
        or_high = max(float(c.get("high", c.get("price", current_price))) for c in or_candles)
        or_low = min(float(c.get("low", c.get("price", current_price))) for c in or_candles)
        or_height = max(0.10, or_high - or_low)

        # Trigger Condition: Current price > OR high with volume confirmation
        rvol = tier_data["rvol_today"]
        vol_mult = float(cfg.get("volume_confirmation_mult", 1.3))

        if current_price > or_high and rvol >= vol_mult:
            if change_pct > float(cfg.get("max_breakout_chg_pct", 6.0)):
                self.db.log_near_miss(symbol, "ORB", f"Price already extended (+{change_pct}%)")
                return None

            entry = current_price
            raw_target = entry + or_height
            stop = or_low if (entry - or_low) <= (atr14 * 1.5) else round(entry - (atr14 * 1.2), 2)

            return {
                "symbol": symbol,
                "setup_type": "ORB",
                "tier": tier_data["tier"],
                "trigger_condition": f"Price crossed above {or_mins}m OR High ({or_high:.2f}) with RVOL {rvol:.1f}x",
                "entry_price": entry,
                "raw_target": raw_target,
                "stop_loss": stop,
                "or_high": or_high,
                "or_low": or_low
            }
        else:
            if current_price > or_high:
                self.db.log_near_miss(symbol, "ORB", f"Price above OR high but RVOL {rvol:.1f}x < {vol_mult}x")
            return None

    def detect_gap_and_go(
        self,
        symbol: str,
        tier_data: Dict[str, Any],
        current_price: float,
        open_price: float,
        prior_close: float,
        atr14: float,
        schedule: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Gap-and-Go continuation."""
        cfg = self.config.get("setups", {}).get("gap_and_go", {})
        if not cfg.get("enabled", True):
            return None

        if prior_close <= 0 or open_price <= 0:
            return None

        gap_pct = ((open_price - prior_close) / prior_close) * 100.0
        min_gap = float(cfg.get("min_gap_pct", 1.5))
        max_gap = float(cfg.get("max_gap_pct", 5.5))

        if gap_pct < min_gap:
            return None
        if gap_pct > max_gap:
            self.db.log_near_miss(symbol, "GAP_AND_GO", f"Gap too wide ({gap_pct:.2f}% > {max_gap}%)")
            return None

        # Price holds above open price
        if current_price >= open_price * 0.998:
            rvol = tier_data["rvol_today"]
            vol_mult = float(cfg.get("volume_confirmation_mult", 1.3))
            if rvol < vol_mult:
                self.db.log_near_miss(symbol, "GAP_AND_GO", f"Gap held but RVOL {rvol:.1f}x < {vol_mult}x")
                return None

            entry = current_price
            raw_target = entry + (atr14 * 1.5)
            stop = round(min(open_price * 0.995, prior_close), 2)

            return {
                "symbol": symbol,
                "setup_type": "GAP_AND_GO",
                "tier": tier_data["tier"],
                "trigger_condition": f"Opened +{gap_pct:.1f}% gap over prior close ({prior_close:.2f}) and holding above open ({open_price:.2f}) with RVOL {rvol:.1f}x",
                "entry_price": entry,
                "raw_target": raw_target,
                "stop_loss": stop
            }
        else:
            self.db.log_near_miss(symbol, "GAP_AND_GO", f"Price filled gap below open ({current_price:.2f} < {open_price:.2f})")
            return None

    def detect_vwap_reclaim(
        self,
        symbol: str,
        tier_data: Dict[str, Any],
        current_price: float,
        vwap: float,
        intraday_low: float,
        atr14: float
    ) -> Optional[Dict[str, Any]]:
        """VWAP Reclaim / Pullback."""
        cfg = self.config.get("setups", {}).get("vwap_reclaim", {})
        if not cfg.get("enabled", True):
            return None

        if vwap <= 0:
            return None

        pullback_thresh = float(cfg.get("max_pullback_from_vwap_pct", 0.8))
        diff_from_vwap_pct = ((current_price - vwap) / vwap) * 100.0

        # Pullback happened: low touched near or below VWAP, and current price reclaimed above VWAP
        low_diff_pct = ((intraday_low - vwap) / vwap) * 100.0
        pulled_back = low_diff_pct <= 0.2  # low touched within 0.2% of VWAP or dipped below
        reclaimed = current_price > vwap and diff_from_vwap_pct <= pullback_thresh

        if pulled_back and reclaimed:
            entry = current_price
            raw_target = entry + (atr14 * 1.5)
            stop = round(vwap * (1.0 - (float(cfg.get("stop_buffer_below_vwap_pct", 0.5)) / 100.0)), 2)

            return {
                "symbol": symbol,
                "setup_type": "VWAP_RECLAIM",
                "tier": tier_data["tier"],
                "trigger_condition": f"Pullback to VWAP ({vwap:.2f}) successfully reclaimed, trading at {current_price:.2f}",
                "entry_price": entry,
                "raw_target": raw_target,
                "stop_loss": stop
            }
        return None

    def detect_circuit_runner(
        self,
        symbol: str,
        tier_data: Dict[str, Any],
        current_price: float,
        change_pct: float,
        atr14: float
    ) -> Optional[Dict[str, Any]]:
        """Circuit Runner Momentum."""
        cfg = self.config.get("setups", {}).get("circuit_runner", {})
        if not cfg.get("enabled", True):
            return None

        near_circuit = float(cfg.get("near_circuit_pct", 5.5))
        rvol_min = float(cfg.get("min_rvol", 2.5))
        rvol = tier_data["rvol_today"]

        is_lock_setup = tier_data.get("has_circuit_anomaly", False) or (change_pct >= near_circuit and rvol >= rvol_min)

        if is_lock_setup:
            c_room = compute_circuit_room(current_price, change_pct)
            upper_limit = c_room["upper_circuit_price"]
            safety = 0.3 / 100.0
            raw_target = round(upper_limit * (1.0 - safety), 2)
            stop_mult = float(cfg.get("stop_atr_mult", 2.0))
            stop = round(current_price - (atr14 * stop_mult), 2)

            return {
                "symbol": symbol,
                "setup_type": "CIRCUIT_RUNNER",
                "tier": "Momentum",
                "trigger_condition": f"Upper circuit momentum lock setup (+{change_pct:.1f}%, RVOL {rvol:.1f}x)",
                "entry_price": current_price,
                "raw_target": raw_target,
                "stop_loss": stop,
                "execution_risk_note": "⚠️ Execution Risk: Circuit-lock fills are not guaranteed — liquidity may dry up immediately."
            }
        return None

    def detect_oversold_bounce(
        self,
        symbol: str,
        tier_data: Dict[str, Any],
        current_price: float,
        intraday_rsi: float,
        pivots: Dict[str, float],
        atr14: float
    ) -> Optional[Dict[str, Any]]:
        """Oversold Intraday Bounce."""
        cfg = self.config.get("setups", {}).get("oversold_bounce", {})
        if not cfg.get("enabled", True):
            return None

        rsi_thresh = float(cfg.get("rsi_oversold_threshold", 32.0))
        if intraday_rsi > rsi_thresh:
            return None

        s1 = pivots.get("s1", 0.0)
        s2 = pivots.get("s2", 0.0)
        pivot = pivots.get("pivot", 0.0)
        support = s1 if s1 > 0 else (s2 if s2 > 0 else current_price * 0.98)

        # Current price near support
        prox_pct = float(cfg.get("pivot_proximity_pct", 0.8)) / 100.0
        if current_price >= support * (1.0 - prox_pct):
            entry = current_price
            raw_target = round(pivot if pivot > entry else entry + (atr14 * 1.2), 2)
            stop = round(support * (1.0 - (float(cfg.get("stop_buffer_below_support_pct", 0.5)) / 100.0)), 2)

            return {
                "symbol": symbol,
                "setup_type": "OVERSOLD_BOUNCE",
                "tier": tier_data["tier"],
                "trigger_condition": f"Intraday RSI oversold ({intraday_rsi:.1f}) near support S1 ({support:.2f})",
                "entry_price": entry,
                "raw_target": raw_target,
                "stop_loss": stop
            }
        return None


# ── Stage 3: Circuit-Awareness & Achievability Gate ─────────────────────────────

class CircuitAchievabilityGate:
    """Clamps target price to PSX upper circuit and filters out unreachable setups."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or load_daily_config()

    def process_candidate(
        self,
        cand: Dict[str, Any],
        current_price: float,
        change_pct: float,
        circuit_limit_pct: float = 7.5
    ) -> Optional[Dict[str, Any]]:
        cg_cfg = self.config.get("circuit_gate", {})
        min_room = float(cg_cfg.get("min_worth_it_room_pct", 1.5))
        safety_buf = float(cg_cfg.get("safety_buffer_pct", 0.3))

        c_room = compute_circuit_room(current_price, change_pct, circuit_limit_pct)
        room_pct = c_room["room_to_upper_pct"]

        # 3.1: Drop candidate entirely if room to circuit is under minimum worth-it threshold
        if room_pct < min_room:
            return None  # Not enough upside headroom before upper circuit lock

        upper_price = c_room["upper_circuit_price"]
        max_achievable_target = round(upper_price * (1.0 - (safety_buf / 100.0)), 2)

        raw_target = cand.get("raw_target", current_price * 1.03)
        entry = cand.get("entry_price", current_price)

        # Clamp target to max achievable ceiling
        target = min(raw_target, max_achievable_target)

        # Verify target is still above entry and provides minimum profit
        if target <= entry or ((target - entry) / entry * 100.0) < 1.0:
            return None

        cand["target_price"] = target
        cand["circuit_room_pct"] = room_pct
        cand["max_achievable_target"] = max_achievable_target

        # 3.2: Flag if room is shrinking toward 0 (near circuit)
        tighten_thresh = float(cg_cfg.get("tighten_trailing_room_pct", 0.5))
        cand["near_circuit_flag"] = (room_pct <= tighten_thresh)

        return cand


# ── Stage 4: Lifecycle State Machine & Time-Exit Scheduler ──────────────────────

class LifecycleStateMachine:
    """
    Manages state transitions: WATCHING -> TRIGGERED -> TARGET_HIT / STOPPED_OUT / TIME_EXIT -> CLOSED
    Enforces forced time-exit before session close (Mon-Thu 15:15, Fri 16:15 PKT).
    Reconciles on startup.
    """

    def __init__(self, db: DailyOpportunitiesDB, config: Optional[Dict[str, Any]] = None):
        self.db = db
        self.config = config or load_daily_config()

    def update_open_candidate(
        self,
        cand: Dict[str, Any],
        live_price: float,
        schedule: Dict[str, Any],
        dt: Optional[datetime.datetime] = None
    ) -> Dict[str, Any]:
        """Check target, stop, watch expiry, and forced time-exit."""
        if dt is None:
            dt = psx_calendar.get_current_pkt_datetime()

        cand_id = cand["id"]
        state = cand["state"]
        entry = float(cand.get("entry_price") or live_price)
        target = float(cand.get("target_price") or entry * 1.03)
        stop = float(cand.get("stop_loss") or entry * 0.98)
        shares = int(cand.get("shares") or 0)
        now_str = dt.strftime("%Y-%m-%d %H:%M:%S")

        cur_mins = dt.hour * 60 + dt.minute
        time_exit_mins = schedule.get("time_exit_mins", 915)  # 15:15 or 16:15
        is_past_time_exit = cur_mins >= time_exit_mins

        # 4.3: Mandatory Time-Exit Cutoff
        if state == "TRIGGERED" and is_past_time_exit:
            pnl_pct = round(((live_price - entry) / entry) * 100.0, 2)
            pnl_pkr = round((live_price - entry) * shares, 2)
            updates = {
                "state": "CLOSED",
                "exit_type": "TIME_EXIT",
                "exit_price": live_price,
                "exit_time": now_str,
                "exit_notes": f"Forced end-of-day time exit at {schedule.get('time_exit_cutoff_str', 'close')}",
                "pnl_pct": pnl_pct,
                "pnl_pkr": pnl_pkr,
                "current_price": live_price
            }
            self.db.update_candidate(cand_id, updates)
            cand.update(updates)
            return cand

        # 4.2: WATCHING auto-expiry
        if state == "WATCHING":
            # Check if watch expired
            expiry_str = cand.get("watch_expires_at")
            if expiry_str:
                try:
                    exp_dt = datetime.datetime.strptime(expiry_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=psx_calendar.PKT_TIMEZONE)
                    if dt >= exp_dt or is_past_time_exit:
                        updates = {
                            "state": "CLOSED",
                            "exit_type": "WATCH_EXPIRED",
                            "exit_time": now_str,
                            "exit_notes": "Expired without triggering",
                            "current_price": live_price
                        }
                        self.db.update_candidate(cand_id, updates)
                        cand.update(updates)
                        return cand
                except Exception:
                    pass

            # Check if triggered now (price crossed entry)
            if live_price >= entry:
                updates = {
                    "state": "TRIGGERED",
                    "current_price": live_price
                }
                self.db.update_candidate(cand_id, updates)
                cand.update(updates)
                state = "TRIGGERED"

        # Check Target / Stop if TRIGGERED
        if state == "TRIGGERED":
            if live_price >= target:
                pnl_pct = round(((live_price - entry) / entry) * 100.0, 2)
                pnl_pkr = round((live_price - entry) * shares, 2)
                updates = {
                    "state": "CLOSED",
                    "exit_type": "TARGET_HIT",
                    "exit_price": live_price,
                    "exit_time": now_str,
                    "exit_notes": f"Target hit ({target:.2f})",
                    "pnl_pct": pnl_pct,
                    "pnl_pkr": pnl_pkr,
                    "current_price": live_price
                }
                self.db.update_candidate(cand_id, updates)
                cand.update(updates)
            elif live_price <= stop:
                pnl_pct = round(((live_price - entry) / entry) * 100.0, 2)
                pnl_pkr = round((live_price - entry) * shares, 2)
                updates = {
                    "state": "CLOSED",
                    "exit_type": "STOPPED_OUT",
                    "exit_price": live_price,
                    "exit_time": now_str,
                    "exit_notes": f"Stop loss hit ({stop:.2f})",
                    "pnl_pct": pnl_pct,
                    "pnl_pkr": pnl_pkr,
                    "current_price": live_price
                }
                self.db.update_candidate(cand_id, updates)
                cand.update(updates)
            else:
                # Update live price and P&L
                pnl_pct = round(((live_price - entry) / entry) * 100.0, 2)
                pnl_pkr = round((live_price - entry) * shares, 2)
                updates = {
                    "current_price": live_price,
                    "pnl_pct": pnl_pct,
                    "pnl_pkr": pnl_pkr
                }
                self.db.update_candidate(cand_id, updates)
                cand.update(updates)

        return cand

    def reconcile_on_startup(self, stocks_cache: Optional[Dict[str, Any]] = None):
        """Reconcile open candidates on server boot to ensure no orphan open trades."""
        dt = psx_calendar.get_current_pkt_datetime()
        today_str = dt.strftime("%Y-%m-%d")
        schedule = get_session_schedule(dt)
        open_cands = self.db.get_open_candidates(today_str)

        cur_mins = dt.hour * 60 + dt.minute
        time_exit_mins = schedule.get("time_exit_mins", 915)
        is_past_exit = cur_mins >= time_exit_mins or not schedule.get("is_in_trading_hours", False)

        for c in open_cands:
            sym = c["symbol"]
            cid = c["id"]
            state = c["state"]
            px = c.get("current_price") or c.get("entry_price") or 10.0
            if stocks_cache and sym in stocks_cache:
                px = float(stocks_cache[sym].get("price", px))

            if state == "TRIGGERED" and is_past_exit:
                entry = float(c.get("entry_price", px))
                shares = int(c.get("shares", 0))
                pnl_pct = round(((px - entry) / entry) * 100.0, 2)
                pnl_pkr = round((px - entry) * shares, 2)
                self.db.update_candidate(cid, {
                    "state": "CLOSED",
                    "exit_type": "TIME_EXIT",
                    "exit_price": px,
                    "exit_time": dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "exit_notes": "Exited late due to downtime / startup reconciliation",
                    "pnl_pct": pnl_pct,
                    "pnl_pkr": pnl_pkr,
                    "current_price": px
                })
            elif state == "WATCHING" and is_past_exit:
                self.db.update_candidate(cid, {
                    "state": "CLOSED",
                    "exit_type": "WATCH_EXPIRED",
                    "exit_time": dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "exit_notes": "Expired during downtime",
                    "current_price": px
                })


# ── Stage 5: Position Sizing & Risk Caps ───────────────────────────────────────

class PositionRiskManager:
    """Manages position sizing, concurrent candidate caps, and market regime scaling."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or load_daily_config()

    def calculate_sizing(
        self,
        entry: float,
        stop: float,
        market_regime: str = "Neutral"
    ) -> Dict[str, Any]:
        risk_cfg = self.config.get("risk_and_sizing", {})
        capital = float(risk_cfg.get("account_capital_pkr", 500000.0))
        risk_pct = float(risk_cfg.get("max_risk_per_trade_pct", 1.0))

        # 5.3: Market Regime Multiplier (scale risk in Bearish/Volatile regime)
        if "bear" in market_regime.lower() or "crash" in market_regime.lower():
            risk_pct *= float(risk_cfg.get("regime_bear_risk_multiplier", 0.5))
        elif "volatile" in market_regime.lower():
            risk_pct *= float(risk_cfg.get("regime_volatile_risk_multiplier", 0.6))

        pos = calculate_position_size(capital, risk_pct, entry, stop)
        return {
            "shares": pos["shares"],
            "outlay_pkr": pos["total_outlay"],
            "risk_pkr": pos["pkr_at_risk"],
            "risk_pct_used": round(risk_pct, 2),
            "capital_pkr": capital
        }


# ── Stage 7: Outcome Tracking (Tier-Separated) ─────────────────────────────────

class OutcomeTracker:
    """Computes win rates and Wilson 95% confidence intervals per (tier x setup_type)."""

    def __init__(self, db: DailyOpportunitiesDB, config: Optional[Dict[str, Any]] = None):
        self.db = db
        self.config = config or load_daily_config()

    def get_track_record(self) -> Dict[str, Any]:
        with self.db._get_conn() as conn:
            rows = conn.execute("""
                SELECT tier, setup_type, state, exit_type, pnl_pct, pnl_pkr, duration_minutes
                FROM daily_candidates
                WHERE state = 'CLOSED' AND exit_type IN ('TARGET_HIT', 'STOPPED_OUT', 'TIME_EXIT')
            """).fetchall()

        groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for r in rows:
            key = (r["tier"], r["setup_type"])
            if key not in groups:
                groups[key] = []
            groups[key].append(dict(r))

        cfg = self.config.get("outcome_tracking", {})
        min_sample = int(cfg.get("min_sample_size_gate", 5))
        z = float(cfg.get("wilson_z", 1.96))

        breakdown = []
        for (tier, setup), items in groups.items():
            n = len(items)
            wins = sum(1 for it in items if float(it.get("pnl_pct", 0) or 0) > 0)
            win_rate = (wins / n) if n > 0 else 0.0

            # Wilson 95% Confidence Interval
            if n > 0:
                denom = 1.0 + (z * z) / n
                center = (win_rate + (z * z) / (2.0 * n)) / denom
                spread = (z * math.sqrt((win_rate * (1.0 - win_rate)) / n + (z * z) / (4.0 * n * n))) / denom
                ci_lower = max(0.0, center - spread)
                ci_upper = min(1.0, center + spread)
            else:
                ci_lower = 0.0
                ci_upper = 0.0

            avg_return = sum(float(it.get("pnl_pct", 0) or 0) for it in items) / n if n > 0 else 0.0
            avg_duration = sum(int(it.get("duration_minutes", 0) or 0) for it in items) / n if n > 0 else 0

            breakdown.append({
                "tier": tier,
                "setup_type": setup,
                "total_trades": n,
                "wins": wins,
                "losses": n - wins,
                "win_rate_pct": round(win_rate * 100.0, 1),
                "ci_95_lower": round(ci_lower * 100.0, 1),
                "ci_95_upper": round(ci_upper * 100.0, 1),
                "is_statistically_valid": n >= min_sample,
                "avg_return_pct": round(avg_return, 2),
                "avg_duration_minutes": round(avg_duration, 1),
                "sample_status": f"{n}/{min_sample} trades" if n < min_sample else "Confident"
            })

        return {
            "total_closed_trades": len(rows),
            "breakdown": sorted(breakdown, key=lambda x: (x["tier"], x["setup_type"]))
        }


# ── Stage 6: Scanner Coordinator ──────────────────────────────────────────────

class DailyOpportunitiesScanner:
    """Coordinates universe scanning, deduplication, and execution."""

    def __init__(self, db: Optional[DailyOpportunitiesDB] = None, config: Optional[Dict[str, Any]] = None):
        self.db = db or DailyOpportunitiesDB()
        self.config = config or load_daily_config()
        self.tier_classifier = TierClassifier(self.db, self.config)
        self.setup_detectors = SetupDetectors(self.db, self.config)
        self.circuit_gate = CircuitAchievabilityGate(self.config)
        self.lifecycle = LifecycleStateMachine(self.db, self.config)
        self.risk_mgr = PositionRiskManager(self.config)
        self.tracker = OutcomeTracker(self.db, self.config)
        self._last_scan_duration_ms: float = 0.0
        self._last_scan_time: Optional[str] = None

    def scan_universe(
        self,
        stocks: List[Dict[str, Any]],
        history_provider: Any,
        market_regime: str = "Neutral",
        circuit_anomalies: Optional[List[str]] = None,
        dt: Optional[datetime.datetime] = None
    ) -> Dict[str, Any]:
        """
        Runs full detector pass across the liquid PSX universe.
        Completes in < 2 seconds.
        """
        t0 = time.time()
        if dt is None:
            dt = psx_calendar.get_current_pkt_datetime()

        today_str = dt.strftime("%Y-%m-%d")
        now_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        schedule = get_session_schedule(dt)
        circuit_set = set(circuit_anomalies or [])

        # Check if already has max open triggered positions
        open_cands = self.db.get_open_candidates(today_str)
        existing_symbols = {c["symbol"] for c in open_cands}

        # Update existing open candidates first
        stocks_by_sym = {s.get("symbol", "").upper().strip(): s for s in stocks if s.get("symbol")}
        for c in open_cands:
            sym = c["symbol"]
            s_data = stocks_by_sym.get(sym)
            if s_data:
                try:
                    px = float(s_data.get("price", s_data.get("current", c.get("current_price", 0))))
                    self.lifecycle.update_open_candidate(c, px, schedule, dt)
                except Exception:
                    pass

        # Check caps
        risk_cfg = self.config.get("risk_and_sizing", {})
        max_triggered = int(risk_cfg.get("max_concurrent_triggered", 4))
        max_watching = int(risk_cfg.get("max_concurrent_watching", 10))

        active_triggered = sum(1 for c in open_cands if c.get("state") == "TRIGGERED")
        active_watching = sum(1 for c in open_cands if c.get("state") == "WATCHING")

        new_candidates_found = []

        # Sort universe by turnover or volume
        sorted_stocks = sorted(
            stocks,
            key=lambda s: float(s.get("turnover", 0) or (float(s.get("volume", 0) or 0) * float(s.get("price", 0) or 0))),
            reverse=True
        )

        # Universe scan limit (e.g. top 150 most liquid)
        scan_limit = int(self.config.get("scanning", {}).get("max_universe_size", 150))
        eval_universe = sorted_stocks[:scan_limit]

        # Calculate watch expiry time for today
        if schedule.get("is_friday"):
            exp_time_str = self.config.get("lifecycle", {}).get("watch_expiry_friday_s2", "15:30")
        else:
            exp_time_str = self.config.get("lifecycle", {}).get("watch_expiry_mon_thu", "13:30")
        watch_expires_at = f"{today_str} {exp_time_str}:00"

        for s in eval_universe:
            sym = s.get("symbol", "").upper().strip()
            if not sym or sym in existing_symbols:
                continue

            try:
                px = float(s.get("price", s.get("current", 0)) or 0)
                chg = float(s.get("change", 0) or 0)
                vol = float(s.get("volume", 0) or 0)
                open_px = float(s.get("open", px) or px)
                ldcp = float(s.get("ldcp", px) or px)
                low_px = float(s.get("low", px) or px)
                high_px = float(s.get("high", px) or px)
            except (ValueError, TypeError):
                continue

            if px <= 0.5:
                continue

            # Fetch daily history for ATR & liquidity gate
            hist = history_provider(sym) if callable(history_provider) else []
            liq = check_liquidity_gate(s, hist)
            if not liq["passes"]:
                continue

            # Calculate ATR14
            atr14 = px * 0.025  # Fallback
            if hist and len(hist) >= 15:
                highs = [float(b.get("high", b.get("close", 0))) for b in hist]
                lows = [float(b.get("low", b.get("close", 0))) for b in hist]
                closes = [float(b.get("close", 0)) for b in hist]
                atr_series = compute_atr_series(highs, lows, closes, 14)
                if atr_series and atr_series[-1] is not None:
                    atr14 = max(0.05, float(atr_series[-1]))

            # Classify Tier
            has_lock = sym in circuit_set
            tier_data = self.tier_classifier.classify_tier(
                sym, px, chg, vol, liq["avg_volume_21d"], atr14, has_lock, schedule
            )

            # Generate intraday indicators for setups
            pivots = compute_classical_pivots(high_px, low_px, ldcp)
            vwap = compute_vwap([px, (high_px + low_px + px) / 3.0], [vol * 0.5, vol * 0.5])

            # Intraday RSI proxy (using recent candles or daily fallback)
            rsi = 50.0
            if hist and len(hist) >= 15:
                c_series = [float(b.get("close", 0)) for b in hist]
                r_series = compute_rsi_series(c_series, 14)
                if r_series and r_series[-1] is not None:
                    rsi = float(r_series[-1])

            # Run Setup Detectors
            found_setups = []

            # 1. Circuit Runner
            s_cr = self.setup_detectors.detect_circuit_runner(sym, tier_data, px, chg, atr14)
            if s_cr:
                found_setups.append(s_cr)

            # 2. ORB
            s_orb = self.setup_detectors.detect_orb(sym, tier_data, px, chg, [{"high": high_px, "low": low_px, "price": px}], atr14, schedule)
            if s_orb:
                found_setups.append(s_orb)

            # 3. Gap-and-Go
            s_gap = self.setup_detectors.detect_gap_and_go(sym, tier_data, px, open_px, ldcp, atr14, schedule)
            if s_gap:
                found_setups.append(s_gap)

            # 4. VWAP Reclaim
            s_vwap = self.setup_detectors.detect_vwap_reclaim(sym, tier_data, px, vwap, low_px, atr14)
            if s_vwap:
                found_setups.append(s_vwap)

            # 5. Oversold Bounce
            s_ob = self.setup_detectors.detect_oversold_bounce(sym, tier_data, px, rsi, pivots, atr14)
            if s_ob:
                found_setups.append(s_ob)

            if not found_setups:
                continue

            # Stage 6.2: Deduplicate (keep highest tier / highest conviction setup)
            # Priority: Circuit Runner > ORB > Gap-and-Go > VWAP Reclaim > Oversold Bounce
            priority_map = {"CIRCUIT_RUNNER": 5, "ORB": 4, "GAP_AND_GO": 3, "VWAP_RECLAIM": 2, "OVERSOLD_BOUNCE": 1}
            best_setup = max(found_setups, key=lambda x: priority_map.get(x["setup_type"], 0))

            # Stage 3: Pass through Circuit Achievability Gate
            clamped_cand = self.circuit_gate.process_candidate(best_setup, px, chg)
            if not clamped_cand:
                continue

            # Stage 5: Position Sizing & Risk Management
            sizing = self.risk_mgr.calculate_sizing(
                clamped_cand["entry_price"], clamped_cand["stop_loss"], market_regime
            )
            clamped_cand.update(sizing)
            clamped_cand["date"] = today_str
            clamped_cand["detected_at"] = now_str
            clamped_cand["watch_expires_at"] = watch_expires_at
            clamped_cand["state"] = "WATCHING"
            clamped_cand["current_price"] = px

            # Respect max watching cap
            if active_watching >= max_watching:
                break

            # Save to SQLite
            cid = self.db.save_candidate(clamped_cand)
            clamped_cand["id"] = cid
            new_candidates_found.append(clamped_cand)
            existing_symbols.add(sym)
            active_watching += 1

        t1 = time.time()
        self._last_scan_duration_ms = round((t1 - t0) * 1000.0, 1)
        self._last_scan_time = now_str

        # Fetch refreshed list of today's candidates
        all_today = self.db.get_all_today(today_str)
        track_record = self.tracker.get_track_record()

        return {
            "success": True,
            "scan_time": now_str,
            "scan_duration_ms": self._last_scan_duration_ms,
            "market_schedule": schedule,
            "candidates": all_today,
            "track_record": track_record,
            "stats": {
                "total_today": len(all_today),
                "watching": sum(1 for c in all_today if c.get("state") == "WATCHING"),
                "triggered": sum(1 for c in all_today if c.get("state") == "TRIGGERED"),
                "closed": sum(1 for c in all_today if c.get("state") == "CLOSED"),
                "target_hit": sum(1 for c in all_today if c.get("exit_type") == "TARGET_HIT"),
                "stopped_out": sum(1 for c in all_today if c.get("exit_type") == "STOPPED_OUT"),
                "time_exit": sum(1 for c in all_today if c.get("exit_type") == "TIME_EXIT")
            }
        }


# Global Singleton
_SCANNER_INSTANCE: Optional[DailyOpportunitiesScanner] = None


def get_scanner() -> DailyOpportunitiesScanner:
    global _SCANNER_INSTANCE
    if _SCANNER_INSTANCE is None:
        _SCANNER_INSTANCE = DailyOpportunitiesScanner()
    return _SCANNER_INSTANCE
