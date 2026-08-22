#!/usr/bin/env python3
"""
PSX Telegram Trade Alert & Human Approval Gateway
-------------------------------------------------
Formats institutional Trade Cards and trade execution notifications for Telegram delivery.
"""

from typing import Dict, Any


def format_telegram_trade_alert(card: Dict[str, Any]) -> str:
    """Format high-impact markdown alert for Telegram bot."""
    sym = card.get("symbol", "UNKNOWN")
    name = card.get("name", sym)
    sector = card.get("sector", "Other")
    conviction = card.get("conviction", "HIGH CONVICTION")
    strategy = card.get("strategy", "Momentum Breakout")
    score = card.get("score", 0)
    brackets = card.get("brackets", {})
    
    reasons = card.get("key_reasons", [])
    reasons_str = "\n".join([f"  • {r}" for r in reasons[:4]])
    
    text = (
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
    return text


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
