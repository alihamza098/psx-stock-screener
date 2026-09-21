import unittest
from multibagger.risk_shield import evaluate_risk_shield
from multibagger.scoring import (
    classify_lifecycle_stage,
    calculate_float_squeeze_index,
    calculate_multibagger_score,
    STAGE_1_STEALTH,
    STAGE_2_CATALYST,
    STAGE_3_VELOCITY,
)
from multibagger.research.synthesizer import generate_investment_thesis


class TestMultibaggerV2(unittest.TestCase):
    def test_risk_shield_defaulter(self):
        stock_defaulter = {
            'symbol': 'BADCO',
            'isNC': True,
            'price': 1.5,
            'volume': 10_000
        }
        res = evaluate_risk_shield(stock_defaulter, free_float_shares=5_000_000)
        self.assertEqual(res['status'], 'FAIL')
        self.assertFalse(res['passed'])
        self.assertIn('DEFAULTERS_SEGMENT', res['flags'])
        self.assertGreaterEqual(res['penalty'], 30)

    def test_risk_shield_churn_trap(self):
        # Stock trading >35% of total free float in one day
        stock_churn = {
            'symbol': 'CHURN',
            'isNC': False,
            'price': 5.0,
            'volume': 20_000_000
        }
        res = evaluate_risk_shield(stock_churn, free_float_shares=40_000_000)
        self.assertIn('OPERATOR_CHURN_TRAP', res['flags'])

    def test_risk_shield_clean_stock(self):
        stock_clean = {
            'symbol': 'GOODCO',
            'isNC': False,
            'price': 18.5,
            'volume': 500_000
        }
        res = evaluate_risk_shield(stock_clean, free_float_shares=50_000_000)
        self.assertEqual(res['status'], 'PASS')
        self.assertTrue(res['passed'])
        self.assertEqual(len(res['flags']), 0)
        self.assertEqual(res['penalty'], 0)

    def test_float_squeeze_index(self):
        tiny_stock = {
            'free_float_shares': 15_000_000,
            'free_float_pct': 18.0
        }
        sq_high = calculate_float_squeeze_index(tiny_stock)
        self.assertGreaterEqual(sq_high, 80)

        large_stock = {
            'free_float_shares': 250_000_000,
            'free_float_pct': 60.0
        }
        sq_low = calculate_float_squeeze_index(large_stock)
        self.assertLessEqual(sq_low, 50)

    def test_stage_classification(self):
        stage1 = classify_lifecycle_stage(
            stock={'current': 12.0, 'vol_ratio': 0.8},
            breakdown={'accum_pts': 15, 'turnaround_pts': 15, 'capital_increase_pts': 0, 'growth_pts': 5}
        )
        self.assertEqual(stage1['stage'], STAGE_1_STEALTH)
        self.assertIn('Stage 1', stage1['stage_name'])

        stage2 = classify_lifecycle_stage(
            stock={'current': 14.0, 'vol_ratio': 1.8},
            breakdown={'capital_increase_pts': 20, 'name_change_pts': 10, 'accum_pts': 10}
        )
        self.assertEqual(stage2['stage'], STAGE_2_CATALYST)
        self.assertIn('Stage 2', stage2['stage_name'])

        stage3 = classify_lifecycle_stage(
            stock={'current': 25.0, 'vol_ratio': 4.5, 'change': 6.5},
            breakdown={'growth_pts': 20, 'accum_pts': 15, 'turnaround_pts': 15}
        )
        self.assertEqual(stage3['stage'], STAGE_3_VELOCITY)
        self.assertIn('Stage 3', stage3['stage_name'])

    def test_multibagger_scoring_v2_integration(self):
        res = calculate_multibagger_score(
            price=15.0,
            has_name_or_sector_change=False,
            has_capital_increase=True,
            volumes=[100_000.0] * 30,
            consecutive_qoq_growth=2,
            price_ceiling=20.0,
            free_float_shares=30_000_000,
            free_float_pct=25.0
        )
        self.assertIn('stage', res)
        self.assertIn('stage_name', res)
        self.assertIn('float_squeeze_index', res)
        self.assertIsInstance(res['float_squeeze_index'], int)
        self.assertIn('name_change_pts', res['breakdown'])
        self.assertIn('capital_increase_pts', res['breakdown'])

    def test_investment_thesis_generation(self):
        thesis = generate_investment_thesis('THCCL')
        self.assertEqual(thesis['symbol'], 'THCCL')
        self.assertIn('thesis_pillars', thesis)
        pillars = thesis['thesis_pillars']
        self.assertIn('pillar_1_spark', pillars)
        self.assertIn('pillar_2_float_squeeze', pillars)
        self.assertIn('pillar_3_asset_floor', pillars)
        self.assertIn('pillar_4_invalidation', pillars)
        self.assertIn('historical_twin', thesis)
        self.assertIn('risk_shield', thesis)

    def test_archetype_benchmark_library_expansion(self):
        from multibagger.analogs import ARCHETYPE_LIBRARY
        self.assertGreaterEqual(len(ARCHETYPE_LIBRARY), 30, 'Benchmark library must contain at least 30 historical runners')
        tickers = [a['ticker'] for a in ARCHETYPE_LIBRARY]
        self.assertEqual(len(tickers), len(set(tickers)), 'All tickers in archetype library must be unique')
        for a in ARCHETYPE_LIBRARY:
            self.assertIn('ticker', a)
            self.assertIn('company_name', a)
            self.assertIn('sector', a)
            self.assertIn('price', a)
            self.assertGreater(a['price'], 0)
            self.assertIn('float_shares', a)
            self.assertGreater(a['float_shares'], 0)
            self.assertIn('peak_multiple', a)
            self.assertGreaterEqual(a['peak_multiple'], 3.0)
            self.assertIn('run_start_date', a)
            self.assertIn('run_peak_date', a)
            self.assertIn('tags', a)
            self.assertGreaterEqual(len(a['tags']), 1)
            self.assertIn('notes', a)
            self.assertGreater(len(a['notes']), 10)


if __name__ == '__main__':
    unittest.main()
