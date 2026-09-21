#!/usr/bin/env python3
"""
Unit Tests for Stage 5: PSX-Specific Intelligence
- 5.1 Event-risk flag: board meetings, financial results, book closures in next 1-2 days suppress STRONG signals.
- 5.2 Ex-dividend / book-closure adjustment: do not penalize payout drop as bearish momentum.
- 5.3 Index filter: KSE-100 strong downtrend downgrades BUY signals one tier (gracefully skips if unavailable).
- 5.4 Sector context: Relative strength vs sector peers (Alpha vs Beta).
"""

import unittest
import datetime
from scoring_engine import (
    parse_psx_date_str,
    evaluate_psx_intelligence,
    compute_v2_recommendation
)


class TestPSXIntelligence(unittest.TestCase):

    def setUp(self):
        self.ref_date = datetime.date(2026, 9, 21)
        self.base_stock = {
            "symbol": "OGDC",
            "price": 100.0,
            "change": 2.5,
            "volume": 200000.0,
            "sector": "Oil & Gas Exploration Companies",
            "avgVolume": 150000.0
        }
        # Ascending 50 daily bars for bullish base
        self.bullish_history = []
        for i in range(50):
            p = 80.0 + i * 0.4
            self.bullish_history.append({
                "date": f"2026-07-{i+1:02d}",
                "open": p - 0.5,
                "high": p + 1.0,
                "low": p - 0.5,
                "close": p,
                "volume": 150000.0
            })

    def test_parse_psx_date_str(self):
        self.assertEqual(parse_psx_date_str("01/09/2026"), datetime.date(2026, 9, 1))
        self.assertEqual(parse_psx_date_str("2026-09-21"), datetime.date(2026, 9, 21))
        self.assertEqual(parse_psx_date_str("Sep 4, 2026"), datetime.date(2026, 9, 4))
        self.assertEqual(parse_psx_date_str("September 22, 2026"), datetime.date(2026, 9, 22))
        self.assertEqual(parse_psx_date_str("August 20, 2026 3:50 PM"), datetime.date(2026, 8, 20))
        self.assertIsNone(parse_psx_date_str(""))
        self.assertIsNone(parse_psx_date_str(None))

    def test_event_risk_flag_and_strong_signal_suppression(self):
        # 1. Company with Board Meeting tomorrow (2026-09-22)
        company_data = {
            "announcements": [
                {"date": "Sep 22, 2026", "title": "BOARD MEETING AND CLOSED PERIOD", "link": ""},
                {"date": "Aug 10, 2026", "title": "Material Information", "link": ""}
            ]
        }
        intel = evaluate_psx_intelligence(
            stock=self.base_stock,
            company_data=company_data,
            current_date=self.ref_date
        )
        self.assertTrue(intel["event_risk"]["has_risk"])
        self.assertTrue(intel["event_risk"]["suppress_strong"])
        self.assertIn("BOARD MEETING", intel["event_risk"]["warning"].upper())

        # 2. Strong BUY candidate should be suppressed to standard BUY
        # Force a high score setup
        stock_high = dict(self.base_stock, change=4.5, volume=500000.0)
        res = compute_v2_recommendation(
            stock=stock_high,
            history=self.bullish_history,
            psx_intelligence=intel
        )
        # Without event risk, high momentum would be STRONG BUY; with event risk, suppressed to BUY
        self.assertNotEqual(res["recommendation"], "STRONG BUY")
        if res["original_recommendation"] == "STRONG BUY":
            self.assertEqual(res["recommendation"], "BUY")
        self.assertTrue(any(f.get("name") == "Event Risk Flag" for f in res["factors"]))

    def test_ex_dividend_negative_momentum_neutralization(self):
        # Stock drops -3.5% on ex-dividend date
        div_calendar = [{
            "symbol": "OGDC",
            "dividendAmount": "35% Final Cash",
            "exDividendDate": "21/09/2026", # Today
            "bookClosure": "22/09/2026 - 24/09/2026"
        }]
        intel = evaluate_psx_intelligence(
            stock=dict(self.base_stock, change=-3.5),
            dividend_calendar=div_calendar,
            current_date=self.ref_date
        )
        self.assertTrue(intel["ex_dividend"]["is_ex_date"])
        self.assertTrue(intel["ex_dividend"]["neutralize_negative_momentum"])
        self.assertEqual(intel["ex_dividend"]["dividend_pkr"], 3.5)

        # In scoring engine, drop is neutralized instead of receiving -12 pts for momentum
        stock_down = dict(self.base_stock, change=-3.5)
        res_ex = compute_v2_recommendation(
            stock=stock_down,
            history=self.bullish_history,
            psx_intelligence=intel
        )
        # Factor list should have Ex-Dividend Adjustment and not huge negative raw flow
        self.assertTrue(any(f.get("name") == "Ex-Dividend Adjustment" for f in res_ex["factors"]))
        flow_factor = next(f for f in res_ex["factors"] if f.get("name") == "Order Flow & Momentum")
        # Since momentum was neutralized to 0, points should be 0.0
        self.assertEqual(flow_factor["points"], 0.0)

    def test_index_filter_downtrend_downgrades_buy(self):
        # KSE-100 down -1.2% (Strong Downtrend)
        index_data = {
            "indices": [
                {"name": "KSE100", "value": 170000.0, "changePercent": -1.20, "isPositive": False}
            ]
        }
        intel = evaluate_psx_intelligence(
            stock=self.base_stock,
            index_data=index_data,
            current_date=self.ref_date
        )
        self.assertTrue(intel["index_filter"]["is_downtrend"])
        self.assertTrue(intel["index_filter"]["downgrade_buy"])

        # Run scoring engine with candidate that would qualify for BUY
        res = compute_v2_recommendation(
            stock=dict(self.base_stock, change=1.5),
            history=self.bullish_history,
            psx_intelligence=intel
        )
        # Signal downgraded one tier
        if res["original_recommendation"] == "BUY":
            self.assertEqual(res["recommendation"], "HOLD")
        elif res["original_recommendation"] == "STRONG BUY":
            self.assertEqual(res["recommendation"], "BUY")
        self.assertTrue(any(f.get("name") == "Index Filter" for f in res["factors"]))

    def test_index_filter_graceful_skip_when_unavailable(self):
        # Missing index data should not break anything
        intel = evaluate_psx_intelligence(
            stock=self.base_stock,
            index_data=None,
            current_date=self.ref_date
        )
        self.assertEqual(intel["index_filter"]["status"], "unavailable")
        self.assertFalse(intel["index_filter"]["downgrade_buy"])

        res = compute_v2_recommendation(
            stock=self.base_stock,
            history=self.bullish_history,
            psx_intelligence=intel
        )
        self.assertIn("recommendation", res)

    def test_sector_context_relative_strength(self):
        all_stocks = [
            {"symbol": "OGDC", "sector": "Oil & Gas Exploration Companies", "change": 3.0, "price": 100.0},
            {"symbol": "PPL", "sector": "Oil & Gas Exploration Companies", "change": 1.0, "price": 120.0},
            {"symbol": "POL", "sector": "Oil & Gas Exploration Companies", "change": 0.5, "price": 400.0},
            {"symbol": "MARI", "sector": "Oil & Gas Exploration Companies", "change": -0.5, "price": 2500.0},
            {"symbol": "HBL", "sector": "Commercial Banks", "change": 2.0, "price": 110.0}
        ]
        # Sector average for O&G is (3.0 + 1.0 + 0.5 - 0.5) / 4 = 1.0%
        # OGDC change is 3.0% -> Relative strength is +2.0% (Alpha)
        intel = evaluate_psx_intelligence(
            stock=self.base_stock,
            all_stocks=all_stocks,
            current_date=self.ref_date
        )
        sec = intel["sector_context"]
        self.assertEqual(sec["sector"], "Oil & Gas Exploration Companies")
        self.assertEqual(sec["sector_avg_change"], 1.0)
        self.assertEqual(sec["relative_strength"], 1.5) # base_stock.change is 2.5, 2.5 - 1.0 = 1.5
        self.assertIn("Sector Alpha", sec["tag"])

        # Verify Factor 7 is generated with bullish direction (+4 pts)
        res = compute_v2_recommendation(
            stock=self.base_stock,
            history=self.bullish_history,
            psx_intelligence=intel
        )
        sec_factor = next(f for f in res["factors"] if f.get("name") == "Sector Context")
        self.assertEqual(sec_factor["direction"], "BULLISH")
        self.assertEqual(sec_factor["points"], 4.0)


if __name__ == "__main__":
    unittest.main()
