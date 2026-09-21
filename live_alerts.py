"""
live_alerts.py - PSX Live Trading Alert Dispatcher & Deduplication Engine

Features:
1. Signal Cross Alerts: Notifies when a symbol crosses into BUY / STRONG BUY or SELL / STRONG SELL.
2. Target Hit Alerts: Notifies when current price reaches or exceeds suggested take-profit target.
3. Stop-Loss Proximity Alerts: Notifies when price approaches within 0.5% of stop-loss.
4. Duplicate Suppression: Tracks alerts by (symbol, alert_type, bucket) to ensure zero duplicate spam.
5. Telegram Gateway: Sends formatted notifications via Telegram Bot API if configured.
"""

import json
import os
import time
import urllib.request
from typing import Dict, List, Optional, Any, Set

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config", "live_trading.json")

# In-memory deduplication cache: set of (symbol, alert_type, timestamp_bucket)
_ALERT_HISTORY: Dict[str, float] = {}


def load_alerts_config() -> Dict[str, Any]:
    defaults = {
        "browser_notifications": True,
        "stop_approach_threshold_pct": 0.5,
        "dedup_window_minutes": 60,
        "telegram": {
            "enabled": False,
            "bot_token": "",
            "chat_id": "",
        },
    }
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                return cfg.get("alerts", defaults)
        except Exception:
            pass
    return defaults


def send_telegram_notification(message: str, cfg: Optional[Dict[str, Any]] = None) -> bool:
    """Sends a formatted message to a configured Telegram chat."""
    config = cfg or load_alerts_config()
    tg_cfg = config.get("telegram", {})
    if not tg_cfg.get("enabled"):
        return False

    bot_token = tg_cfg.get("bot_token", "").strip()
    chat_id = tg_cfg.get("chat_id", "").strip()
    if not bot_token or not chat_id:
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "PSX-Live-Trading-Bot"}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"[ALERTS] Error sending Telegram message: {e}")
        return False


def check_symbol_alerts(
    symbol: str,
    current_price: float,
    recommendation: str,
    target_price: float,
    stop_loss: float,
    previous_recommendation: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    Evaluates price and recommendation against alert conditions.
    Enforces deduplication window to prevent alert flooding.
    """
    cfg = config or load_alerts_config()
    dedup_window_sec = float(cfg.get("dedup_window_minutes", 60)) * 60.0
    stop_thresh_pct = float(cfg.get("stop_approach_threshold_pct", 0.5)) / 100.0
    now = time.time()

    alerts_triggered = []
    symbol = symbol.upper().strip()

    # 1. Signal Crossing Alert (Transition to BUY / SELL tier)
    if "BUY" in recommendation or "SELL" in recommendation:
        is_transition = (
            previous_recommendation is None
            or previous_recommendation != recommendation
        )
        alert_key = f"{symbol}:SIGNAL:{recommendation}"
        last_time = _ALERT_HISTORY.get(alert_key, 0)
        if (now - last_time) >= dedup_window_sec:
            _ALERT_HISTORY[alert_key] = now
            emoji = "🚀" if "BUY" in recommendation else "🔻"
            msg = f"{emoji} <b>PSX Live Alert: {symbol}</b> crossed into <b>{recommendation}</b> @ PKR {current_price:.2f} (Target: {target_price:.2f}, Stop: {stop_loss:.2f})"
            alerts_triggered.append({
                "type": "SIGNAL_CROSS",
                "symbol": symbol,
                "title": f"{symbol} {recommendation}",
                "message": msg,
                "price": current_price,
                "timestamp": now,
            })
            send_telegram_notification(msg, cfg)

    # 2. Target Hit Alert
    if target_price > 0:
        target_hit = False
        if "BUY" in recommendation and current_price >= target_price:
            target_hit = True
        elif "SELL" in recommendation and current_price <= target_price:
            target_hit = True

        if target_hit:
            alert_key = f"{symbol}:TARGET_HIT"
            last_time = _ALERT_HISTORY.get(alert_key, 0)
            if (now - last_time) >= dedup_window_sec:
                _ALERT_HISTORY[alert_key] = now
                msg = f"🎯 <b>Target Hit: {symbol}</b> reached target PKR {target_price:.2f} (Current: PKR {current_price:.2f})"
                alerts_triggered.append({
                    "type": "TARGET_HIT",
                    "symbol": symbol,
                    "title": f"Target Hit: {symbol}",
                    "message": msg,
                    "price": current_price,
                    "timestamp": now,
                })
                send_telegram_notification(msg, cfg)

    # 3. Approaching Stop-Loss Alert (within 0.5% of stop)
    if stop_loss > 0:
        approaching_stop = False
        if "BUY" in recommendation:
            # Stop is below price; approaching when price <= stop * (1 + thresh)
            if current_price <= stop_loss * (1.0 + stop_thresh_pct) and current_price > stop_loss * 0.95:
                approaching_stop = True
        elif "SELL" in recommendation:
            # Stop is above price; approaching when price >= stop * (1 - thresh)
            if current_price >= stop_loss * (1.0 - stop_thresh_pct) and current_price < stop_loss * 1.05:
                approaching_stop = True

        if approaching_stop:
            alert_key = f"{symbol}:STOP_APPROACH"
            last_time = _ALERT_HISTORY.get(alert_key, 0)
            if (now - last_time) >= dedup_window_sec:
                _ALERT_HISTORY[alert_key] = now
                msg = f"⚠️ <b>Stop-Loss Warning: {symbol}</b> is approaching stop loss @ PKR {stop_loss:.2f} (Current: PKR {current_price:.2f})"
                alerts_triggered.append({
                    "type": "STOP_APPROACH",
                    "symbol": symbol,
                    "title": f"Stop Warning: {symbol}",
                    "message": msg,
                    "price": current_price,
                    "timestamp": now,
                })
                send_telegram_notification(msg, cfg)

    return alerts_triggered


def clear_alert_history():
    """Clears deduplication history for testing."""
    _ALERT_HISTORY.clear()
