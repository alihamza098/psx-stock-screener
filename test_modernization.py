#!/usr/bin/env python3
"""
Unit tests for 5-Pillar Modernization:
1. TradingView Chart Data & Circuit Bands
2. Portfolio Performance Analytics & Trade Journal
3. Archetype Benchmark Library (30+ Historical PSX Runners)
4. PWA Manifest & Offline Configuration
"""

import unittest
import json
from pathlib import Path


class TestModernizationFeatures(unittest.TestCase):

    def test_chart_data_enrichment_and_circuit_bands(self):
        from server import fetch_stock_timeframe_series
        candles = fetch_stock_timeframe_series('OGDC', '1D', 10)
        self.assertIsInstance(candles, list)
        self.assertGreater(len(candles), 0, "Should return at least 1 candle for OGDC")
        
        sample = candles[-1]
        self.assertIn('open', sample)
        self.assertIn('high', sample)
        self.assertIn('low', sample)
        self.assertIn('close', sample)
        self.assertIn('volume', sample)
        self.assertIn('vwap', sample)
        self.assertIn('circuit_upper', sample)
        self.assertIn('circuit_lower', sample)
        
        self.assertGreater(sample['vwap'], 0)
        self.assertGreater(sample['circuit_upper'], sample['circuit_lower'])
        self.assertGreaterEqual(sample['circuit_upper'], sample['close'] * 0.9)

    def test_chart_timeframes(self):
        from server import fetch_stock_timeframe_series
        for tf in ['1D', '4H', '1W']:
            candles = fetch_stock_timeframe_series('SYS', tf, 5)
            self.assertIsInstance(candles, list)
            if candles:
                self.assertIn('close', candles[0])
                self.assertIn('vwap', candles[0])

    def test_portfolio_drawdown_and_sharpe_analytics(self):
        from psx_portfolio import get_portfolio_summary
        summary = get_portfolio_summary()
        
        self.assertIn('max_drawdown_pct', summary)
        self.assertIn('max_drawdown_pkr', summary)
        self.assertIn('sharpe_ratio', summary)
        self.assertIn('payoff_ratio', summary)
        self.assertIn('win_loss_ratio', summary)
        self.assertIn('trade_journal', summary)
        
        self.assertGreaterEqual(summary['max_drawdown_pct'], 0.0)
        self.assertGreaterEqual(summary['max_drawdown_pkr'], 0.0)
        self.assertIsInstance(summary['trade_journal'], list)

    def test_expanded_archetype_library_30_plus(self):
        from multibagger.analogs import ARCHETYPE_LIBRARY
        self.assertGreaterEqual(len(ARCHETYPE_LIBRARY), 30, "Must contain 30+ PSX historical runners")
        tickers = [a['ticker'] for a in ARCHETYPE_LIBRARY]
        self.assertEqual(len(tickers), len(set(tickers)), "Archetype tickers must be unique")
        
        # Check newly added notable runners
        for sym in ['AVN', 'SYS', 'AIRLINK', 'PAEL', 'MEBL', 'SEARL', 'CHCC', 'MLCF', 'HUBC']:
            self.assertIn(sym, tickers, f"Archetype {sym} must be present in library")

    def test_pwa_manifest_shortcuts_and_spec(self):
        manifest_path = Path(__file__).parent / 'manifest.json'
        self.assertTrue(manifest_path.exists())
        with open(manifest_path, 'r', encoding='utf-8') as f:
            manifest = json.load(f)
        
        self.assertIn('shortcuts', manifest)
        self.assertGreaterEqual(len(manifest['shortcuts']), 4)
        shortcut_names = [s['short_name'] for s in manifest['shortcuts']]
        self.assertIn('Screener', shortcut_names)
        self.assertIn('Portfolio', shortcut_names)
        self.assertIn('Multibagger', shortcut_names)
        self.assertIn('Alerts', shortcut_names)


if __name__ == '__main__':
    unittest.main()
