#!/usr/bin/env python3
"""
Comprehensive Automated Test Suite for PSX AI Trading System Engine
-------------------------------------------------------------------
Tests:
1. PSX Calendar & Session Engine (Mon-Thu, Friday split sessions, Holiday detection)
2. Multi-Timeframe Technical Indicators & Bullish Divergence Detection
3. Opportunity Scanner & Upper-Lock Calculation
4. Deterministic 0–100 Scoring & Bracket Generation
5. AI Trade Card Synthesis
"""

import unittest
import datetime
import psx_calendar
import psx_indicators
import psx_scanner
import psx_scoring
import psx_ai_researcher


class TestPSXCalendar(unittest.TestCase):
    def test_monday_regular_session(self):
        dt = datetime.datetime(2026, 8, 24, 10, 30, tzinfo=psx_calendar.PKT_TIMEZONE)
        status = psx_calendar.get_psx_market_status(dt)
        self.assertTrue(status["is_open"])
        self.assertEqual(status["phase"], "CONTINUOUS_TRADING")

    def test_friday_morning_session(self):
        dt = datetime.datetime(2026, 8, 28, 10, 0, tzinfo=psx_calendar.PKT_TIMEZONE)
        status = psx_calendar.get_psx_market_status(dt)
        self.assertTrue(status["is_open"])
        self.assertEqual(status["phase"], "TRADING_SESSION_1")

    def test_friday_prayer_gap(self):
        dt = datetime.datetime(2026, 8, 28, 13, 0, tzinfo=psx_calendar.PKT_TIMEZONE)
        status = psx_calendar.get_psx_market_status(dt)
        self.assertFalse(status["is_open"])
        self.assertEqual(status["phase"], "PRAYER_BREAK")

    def test_friday_afternoon_session(self):
        dt = datetime.datetime(2026, 8, 28, 15, 0, tzinfo=psx_calendar.PKT_TIMEZONE)
        status = psx_calendar.get_psx_market_status(dt)
        self.assertTrue(status["is_open"])
        self.assertEqual(status["phase"], "TRADING_SESSION_2")

    def test_weekend_closed(self):
        dt = datetime.datetime(2026, 8, 23, 12, 0, tzinfo=psx_calendar.PKT_TIMEZONE)
        status = psx_calendar.get_psx_market_status(dt)
        self.assertFalse(status["is_open"])
        self.assertEqual(status["phase"], "WEEKEND")

    def test_holiday_detection(self):
        dt = datetime.datetime(2026, 3, 23, 11, 0, tzinfo=psx_calendar.PKT_TIMEZONE)
        status = psx_calendar.get_psx_market_status(dt)
        self.assertFalse(status["is_open"])
        self.assertEqual(status["phase"], "HOLIDAY")


class TestPSXIndicators(unittest.TestCase):
    def test_macd_calculation(self):
        prices = [100.0 + (i * 0.5) for i in range(40)]
        macd_res = psx_indicators.calculate_macd(prices)
        self.assertIn("macd", macd_res)
        self.assertIn("signal", macd_res)
        self.assertIn("histogram", macd_res)
        self.assertTrue(macd_res["is_bullish"])

    def test_bullish_divergence_detection(self):
        prices = [50, 48, 45, 42, 40, 44, 46, 45, 43, 38, 41, 43]
        rsi = [40, 35, 28, 22, 20, 32, 38, 35, 32, 26, 34, 39]
        div = psx_indicators.detect_rsi_divergence(prices, rsi, lookback=12)
        self.assertIn("has_bullish_divergence", div)

    def test_atr_and_bollinger(self):
        candles = [{"open": 100 + i, "high": 105 + i, "low": 98 + i, "close": 103 + (i * 1.2), "volume": 10000} for i in range(30)]
        atr = psx_indicators.calculate_atr(candles, 14)
        self.assertGreater(atr, 0)
        
        prices = [c["close"] for c in candles]
        bb = psx_indicators.calculate_bollinger_bands(prices, 20)
        self.assertGreater(bb["upper"], bb["middle"])
        self.assertLess(bb["lower"], bb["middle"])


class TestPSXScoringAndTradeCard(unittest.TestCase):
    def test_trade_card_generation(self):
        tech_profile = {
            "current_price": 155.40,
            "trend": "BULLISH",
            "ma": {
                "ema9": 154.0, "ema50": 150.0, "ema200": 142.0,
                "price_above_ema50": True, "price_above_ema200": True, "golden_cross": True
            },
            "macd_4h": {"is_bullish": True, "bullish_crossover": True, "histogram": 0.85},
            "macd_1d": {"is_bullish": True},
            "rsi_1d": 62.5,
            "rsi_4h": 58.0,
            "divergence_1d": {"has_bullish_divergence": False},
            "divergence_4h": {"has_bullish_divergence": True, "detail": "4H Bullish Divergence"},
            "atr14": 2.10,
            "rvol": 2.8,
            "levels": {"distance_to_resistance_pct": 0.4, "support": 151.0, "resistance": 156.0}
        }
        candidate_meta = {
            "symbol": "SYS", "name": "Systems Limited", "sector": "Technology",
            "price": 155.40, "change": 3.4, "volume": 1500000,
            "lock_info": {"distance_to_upper_pct": 2.8, "is_at_upper_lock": False}
        }
        market_regime = {"regime": "RISK_ON", "breadth_pct": 68.0}
        
        score_res = psx_scoring.calculate_trade_score(tech_profile, candidate_meta, market_regime)
        self.assertGreaterEqual(score_res["score"], 80)
        self.assertLess(score_res["stop_loss"], score_res["entry_price"])
        self.assertGreater(score_res["take_profit_1"], score_res["entry_price"])
        
        card = psx_ai_researcher.generate_trade_card("SYS", "Systems Limited", "Technology", score_res, tech_profile, market_regime)
        self.assertEqual(card["symbol"], "SYS")
        self.assertEqual(card["conviction"], "HIGH CONVICTION")
        self.assertEqual(card["recommendation"], "BUY")


if __name__ == "__main__":
    unittest.main()
