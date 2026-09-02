#!/usr/bin/env python3
"""
PSX Telegram Alert Engine
==========================
Sends real-time trade alerts to your Telegram channel/chat.

Trigger sources:
  1. Weekly Scan  — Grade A / A+ candidates (entry zone, SL, TP1, TP2, RR, conviction%)
  2. Intelligence Engine — high-confidence anomaly signals (POSSIBLE_BREAKOUT / WATCH
                           with confidence >= INTEL_ALERT_MIN_CONFIDENCE)

Config: cache/telegram_config.json
  {
    "bot_token": "123456:ABC-...",
    "chat_id":   "-100123456789",
    "weekly_scan_min_grade": "A",         // "A_PLUS" | "A"
    "intel_min_confidence": 65,           // 0-100
    "intel_signals_to_alert": ["POSSIBLE_BREAKOUT", "WATCH"],
    "enabled": true
  }

Zero pip-dependencies — uses stdlib urllib only.
"""

import json
import time
import threading
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Dict, Any, List, Optional

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_PATH = Path("cache/telegram_config.json")

# Per-alert cooldown: don't re-alert same symbol+type within this many seconds
ALERT_COOLDOWN_SECONDS = 3600   # 1 hour

# Default thresholds (overridden by config)
DEFAULT_WEEKLY_MIN_GRADE      = "A"
DEFAULT_INTEL_MIN_CONFIDENCE  = 65
DEFAULT_INTEL_SIGNALS         = ["POSSIBLE_BREAKOUT", "WATCH"]

# ── Internal state ────────────────────────────────────────────────────────────

_alert_lock  = threading.Lock()
_sent_alerts: Dict[str, float] = {}   # dedup key → timestamp


# ── Config loader ─────────────────────────────────────────────────────────────

def load_config() -> Dict[str, Any]:
    """Read telegram_config.json. Returns {} if missing or invalid."""
    try:
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_config(cfg: Dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def is_enabled() -> bool:
    cfg = load_config()
    return bool(cfg.get("enabled") and cfg.get("bot_token") and cfg.get("chat_id"))


# ── HTTP send ─────────────────────────────────────────────────────────────────

def _send_message(text: str, parse_mode: str = "HTML") -> bool:
    """
    Send a message via Telegram Bot API.
    Returns True on success, False on failure.
    Uses stdlib urllib — zero dependencies.
    """
    cfg = load_config()
    bot_token = cfg.get("bot_token", "")
    chat_id   = str(cfg.get("chat_id", ""))

    if not bot_token or not chat_id:
        print("[Telegram] No bot_token or chat_id configured — alert skipped.")
        return False

    url  = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    body = json.dumps({
        "chat_id":    chat_id,
        "text":       text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True
    }).encode()

    req = urllib.request.Request(url, data=body,
                                  headers={"Content-Type": "application/json"},
                                  method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return True, None
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode(errors="ignore")[:300]
        print(f"[Telegram] HTTP {e.code}: {body_txt}")
        return False, f"HTTP {e.code}: {body_txt}"
    except Exception as ex:
        print(f"[Telegram] Send error: {ex}")
        return False, str(ex)
    return False, "Unknown error"


def _send_async(text: str) -> None:
    """Fire-and-forget in background thread so it never blocks the engine."""
    def _worker():
        _send_message(text)  # tuple return is intentionally discarded
    t = threading.Thread(target=_worker, daemon=True)
    t.start()


# ── Dedup / cooldown ──────────────────────────────────────────────────────────

def _is_cooldown(key: str) -> bool:
    """Returns True if this alert key was sent within ALERT_COOLDOWN_SECONDS."""
    with _alert_lock:
        last = _sent_alerts.get(key, 0)
        if time.time() - last < ALERT_COOLDOWN_SECONDS:
            return True
        _sent_alerts[key] = time.time()
        return False


# ── FORMATTER 1: Weekly Scan Grade A/A+ candidate ────────────────────────────

def alert_intraday_setup(candidate: Dict[str, Any], mode: str = "INSTANT") -> bool:
    """
    Send intraday trade alert via Telegram.
    mode: "INSTANT" | "MORNING_PICK" | "AFTERNOON_PICK"
    Returns True if alert dispatched.
    """
    if not is_enabled():
        return False

    sym    = candidate.get("symbol", "?")
    key    = f"intraday_{sym}"
    if _is_cooldown(key):
        return False

    name   = candidate.get("name", sym)
    sector = candidate.get("sector", "?")
    score  = candidate.get("score", 0)
    rvol   = candidate.get("rvol", 0)
    change = candidate.get("change", 0)
    price  = candidate.get("price", 0)
    lvl    = candidate.get("levels", {})
    at     = candidate.get("scanned_at", "")

    entry_min  = lvl.get("entry_min", price)
    entry_max  = lvl.get("entry_max", price)
    stop       = lvl.get("stop", 0)
    target     = lvl.get("target", 0)
    risk_pct   = lvl.get("risk_pct", 0)
    reward_pct = lvl.get("reward_pct", 0)
    rr         = lvl.get("rr", 0)

    mode_badge = {
        "INSTANT":       "⚡ INSTANT — High Conviction Setup",
        "MORNING_PICK":  "🌅 MORNING PICK — Best Setup (10:30 AM)",
        "AFTERNOON_PICK":"🌆 AFTERNOON PICK — Best Setup (1:00 PM)",
    }.get(mode, "⚡ INTRADAY ALERT")

    # Catalyst text
    catalysts = []
    if rvol >= 3.0:
        catalysts.append(f"Volume {rvol}x above average 🔥")
    elif rvol >= 2.0:
        catalysts.append(f"Volume {rvol}x above average")
    if change >= 3.0:
        catalysts.append(f"+{change}% strong momentum")
    elif change >= 1.5:
        catalysts.append(f"+{change}% positive momentum")
    if not catalysts:
        catalysts.append(f"Score {score}/100 — multi-factor setup")

    catalyst_str = "\n".join(f"  • {c}" for c in catalysts[:3])

    text = (
        f"⚡ <b>PSX INTRADAY SETUP</b>\n"
        f"<i>{mode_badge}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Symbol:</b>  {sym} 📈 LONG\n"
        f"<b>Sector:</b>  {sector}\n"
        f"<b>Score:</b>   {score}/100  |  <b>RVol:</b> {rvol}x  |  <b>Move:</b> +{change}%\n\n"
        f"📍 <b>TRADE LEVELS</b>\n"
        f"  • <b>Entry:</b>   ₨{entry_min:.2f} – ₨{entry_max:.2f}\n"
        f"  • <b>Stop:</b>    ₨{stop:.2f} (-{risk_pct}%) [Session low basis] 🛡\n"
        f"  • <b>Target:</b>  ₨{target:.2f} (+{reward_pct}%) 🎯\n"
        f"  • <b>R:R:</b>     {rr}x\n\n"
        f"⚡ <b>WHY NOW?</b>\n"
        f"{catalyst_str}\n\n"
        f"⏱ <i>Intraday only — close by 3:00 PM PKT · Scanned at {at}</i>\n"
        f"<i>PSX Alert · psx.up.railway.app</i>"
    )

    _send_async(text)
    print(f"[Telegram] Intraday alert dispatched → {sym} ({mode}, score {score})")
    return True


def alert_intraday_close(symbol: str, entry: float, live_price: float,
                          target: float, stop: float,
                          hit_target: bool, mode: str = "INTRADAY") -> bool:
    """
    Send "Close Trade Now" alert when intraday target or stop is reached.
    Returns True if alert dispatched.
    """
    if not is_enabled():
        return False

    key = f"intraday_close_{symbol}"
    if _is_cooldown(key):
        return False

    pnl_pct  = round((live_price - entry) / entry * 100, 2)
    pnl_sign = "+" if pnl_pct >= 0 else ""

    if hit_target:
        emoji      = "✅"
        result     = "TARGET HIT"
        action_msg = "Book profits now — target reached! 🎯"
        pnl_color  = f"+{abs(pnl_pct)}%"
    else:
        emoji      = "🛑"
        result     = "STOP LOSS HIT"
        action_msg = "Exit now — stop loss triggered. Cut the loss. 🛡"
        pnl_color  = f"-{abs(pnl_pct)}%"

    text = (
        f"{emoji} <b>PSX INTRADAY — {result}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Symbol:</b>      {symbol}\n"
        f"<b>Entry:</b>       ₨{entry:.2f}\n"
        f"<b>Live Price:</b>  ₨{live_price:.2f}\n"
        f"<b>P&amp;L:</b>          {pnl_sign}{pnl_pct}%\n\n"
        f"<b>Target was:</b>  ₨{target:.2f}\n"
        f"<b>Stop was:</b>    ₨{stop:.2f}\n\n"
        f"🔔 <b>{action_msg}</b>\n\n"
        f"<i>PSX Intraday Monitor · psx.up.railway.app</i>"
    )

    _send_async(text)
    print(f"[Telegram] Close alert dispatched → {symbol} ({'TARGET' if hit_target else 'STOP'}) @ ₨{live_price:.2f}")
    return True


def alert_weekly_scan_candidate(candidate: Dict[str, Any]) -> bool:


    """
    Call this for every Grade A / A+ candidate from execute_weekly_scan().
    Returns True if an alert was dispatched.
    """
    if not is_enabled():
        return False

    cfg   = load_config()
    min_g = cfg.get("weekly_scan_min_grade", DEFAULT_WEEKLY_MIN_GRADE)

    grade_rank = {"A_PLUS": 3, "A": 2, "B": 1}
    cand_rank  = grade_rank.get(candidate.get("grade", "B"), 1)
    min_rank   = grade_rank.get(min_g, 2)

    if cand_rank < min_rank:
        return False

    sym    = candidate.get("symbol", "?")
    key    = f"weekly_{sym}_{candidate.get('grade')}"
    if _is_cooldown(key):
        return False

    grade   = candidate.get("grade", "?")
    sector  = candidate.get("sector", "?")
    dirn    = candidate.get("direction", "LONG")
    conv    = candidate.get("convictionPct", 0)
    score   = candidate.get("score", {})
    risk    = candidate.get("risk", {})
    ezone   = candidate.get("entryZone", {})

    entry_min = ezone.get("min", 0)
    entry_max = ezone.get("max", 0)
    stop      = risk.get("stopLoss", 0)
    tp1       = risk.get("takeProfit1", 0)
    tp2       = risk.get("takeProfit2", 0)
    rr        = risk.get("rewardRiskRatio", 0)
    risk_pct  = risk.get("riskPct", 0)
    rwd_tp1   = risk.get("rewardPctTp1", 0)
    rwd_tp2   = risk.get("rewardPctTp2", 0)
    basis     = risk.get("stopBasis", "").replace("_", " ")

    triggers  = candidate.get("triggers", [])
    trig_str  = ", ".join(t.get("type", "").replace("_", " ") for t in triggers[:3])

    grade_emoji = "🔥" if grade == "A_PLUS" else "✅"
    dir_emoji   = "📈" if dirn == "LONG" else "📉"

    text = (
        f"{grade_emoji} <b>PSX WEEKLY SCAN — {grade.replace('_', '+')} SETUP</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Symbol:</b> {sym}  {dir_emoji} {dirn}\n"
        f"<b>Sector:</b> {sector}\n"
        f"<b>Conviction:</b> {conv}%  |  <b>Raw Score:</b> {score.get('rawScore', 0)}/6\n\n"
        f"🎯 <b>TRADE LEVELS</b>\n"
        f"  • <b>Entry Zone:</b> ₨{entry_min:.2f} – ₨{entry_max:.2f}\n"
        f"  • <b>Stop Loss:</b>  ₨{stop:.2f} (-{risk_pct}%)  [{basis}] 🛡\n"
        f"  • <b>TP1:</b>        ₨{tp1:.2f} (+{rwd_tp1}%) 🎯\n"
        f"  • <b>TP2:</b>        ₨{tp2:.2f} (+{rwd_tp2}%) 🚀\n"
        f"  • <b>R:R Ratio:</b>  {rr:.1f}x\n\n"
        f"⚡ <b>TRIGGERS:</b> {trig_str or 'N/A'}\n"
        f"<i>PSX Weekly Scan · psx.up.railway.app</i>"
    )

    _send_async(text)
    print(f"[Telegram] Weekly scan alert dispatched → {sym} ({grade})")
    return True


# ── FORMATTER 2: Intelligence Engine high-confidence signal ───────────────────

def alert_intelligence_signal(pred: Dict[str, Any],
                               event: Dict[str, Any],
                               causes: List[Dict[str, Any]]) -> bool:
    """
    Call this right after generate_prediction() when confidence is high enough.
    Returns True if an alert was dispatched.
    """
    if not is_enabled():
        return False

    cfg          = load_config()
    min_conf     = int(cfg.get("intel_min_confidence", DEFAULT_INTEL_MIN_CONFIDENCE))
    alert_sigs   = cfg.get("intel_signals_to_alert", DEFAULT_INTEL_SIGNALS)

    confidence = pred.get("confidence", 0)
    signal     = pred.get("signal", "")

    if confidence < min_conf:
        return False
    if signal not in alert_sigs:
        return False

    sym    = pred.get("symbol", "?")
    key    = f"intel_{sym}_{signal}"
    if _is_cooldown(key):
        return False

    sector   = event.get("sector", "?")
    ev_type  = event.get("event_type", "?").replace("_", " ")
    price    = pred.get("price_at_signal", 0)
    pattern  = pred.get("pattern_name", "No Pattern")
    wr       = pred.get("historical_win_rate", 0)
    n        = pred.get("historical_sample", 0)

    # Top causes
    top_causes = causes[:3]
    causes_str = "\n".join(
        f"  • {c.get('factor','?').replace('_',' ').title()} [{c.get('confidence',0)}%] — {c.get('evidence','')[:60]}"
        for c in top_causes
    )

    # Calibration note
    reasoning = {}
    try:
        reasoning = json.loads(pred.get("reasoning_json", "{}"))
    except Exception:
        pass
    cal_note = reasoning.get("calibration_note", "")

    sig_emoji = {
        "POSSIBLE_BREAKOUT": "🚨",
        "WATCH":             "👁",
        "REVERSAL_RISK":     "⚠️",
        "EXTENDED_AVOID":    "🚫",
    }.get(signal, "📊")

    text = (
        f"{sig_emoji} <b>PSX AI ENGINE — {signal.replace('_',' ')}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Symbol:</b> {sym}  |  <b>Signal Price:</b> ₨{price:.2f}\n"
        f"<b>Sector:</b> {sector}  |  <b>Event:</b> {ev_type}\n"
        f"<b>AI Confidence:</b> {confidence}%\n\n"
        f"🧠 <b>PATTERN MATCHED:</b> {pattern}\n"
        f"<b>Historical Win Rate:</b> {wr:.1f}% (n={n})\n\n"
        f"🔍 <b>WHY IS IT MOVING?</b>\n"
        f"{causes_str or '  • Investigating...'}\n"
    )

    if cal_note:
        text += f"\n⚙️ <i>Calibration: {cal_note}</i>\n"

    text += f"\n<i>PSX Intelligence Engine · psx.up.railway.app</i>"

    _send_async(text)
    print(f"[Telegram] Intelligence alert dispatched → {sym} ({signal} {confidence}%)")
    return True


# ── FORMATTER 3: Pattern Library hit (Pattern P001 / P003 etc.) ──────────────

def alert_pattern_match(symbol: str, pattern_name: str, pattern_id: str,
                         confidence: int, price: float,
                         event_type: str, sector: str) -> bool:
    """
    Call when a high-value pattern (P001 Breakout+Volume, P003 Upper Lock+Accum)
    is matched for the first time on this symbol today.
    """
    if not is_enabled():
        return False

    # Only alert on the most actionable patterns
    ALERT_PATTERNS = {"P001", "P003", "P006"}
    if pattern_id not in ALERT_PATTERNS:
        return False

    key = f"pattern_{symbol}_{pattern_id}"
    if _is_cooldown(key):
        return False

    text = (
        f"🔔 <b>PSX PATTERN DETECTED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Symbol:</b>  {symbol}\n"
        f"<b>Sector:</b>  {sector}\n"
        f"<b>Pattern:</b> [{pattern_id}] {pattern_name}\n"
        f"<b>Event:</b>   {event_type.replace('_', ' ')}\n"
        f"<b>Price:</b>   ₨{price:.2f}\n"
        f"<b>Confidence:</b> {confidence}%\n\n"
        f"<i>Check PSX Intelligence Engine for full cause analysis.</i>\n"
        f"<i>psx.up.railway.app</i>"
    )

    _send_async(text)
    print(f"[Telegram] Pattern alert dispatched → {symbol} ({pattern_id})")
    return True


# ── FORMATTER 4: Daily scan summary ──────────────────────────────────────────

def alert_daily_scan_summary(run_meta: Dict[str, Any],
                              top_candidates: List[Dict[str, Any]]) -> bool:
    """
    Send a morning summary of the daily long-term scan top picks.
    Call after run_scan() completes each morning.
    """
    if not is_enabled():
        return False

    key = f"daily_scan_{run_meta.get('run_id', '')}"
    if _is_cooldown(key):
        return False

    a_plus = run_meta.get("a_plus_count", 0)
    a      = run_meta.get("a_count", 0)
    total  = run_meta.get("shortlist_count", 0)
    avg_sc = run_meta.get("avg_score", 0)

    lines = [
        f"📈 <b>PSX LONG-TERM SCAN COMPLETE</b>",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"<b>Universe:</b> {run_meta.get('eligible_count', 0)} stocks scanned",
        f"<b>Shortlist:</b> {total} stocks  |  A+: {a_plus}  |  A: {a}",
        f"<b>Avg Score:</b> {avg_sc:.1f}/100",
        f"",
        f"🏆 <b>TOP PICKS TODAY:</b>",
    ]

    for c in top_candidates[:5]:
        grade  = c.get("grade", "?").replace("_PLUS", "+").replace("_MINUS", "-")
        sym    = c.get("symbol", "?")
        sc     = c.get("totalScore", c.get("total_score", 0))
        sector = c.get("sector", "?")
        div    = c.get("divYield", c.get("div_yield", 0))
        lines.append(f"  • <b>{sym}</b> ({grade}) — {sector} | Score {sc:.0f}/100 | Div {div:.1f}%")

    lines.append(f"\n<i>PSX Long-Term Engine · psx.up.railway.app</i>")
    text = "\n".join(lines)

    _send_async(text)
    print(f"[Telegram] Daily scan summary dispatched ({total} shortlist)")
    return True


# ── Legacy formatters (kept for backward compat) ──────────────────────────────

def format_telegram_trade_alert(card: Dict[str, Any]) -> str:
    """Format high-impact markdown alert for Telegram bot."""
    sym       = card.get("symbol", "UNKNOWN")
    name      = card.get("name", sym)
    sector    = card.get("sector", "Other")
    conviction= card.get("conviction", "HIGH CONVICTION")
    strategy  = card.get("strategy", "Momentum Breakout")
    score     = card.get("score", 0)
    brackets  = card.get("brackets", {})
    reasons   = card.get("key_reasons", [])
    reasons_str = "\n".join([f"  • {r}" for r in reasons[:4]])

    return (
        f"🚨 <b>NEW PSX TRADE PROPOSAL</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Symbol:</b> {sym} ({name})\n"
        f"<b>Sector:</b> {sector}\n"
        f"<b>Strategy:</b> {strategy}\n"
        f"<b>AI Conviction Score:</b> {score}/100 ({conviction})\n\n"
        f"🎯 <b>TRADE BRACKETS:</b>\n"
        f"  • <b>Entry Zone:</b> PKR {brackets.get('entry_range')}\n"
        f"  • <b>Stop Loss:</b> PKR {brackets.get('stop_loss')} (-{brackets.get('risk_pct')}%) 🛡️\n"
        f"  • <b>Target 1:</b> PKR {brackets.get('take_profit_1')} (+{brackets.get('reward_pct_tp1')}%) 🎯\n"
        f"  • <b>Target 2:</b> PKR {brackets.get('take_profit_2')} 🚀\n"
        f"  • <b>Risk:Reward:</b> {brackets.get('rr_ratio')}\n\n"
        f"📊 <b>SETUP CATALYSTS:</b>\n"
        f"{reasons_str}\n\n"
        f"⚠️ <i>Requires your approval to execute in Paper/Live broker.</i>"
    )


def format_telegram_execution_report(action: str, symbol: str, details: Dict[str, Any]) -> str:
    """Format trade execution notification."""
    return (
        f"✅ <b>PSX TRADE EXECUTED: {action}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Symbol:</b> {symbol}\n"
        f"<b>Shares:</b> {details.get('shares', 0):,}\n"
        f"<b>Fill Price:</b> PKR {details.get('fill_price', 0):.2f}\n"
        f"<b>Total Value:</b> PKR {details.get('total_cost', details.get('net_proceeds', 0)):,.2f}\n"
        f"<b>Time:</b> {details.get('timestamp', details.get('exit_time', ''))}\n"
    )
