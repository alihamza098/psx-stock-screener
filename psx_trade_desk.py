#!/usr/bin/env python3
"""
PSX Trade Desk (Phase 2)
========================
Turns validated strategies into orders you can place on KTrade by hand, and keeps an honest
forward-test record of every strategy — validated or not.

Swing / long-term (end of day)
  * After the close, every configured Strategy Lab strategy is replayed from its go-live date
    to today with the same engine as the backtests (psx_backtester.simulate). That replay *is*
    the forward test: no hand-maintained state can drift from the backtest rules.
  * What is queued for tomorrow's open becomes the order plan (sized for the configured capital).
  * Status: LIVE only if the strategy's latest Strategy Lab verdict is PASS, else PAPER.
    Only LIVE strategies are alerted on Telegram.

Day trading (during the session, from recorded real ticks)
  * ORB  — opening-range breakout (long; short only for short-eligible symbols)
  * VWAP reclaim — price recovers above session VWAP after trading below it
  * Risk guard: max trades/day, max open positions, daily loss lock, no entries after the cut-off,
    forced time exit before the close, circuit-room check, no chasing.
  * Every signal is paper-traded and recorded. A strategy is promoted to LIVE only after
    `min_forward_trades` closed trades with positive net expectancy and profit factor above the bar.
"""

import datetime
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import psx_backtester as bt
import psx_market_data as md
import shared_trading_utils
from psx_costs import CostModel, get_cost_model

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config" / "trade_desk.json"
DATA_DIR = Path(os.environ.get("PSX_DATA_DIR") or (BASE_DIR / "cache"))
DB_PATH = DATA_DIR / "trade_desk.db"
SWING_STATE_PATH = DATA_DIR / "trade_desk_swing.json"

PKT = md.PKT
LIVE, PAPER, DISABLED = "LIVE", "PAPER", "DISABLED"

_DEFAULTS: Dict[str, Any] = {
    "capital_pkr": 500000,
    "alerts": {"telegram": True, "force_live": [], "disabled": []},
    "swing": {"strategies": ["breakout_20d", "pullback_rsi2", "momentum_12_1_monthly", "live_engine_v2"],
              "risk_per_trade_pct": 1.0, "max_position_pct": 20.0, "max_positions": 5,
              "limit_buffer_pct": 1.0, "universe_size": 100},
    "intraday": {"enabled": True, "strategies": ["orb", "vwap_reclaim"], "universe_size": 30,
                 "risk_per_trade_pct": 0.5, "max_position_pct": 25.0, "max_open_positions": 2,
                 "max_trades_per_day": 3, "max_daily_loss_pct": 1.5, "opening_range_minutes": 15,
                 "min_opening_range_ticks": 3, "max_stop_pct": 2.5, "max_chase_pct": 1.0,
                 "min_reward_risk": 1.5, "target_r_multiple": 2.0, "min_rvol": 1.5,
                 "require_volume": True, "no_new_entries_after": "14:30",
                 "friday_no_new_entries_after": "15:45", "min_trade_value_pkr": 10000,
                 "promotion": {"min_forward_trades": 40, "min_profit_factor": 1.1}},
}


def load_config() -> Dict[str, Any]:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            user = json.load(f)
    except Exception:
        user = {}
    cfg = json.loads(json.dumps(_DEFAULTS))
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


def _now_pkt(now: Optional[datetime.datetime] = None) -> datetime.datetime:
    if now is None:
        return datetime.datetime.now(PKT)
    return now if now.tzinfo else now.replace(tzinfo=PKT)


def _hhmm_to_mins(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _fmt_pkr(v: float) -> str:
    return f"{v:,.0f}"


def _default_sender(text: str) -> bool:
    try:
        import psx_telegram_bot as tg
        if not tg.is_enabled():
            return False
        ok, _ = tg._send_message(text)
        return bool(ok)
    except Exception as e:
        print(f"[TradeDesk] telegram error: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────────────────────────────────────

class DeskDB:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS intraday_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL, strategy TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
                status_at_signal TEXT NOT NULL,
                entry_time TEXT NOT NULL, entry REAL NOT NULL, stop REAL NOT NULL, target REAL,
                qty INTEGER NOT NULL, entry_fee REAL NOT NULL, reason TEXT,
                state TEXT NOT NULL DEFAULT 'OPEN',
                exit_time TEXT, exit REAL, exit_reason TEXT, net_pnl REAL, ret_pct REAL)""")
            c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self.path), timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._conn() as c:
            r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))

    def open_trade(self, t: Dict[str, Any]) -> int:
        cols = ["date", "strategy", "symbol", "side", "status_at_signal", "entry_time", "entry", "stop",
                "target", "qty", "entry_fee", "reason"]
        with self._lock, self._conn() as c:
            cur = c.execute(f"INSERT INTO intraday_trades ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                            [t.get(k) for k in cols])
            return int(cur.lastrowid)

    def close_trade(self, tid: int, exit_time: str, exit_px: float, reason: str, net: float, ret_pct: float) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE intraday_trades SET state='CLOSED', exit_time=?, exit=?, exit_reason=?, net_pnl=?, ret_pct=? "
                      "WHERE id=?", (exit_time, exit_px, reason, net, ret_pct, tid))

    def recent_closed(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM intraday_trades WHERE state='CLOSED' ORDER BY id DESC LIMIT ?", (int(limit),))]

    def trades(self, where: str = "1=1", args: Tuple = ()) -> List[Dict[str, Any]]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(f"SELECT * FROM intraday_trades WHERE {where} ORDER BY id", args)]


# ─────────────────────────────────────────────────────────────────────────────
# Status / promotion rules
# ─────────────────────────────────────────────────────────────────────────────

def forward_stats(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    closed = [t for t in trades if t.get("state", "CLOSED") == "CLOSED" and t.get("net_pnl") is not None]
    n = len(closed)
    wins = [t["net_pnl"] for t in closed if t["net_pnl"] > 0]
    losses = [-t["net_pnl"] for t in closed if t["net_pnl"] <= 0]
    net = sum(t["net_pnl"] for t in closed)
    pf = (sum(wins) / sum(losses)) if sum(losses) > 0 else (999.0 if wins else 0.0)
    return {"trades": n, "win_rate_pct": round(len(wins) / n * 100, 1) if n else 0.0,
            "net_pnl_pkr": round(net, 2), "expectancy_pkr": round(net / n, 2) if n else 0.0,
            "profit_factor": round(pf, 2), "win_rate_ci95": bt.wilson_ci(len(wins), n)}


def swing_status(name: str, cfg: Dict[str, Any], report: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    alerts = cfg["alerts"]
    if name in alerts.get("disabled", []):
        return DISABLED, "disabled in config/trade_desk.json"
    if name in alerts.get("force_live", []):
        return LIVE, "forced live in config/trade_desk.json (not validated)"
    verdict = next((s for s in (report or {}).get("strategies", []) if s.get("name") == name), None)
    if not verdict:
        return PAPER, "no Strategy Lab verdict yet"
    if verdict.get("verdict") == "PASS":
        return LIVE, "Strategy Lab: PASS"
    return PAPER, f"Strategy Lab: {verdict.get('verdict')} — {verdict.get('reason', '')}"


def intraday_status(name: str, cfg: Dict[str, Any], stats: Dict[str, Any]) -> Tuple[str, str]:
    alerts, promo = cfg["alerts"], cfg["intraday"]["promotion"]
    if name in alerts.get("disabled", []):
        return DISABLED, "disabled in config/trade_desk.json"
    if name in alerts.get("force_live", []):
        return LIVE, "forced live in config/trade_desk.json (not validated)"
    need = int(promo["min_forward_trades"])
    if stats["trades"] < need:
        return PAPER, f"forward test: {stats['trades']}/{need} trades"
    if stats["expectancy_pkr"] <= 0:
        return PAPER, "forward test: negative expectancy after costs"
    if stats["profit_factor"] < float(promo["min_profit_factor"]):
        return PAPER, f"forward test: profit factor {stats['profit_factor']} < {promo['min_profit_factor']}"
    return LIVE, f"forward test passed ({stats['trades']} trades)"


# ─────────────────────────────────────────────────────────────────────────────
# Swing / long-term desk (end of day)
# ─────────────────────────────────────────────────────────────────────────────

def run_swing_eod(fetch: Optional[Callable[..., str]] = None, stocks: Optional[List[Dict[str, Any]]] = None,
                  data: Optional[Dict[str, List[Dict[str, Any]]]] = None, db: Optional[DeskDB] = None,
                  costs: Optional[CostModel] = None, send: Optional[Callable[[str], bool]] = None,
                  state_path: Path = SWING_STATE_PATH) -> Dict[str, Any]:
    cfg = load_config()
    sw = cfg["swing"]
    db = db or DeskDB()
    costs = costs or get_cost_model()
    send = send or _default_sender
    if data is None:
        universe = bt.default_universe(stocks or [], int(sw["universe_size"]))
        data = bt.load_universe(universe, fetch, pause_s=0.2)
    data = {s: b for s, b in data.items() if b}
    if not data:
        return {"success": False, "error": "no price history available"}
    as_of = max(b[-1]["date"] for b in data.values())
    report = bt.load_report()
    settings = {"capital_pkr": float(cfg["capital_pkr"]), "risk_per_trade_pct": sw["risk_per_trade_pct"],
                "max_position_pct": sw["max_position_pct"], "max_positions": sw["max_positions"]}

    state = {"success": True, "as_of": as_of, "generated_at": datetime.datetime.now(PKT).isoformat(),
             "capital_pkr": cfg["capital_pkr"], "strategies": []}
    messages = []
    for name in sw["strategies"]:
        if name not in bt.STRATEGIES:
            continue
        status, why = swing_status(name, cfg, report)
        go_live = db.get_meta(f"swing_go_live:{name}")
        if not go_live:
            go_live = as_of
            db.set_meta(f"swing_go_live:{name}", go_live)
        entry = {"name": name, "status": status, "status_reason": why, "go_live": go_live}
        try:
            proto = bt.STRATEGIES[name]()
            entry["description"], entry["horizon"] = proto.description, proto.horizon
            res = bt.simulate(proto, data, start=go_live, settings=settings, costs=costs, close_at_end=False)
        except Exception as e:
            entry.update(error=str(e))
            state["strategies"].append(entry)
            continue
        buf = float(sw["limit_buffer_pct"]) / 100.0
        plan = []
        for p in res["planned_entries"]:
            limit = p["ref_close"] * (1 + buf) if p["side"] == "long" else p["ref_close"] * (1 - buf)
            risk = abs(p["ref_close"] - p["stop"]) * p["est_qty"]
            plan.append({**p, "limit": round(limit, 2), "est_value_pkr": round(p["est_qty"] * p["ref_close"], 0),
                         "est_risk_pkr": round(risk, 0)})
        exits = [{"symbol": p["symbol"], "qty": p["qty"], "reason": p["pending_exit"]}
                 for p in res["open_positions"] if p.get("pending_exit")]
        entry.update({
            "forward": res["metrics"], "equity": res["equity"], "cash": res["cash"], "unsettled": res["unsettled"],
            "open_positions": [{"symbol": p["symbol"], "side": p["side"], "qty": p["qty"],
                                "entry": round(p["entry"], 2), "entry_date": p["entry_date"],
                                "stop": round(p["stop"], 2), "target": round(p["target"], 2) if p.get("target") else None,
                                "last": round(p["last_px"], 2),
                                "unrealised_pkr": round((p["last_px"] - p["entry"]) * p["qty"] * (1 if p["side"] == "long" else -1), 0),
                                "pending_exit": p.get("pending_exit")} for p in res["open_positions"]],
            "plan_entries": plan, "plan_exits": exits,
            "recent_trades": res["trades"][-10:],
        })
        state["strategies"].append(entry)

        alert_key = f"swing_alerted:{name}"
        if status == LIVE and cfg["alerts"].get("telegram") and (plan or exits) and db.get_meta(alert_key) != as_of:
            messages.append(format_swing_plan(name, as_of, plan, exits))
            db.set_meta(alert_key, as_of)

    sent = [m for m in messages if send(m)]
    state["alerts_sent"] = len(sent)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, default=str))
    tmp.replace(state_path)
    return state


def format_swing_plan(name: str, as_of: str, plan: List[Dict[str, Any]], exits: List[Dict[str, Any]]) -> str:
    lines = [f"📋 <b>Order plan for next session</b> — {name}", f"<i>Based on close of {as_of}</i>", ""]
    for x in exits:
        lines.append(f"🔴 SELL {x['qty']:,} <b>{x['symbol']}</b> at the open ({x['reason']})")
    for p in plan:
        verb = "BUY" if p["side"] == "long" else "SELL SHORT"
        cond = "≤" if p["side"] == "long" else "≥"
        tgt = f" · target {p['target']:,.2f}" if p.get("target") else ""
        lines.append(f"🟢 {verb} ~{p['est_qty']:,} <b>{p['symbol']}</b> at the open, limit {cond} {p['limit']:,.2f}"
                     f" · stop {p['stop']:,.2f}{tgt} · risk ≈ PKR {_fmt_pkr(p['est_risk_pkr'])}")
    lines += ["", "Skip any entry that opens beyond the limit. Educational signal, not investment advice."]
    return "\n".join(lines)


def load_swing_state(path: Path = SWING_STATE_PATH) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Intraday desk (during the session)
# ─────────────────────────────────────────────────────────────────────────────

def _session(now: datetime.datetime) -> Dict[str, Any]:
    return shared_trading_utils.get_session_schedule(now)


def _ldcp(stock: Dict[str, Any]) -> Optional[float]:
    if stock.get("ldcp"):
        return float(stock["ldcp"])
    px, chg = stock.get("price"), stock.get("change")  # screener "change" is percent
    try:
        return float(px) / (1 + float(chg) / 100.0) if px and chg is not None else None
    except (TypeError, ZeroDivisionError, ValueError):
        return None


def intraday_universe(stocks: List[Dict[str, Any]], n: int) -> List[Dict[str, Any]]:
    ok = [s for s in stocks if s.get("symbol") and not s.get("isNC") and float(s.get("price") or 0) >= 5]
    ok.sort(key=lambda s: -(float(s.get("price") or 0) * float(s.get("avgVolume30d") or s.get("volume") or 0)))
    return ok[:n]


def detect_orb(sym: str, price: float, ticks: List[Dict[str, Any]], open_ts: int, icfg: Dict[str, Any],
               band: Tuple[float, float], can_short: bool) -> Optional[Dict[str, Any]]:
    orb_end = open_ts + int(icfg["opening_range_minutes"]) * 60
    window = [t["price"] for t in ticks if open_ts <= t["ts"] < orb_end]
    if len(window) < int(icfg["min_opening_range_ticks"]):
        return None
    hi, lo = max(window), min(window)
    if hi <= lo:
        return None
    chase, max_stop = float(icfg["max_chase_pct"]) / 100, float(icfg["max_stop_pct"]) / 100
    r_mult, min_rr = float(icfg["target_r_multiple"]), float(icfg["min_reward_risk"])
    lower, upper = band
    if hi < price <= hi * (1 + chase):
        risk = price - lo
        if risk <= 0 or risk / price > max_stop:
            return None
        target = min(price + r_mult * risk, upper * 0.995)
        if (target - price) / risk < min_rr:
            return None
        return {"strategy": "orb", "symbol": sym, "side": "long", "entry": price, "stop": lo, "target": target,
                "reason": f"broke opening-range high {hi:,.2f} (range {lo:,.2f}–{hi:,.2f})"}
    if can_short and lo * (1 - chase) <= price < lo:
        risk = hi - price
        if risk <= 0 or risk / price > max_stop:
            return None
        target = max(price - r_mult * risk, lower * 1.005)
        if (price - target) / risk < min_rr:
            return None
        return {"strategy": "orb", "symbol": sym, "side": "short", "entry": price, "stop": hi, "target": target,
                "reason": f"broke opening-range low {lo:,.2f} (range {lo:,.2f}–{hi:,.2f})"}
    return None


def detect_vwap_reclaim(sym: str, price: float, bars: List[Dict[str, Any]], now_ts: int, icfg: Dict[str, Any],
                        band: Tuple[float, float]) -> Optional[Dict[str, Any]]:
    done = [b for b in bars if b["timestamp"] + 300 <= now_ts]
    if len(done) < 4:
        return None
    vol = sum(b["volume"] for b in done)
    if vol <= 0:
        return None
    vwap = sum((b["high"] + b["low"] + b["close"]) / 3 * b["volume"] for b in done) / vol
    prev3, last = done[-4:-1], done[-1]
    if not (all(b["close"] < vwap for b in prev3) and last["close"] > vwap and price > vwap):
        return None
    stop = min(b["low"] for b in done[-4:])
    risk = price - stop
    if risk <= 0 or risk / price > float(icfg["max_stop_pct"]) / 100:
        return None
    target = min(price + float(icfg["target_r_multiple"]) * risk, band[1] * 0.995)
    if (target - price) / risk < float(icfg["min_reward_risk"]):
        return None
    return {"strategy": "vwap_reclaim", "symbol": sym, "side": "long", "entry": price, "stop": stop, "target": target,
            "reason": f"reclaimed session VWAP {vwap:,.2f} after 3 bars below"}


class IntradayDesk:
    def __init__(self, db: Optional[DeskDB] = None, store: Optional[md.MarketDataStore] = None,
                 costs: Optional[CostModel] = None, send: Optional[Callable[[str], bool]] = None):
        self.db = db or DeskDB()
        self._store = store
        self.costs = costs or get_cost_model()
        self.send = send or _default_sender

    @property
    def store(self) -> md.MarketDataStore:
        return self._store or md.get_store()

    def strategy_statuses(self, cfg: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        out = {}
        for name in cfg["intraday"]["strategies"]:
            stats = forward_stats(self.db.trades("strategy=? AND state='CLOSED'", (name,)))
            status, why = intraday_status(name, cfg, stats)
            out[name] = {"status": status, "status_reason": why, "forward": stats}
        return out

    def risk_state(self, cfg: Dict[str, Any], today: str) -> Dict[str, Any]:
        icfg = cfg["intraday"]
        trades = self.db.trades("date=?", (today,))
        realised = sum(t["net_pnl"] or 0 for t in trades if t["state"] == "CLOSED")
        open_n = sum(1 for t in trades if t["state"] == "OPEN")
        loss_limit = -float(cfg["capital_pkr"]) * float(icfg["max_daily_loss_pct"]) / 100
        locked_reason = None
        if realised <= loss_limit:
            locked_reason = f"daily loss limit hit (PKR {_fmt_pkr(realised)} ≤ {_fmt_pkr(loss_limit)})"
        elif len(trades) >= int(icfg["max_trades_per_day"]):
            locked_reason = f"max {icfg['max_trades_per_day']} trades for today reached"
        return {"date": today, "trades_today": len(trades), "open_positions": open_n,
                "realised_pnl_pkr": round(realised, 2), "daily_loss_limit_pkr": round(loss_limit, 2),
                "locked": locked_reason is not None, "locked_reason": locked_reason}

    def tick(self, stocks: List[Dict[str, Any]], now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
        """Evaluate exits and new setups. Call about once a minute during the session."""
        cfg = load_config()
        icfg = cfg["intraday"]
        now = _now_pkt(now)
        today = now.strftime("%Y-%m-%d")
        now_ts = int(now.timestamp())
        sess = _session(now)
        mins = now.hour * 60 + now.minute
        by_sym = {(s.get("symbol") or "").upper(): s for s in stocks}
        events: List[Dict[str, Any]] = []

        # Close anything left open from an earlier day (server was down at the time exit)
        for t in self.db.trades("state='OPEN' AND date<?", (today,)):
            start, end = md._day_bounds(t["date"])
            seen = self.store.get_ticks(t["symbol"], start_ts=start, end_ts=end)
            self._close(t, seen[-1]["price"] if seen else t["entry"], "stale_session", now, events)

        statuses = self.strategy_statuses(cfg)

        # 1) manage open positions on the latest observed price
        for t in self.db.trades("state='OPEN' AND date=?", (today,)):
            s = by_sym.get(t["symbol"])
            if not s or not s.get("price"):
                continue
            px = float(s["price"])
            long_ = t["side"] == "long"
            if (px <= t["stop"]) if long_ else (px >= t["stop"]):
                self._close(t, px, "stop", now, events)
            elif t["target"] and ((px >= t["target"]) if long_ else (px <= t["target"])):
                self._close(t, t["target"], "target", now, events)  # resting limit order at the target
            elif mins >= int(sess.get("time_exit_mins", 915)):
                self._close(t, px, "time_exit", now, events)

        # 2) new setups
        risk = self.risk_state(cfg, today)
        cutoff = icfg["friday_no_new_entries_after"] if now.weekday() == 4 else icfg["no_new_entries_after"]
        open_mins = int(sess.get("session_open_mins", 572))
        open_ts = int(now.replace(hour=open_mins // 60, minute=open_mins % 60, second=0, microsecond=0).timestamp())
        can_enter = (icfg.get("enabled", True) and sess.get("is_in_trading_hours") and not risk["locked"]
                     and mins < _hhmm_to_mins(cutoff) and mins >= open_mins + int(icfg["opening_range_minutes"]))
        if can_enter:
            taken_today = {(t["strategy"], t["symbol"]) for t in self.db.trades("date=?", (today,))}
            open_syms = {t["symbol"] for t in self.db.trades("state='OPEN'")}
            frac = shared_trading_utils.get_time_of_day_volume_fraction(
                int(sess.get("elapsed_minutes", 0)), int(sess.get("total_session_minutes", 358)))
            candidates = []
            for s in intraday_universe(stocks, int(icfg["universe_size"])):
                sym = s["symbol"].upper()
                if sym in open_syms:
                    continue
                price, ref = float(s["price"]), _ldcp(s)
                if not ref:
                    continue
                today_vol, avg_vol = s.get("todayVolume"), s.get("avgVolume30d")
                rvol = (float(today_vol) / (float(avg_vol) * frac)) if today_vol and avg_vol and frac > 0 else None
                if rvol is None and icfg["require_volume"]:
                    continue
                if rvol is not None and rvol < float(icfg["min_rvol"]):
                    continue
                band = self.costs.circuit_band(ref)
                ticks = self.store.get_ticks(sym, start_ts=open_ts, end_ts=now_ts + 1)
                setups = []
                if "orb" in icfg["strategies"] and ("orb", sym) not in taken_today:
                    setups.append(detect_orb(sym, price, ticks, open_ts, icfg, band, self.costs.can_short(sym)))
                if "vwap_reclaim" in icfg["strategies"] and ("vwap_reclaim", sym) not in taken_today:
                    setups.append(detect_vwap_reclaim(sym, price, md.aggregate_ticks(ticks, 5), now_ts, icfg, band))
                for st in setups:
                    if st:
                        st["rvol"] = round(rvol, 2) if rvol is not None else None
                        candidates.append(st)
            candidates.sort(key=lambda c: -(c.get("rvol") or 0))
            slots = min(int(icfg["max_open_positions"]) - risk["open_positions"],
                        int(icfg["max_trades_per_day"]) - risk["trades_today"])
            for c in candidates:
                if slots <= 0:
                    break
                if statuses.get(c["strategy"], {}).get("status") == DISABLED:
                    continue
                if self._open(c, statuses.get(c["strategy"], {}).get("status", PAPER), cfg, now, events):
                    slots -= 1

        msgs = [e["message"] for e in events if e.get("alert")]
        if cfg["alerts"].get("telegram"):
            for m in msgs:
                self.send(m)
        return {"events": events, "risk": self.risk_state(cfg, today), "statuses": statuses}

    def _open(self, c: Dict[str, Any], status: str, cfg: Dict[str, Any], now: datetime.datetime,
              events: List[Dict[str, Any]]) -> bool:
        icfg = cfg["intraday"]
        side_in = "buy" if c["side"] == "long" else "sell"
        px = self.costs.fill_price(c["entry"], side_in)
        risk_ps = abs(px - c["stop"])
        if risk_ps <= 0:
            return False
        capital = float(cfg["capital_pkr"])
        qty = capital * float(icfg["risk_per_trade_pct"]) / 100 / risk_ps
        qty = min(qty, capital * float(icfg["max_position_pct"]) / 100 / px)
        qty = self.costs.round_lot(qty)
        if qty <= 0 or qty * px < float(icfg["min_trade_value_pkr"]):
            return False
        fee = self.costs.side_cost(px, qty)
        t = {"date": now.strftime("%Y-%m-%d"), "strategy": c["strategy"], "symbol": c["symbol"], "side": c["side"],
             "status_at_signal": status, "entry_time": now.strftime("%H:%M"), "entry": round(px, 4),
             "stop": round(c["stop"], 4), "target": round(c["target"], 4) if c.get("target") else None,
             "qty": qty, "entry_fee": round(fee, 2), "reason": c["reason"]}
        t["id"] = self.db.open_trade(t)
        verb = "BUY" if c["side"] == "long" else "SELL SHORT"
        sess = _session(now)
        exit_by = f"{int(sess.get('time_exit_mins', 915)) // 60:02d}:{int(sess.get('time_exit_mins', 915)) % 60:02d}"
        msg = (f"⚡ <b>{verb} {qty:,} {c['symbol']}</b> now ≈ {c['entry']:,.2f} ({c['strategy'].upper()})\n"
               f"Stop {c['stop']:,.2f} · Target {c['target']:,.2f} · Risk ≈ PKR {_fmt_pkr(risk_ps * qty)}\n"
               f"Why: {c['reason']} · RVOL {c.get('rvol') or '—'}\nExit by {exit_by} PKT if neither level hits.")
        events.append({"type": "entry", "trade": t, "status": status, "message": msg, "alert": status == LIVE})
        return True

    def _close(self, t: Dict[str, Any], raw_px: float, reason: str, now: datetime.datetime,
               events: List[Dict[str, Any]]) -> None:
        long_ = t["side"] == "long"
        px = raw_px if reason == "target" else self.costs.fill_price(raw_px, "sell" if long_ else "buy")
        same_day = t["date"] == now.strftime("%Y-%m-%d")
        fee = self.costs.side_cost(px, t["qty"], charge_commission=not (same_day and self.costs.day_one_side))
        gross = (px - t["entry"]) * t["qty"] * (1 if long_ else -1)
        net = gross - t["entry_fee"] - fee
        ret = net / (t["entry"] * t["qty"]) * 100
        self.db.close_trade(t["id"], now.strftime("%H:%M"), round(px, 4), reason, round(net, 2), round(ret, 3))
        icon = {"target": "🎯", "stop": "🛑", "time_exit": "⏰"}.get(reason, "•")
        verb = "SELL" if long_ else "BUY TO COVER"
        msg = (f"{icon} <b>{verb} {t['qty']:,} {t['symbol']}</b> — {reason.replace('_', ' ')} at ≈ {px:,.2f}\n"
               f"Net P&amp;L ≈ PKR {net:+,.0f} ({ret:+.2f}%) after costs")
        events.append({"type": "exit", "trade_id": t["id"], "symbol": t["symbol"], "reason": reason, "net_pnl": net,
                       "message": msg, "alert": t["status_at_signal"] == LIVE})


_intraday: Optional[IntradayDesk] = None


def get_intraday_desk() -> IntradayDesk:
    global _intraday
    if _intraday is None:
        _intraday = IntradayDesk()
    return _intraday


def desk_snapshot(now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    cfg = load_config()
    desk = get_intraday_desk()
    today = _now_pkt(now).strftime("%Y-%m-%d")
    return {
        "success": True,
        "capital_pkr": cfg["capital_pkr"],
        "round_trip_cost_pct": {"swing": round(desk.costs.round_trip_pct(100, 1000), 3),
                                "day_trade": round(desk.costs.round_trip_pct(100, 1000, day_trade=True), 3)},
        "swing": load_swing_state(),
        "intraday": {
            "enabled": cfg["intraday"].get("enabled", True),
            "rules": {k: cfg["intraday"][k] for k in ("risk_per_trade_pct", "max_trades_per_day", "max_daily_loss_pct",
                                                      "max_open_positions", "no_new_entries_after", "min_rvol")},
            "risk": desk.risk_state(cfg, today),
            "strategies": desk.strategy_statuses(cfg),
            "today": desk.db.trades("date=?", (today,)),
            "recent": desk.db.recent_closed(20),
        },
        "short_eligible_symbols": sorted(desk.costs.short_eligible),
    }
