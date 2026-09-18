#!/usr/bin/env python3
"""
Unit tests for Phase 2 and Phase 3 modules:
- Shariah compliance filter (psx_shariah.py)
- SBP Macro Context & sensitivity (psx_macro_context.py)
- Corporate Actions & Ex-Date stop adjustment (psx_corporate_actions.py)
- Piotroski F-Score fundamental valuation (psx_piotroski.py)
- Advanced Indicators: Fibonacci, BB Squeeze, OBV (psx_indicators.py)
- Live Portfolio & P&L Tracker (psx_portfolio.py)
- Corporate Earnings Calendar (psx_earnings_calendar.py)
- Bilingual Urdu NLP Explainer (psx_nlp_explainer.py)
"""

import unittest
from psx_shariah import is_shariah_compliant, get_shariah_symbols, filter_shariah_stocks
from psx_macro_context import get_macro_context, get_sector_macro_multiplier
from psx_corporate_actions import get_upcoming_corporate_actions, is_near_ex_date, check_ex_date_stop_adjustment
from psx_piotroski import calculate_piotroski_f_score
from psx_indicators import compute_fibonacci_levels, compute_bollinger_squeeze, compute_obv
from psx_portfolio import get_portfolio_summary
from psx_earnings_calendar import get_upcoming_earnings_calendar
from psx_nlp_explainer import generate_candidate_explanation


class TestPhase2Phase3(unittest.TestCase):

    def test_shariah_filter(self):
        self.assertTrue(is_shariah_compliant('LUCK'))
        self.assertTrue(is_shariah_compliant('ENGRO'))
        self.assertTrue(is_shariah_compliant('OGDC'))
        self.assertFalse(is_shariah_compliant('UBL'))
        self.assertFalse(is_shariah_compliant('MCB'))

        symbols = get_shariah_symbols()
        self.assertGreater(len(symbols), 50)
        self.assertIn('SYS', symbols)

        test_universe = [{'symbol': 'LUCK'}, {'symbol': 'UBL'}, {'symbol': 'MEBL'}]
        filtered = filter_shariah_stocks(test_universe)
        filtered_syms = [s['symbol'] for s in filtered]
        self.assertIn('LUCK', filtered_syms)
        self.assertIn('MEBL', filtered_syms)
        self.assertNotIn('UBL', filtered_syms)

    def test_macro_context(self):
        macro = get_macro_context()
        self.assertIn('sbp_policy_rate', macro)
        self.assertIn('cycle', macro)
        self.assertEqual(macro['cycle'], 'EASING')

        cement_mult = get_sector_macro_multiplier('Cement')
        self.assertGreater(cement_mult, 1.0)

        bank_mult = get_sector_macro_multiplier('Commercial Banks')
        self.assertLess(bank_mult, 1.0)

    def test_corporate_actions_and_stop_adjustment(self):
        actions = get_upcoming_corporate_actions()
        self.assertIsInstance(actions, list)

        near, act = is_near_ex_date('MEBL', 30)
        self.assertTrue(near)
        self.assertIsNotNone(act)

        # Check drop adjustment detection
        is_adj, note = check_ex_date_stop_adjustment(
            symbol='UNKNOWN_TICKER',
            entry_price=100.0,
            drop_pct=-5.0
        )
        self.assertFalse(is_adj)

    def test_piotroski_f_score(self):
        res = calculate_piotroski_f_score('LUCK')
        self.assertIn('score', res)
        self.assertIn('verdict', res)
        self.assertIn('breakdown', res)
        self.assertGreaterEqual(res['score'], 0)
        self.assertLessEqual(res['score'], 9)

    def test_advanced_indicators(self):
        fib = compute_fibonacci_levels(high=100.0, low=50.0, trend='UP')
        self.assertAlmostEqual(fib['50.0%'], 75.0)
        self.assertAlmostEqual(fib['61.8%'], 69.1, places=1)
        self.assertAlmostEqual(fib['38.2%'], 80.9, places=1)

        prices = [10.0, 10.1, 10.05, 10.08, 10.12, 10.11, 10.09, 10.10, 10.15, 10.12]
        squeeze = compute_bollinger_squeeze(prices, period=5)
        self.assertIn('is_squeeze', squeeze)
        self.assertIn('bandwidth', squeeze)

        close_series = [10.0, 11.0, 10.5, 12.0]
        vol_series = [1000, 1500, 800, 2000]
        obv = compute_obv(close_series, vol_series)
        self.assertEqual(len(obv), 4)
        self.assertGreater(obv[-1], obv[0])

    def test_portfolio_summary(self):
        pf = get_portfolio_summary()
        self.assertIn('total_equity', pf)
        self.assertIn('cash', pf)
        self.assertIn('open_positions', pf)
        self.assertIn('closed_trades', pf)
        self.assertIn('win_rate_pct', pf)
        self.assertIn('sector_exposure', pf)

    def test_earnings_calendar(self):
        cal = get_upcoming_earnings_calendar()
        self.assertIn('upcoming_meetings', cal)
        self.assertIn('total_meetings', cal)
        self.assertGreaterEqual(len(cal['upcoming_meetings']), 1)

    def test_bilingual_nlp_explainer(self):
        sample_candidate = {
            'symbol': 'LUCK',
            'sector': 'Cement',
            'direction': 'LONG',
            'grade': 'A_PLUS',
            'conviction': 94,
            'risk': {
                'entry': 850.0,
                'stop': 816.0,
                'takeProfit1': 918.0,
                'rewardPctTp1': 8.0,
                'riskPct': 4.0,
                'rewardRiskRatio': 2.0
            }
        }
        en = generate_candidate_explanation(sample_candidate, lang='en')
        ur = generate_candidate_explanation(sample_candidate, lang='ur')

        self.assertIn('LUCK', en)
        self.assertIn('Cement', en)
        self.assertIn('94%', en)

        self.assertIn('LUCK', ur)
        self.assertIn('سیمنٹ', ur)
        self.assertIn('سٹاپ لاس', ur)


if __name__ == '__main__':
    unittest.main()
