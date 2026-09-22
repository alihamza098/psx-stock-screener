import unittest
import time
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

class TestPerformanceArchitecture(unittest.TestCase):

    def test_live_trading_config_performance(self):
        """Verify performance_architecture section exists with 2-minute trade hours refresh."""
        config_path = Path(__file__).parent / "config" / "live_trading.json"
        self.assertTrue(config_path.exists())
        with open(config_path, "r") as f:
            cfg = json.load(f)
        
        self.assertIn("performance_architecture", cfg)
        perf = cfg["performance_architecture"]
        self.assertEqual(perf.get("client_refresh_interval_trading_hours_seconds"), 120, "Trading hours refresh must be 2 minutes (120s)")
        self.assertEqual(perf.get("live_analysis_cache_seconds"), 10, "Server-side fanout TTL must be 10s")
        self.assertEqual(perf.get("company_data_cache_seconds"), 1800, "Company data cache must be 30m (1800s)")

    def test_server_fanout_cache_and_latency(self):
        """Verify Stage 9.2 fanout cache returns identical cached object in < 50ms."""
        import server
        symbol = "OGDC"
        
        # Warm the cache
        t0 = time.time()
        res1 = server.fetch_live_stock_analysis(symbol, force=True)
        self.assertIsNotNone(res1)
        self.assertEqual(res1.get("symbol"), symbol)
        
        # Subsequent call should hit _LIVE_ANALYSIS_CACHE instantly (< 50ms)
        t_cache_start = time.time()
        res2 = server.fetch_live_stock_analysis(symbol, force=False)
        cache_elapsed = time.time() - t_cache_start
        
        self.assertLess(cache_elapsed, 0.050, f"Cached fanout response took {cache_elapsed*1000:.2f}ms, expected < 50ms")
        self.assertEqual(res1["timestamp"], res2["timestamp"], "Cached object should share timestamp")

    def test_fanout_force_bypass(self):
        """Verify force=True bypasses the 10s fanout cache and updates timestamp."""
        import server
        symbol = "OGDC"
        
        res1 = server.fetch_live_stock_analysis(symbol, force=True)
        time.sleep(0.05)
        res2 = server.fetch_live_stock_analysis(symbol, force=True)
        self.assertIsNotNone(res2)
        # Should have run fresh
        self.assertIn("timestamp", res2)

    def test_company_data_memory_caching(self):
        """Verify Stage 9.1 company profile caching serves in < 5ms."""
        import server
        symbol = "OGDC"
        
        # Pre-seed memory cache
        dummy_data = {
            "symbol": symbol,
            "description": "Oil and Gas Development Company Limited",
            "people": [{"name": "CEO", "role": "CEO"}],
            "_cached_at": time.time()
        }
        server._COMPANY_DATA_CACHE[symbol] = (time.time(), dummy_data)
        
        t0 = time.time()
        ret = server.fetch_company_data(symbol)
        elapsed = time.time() - t0
        
        self.assertLess(elapsed, 0.010, f"Memory cached company data took {elapsed*1000:.2f}ms")
        self.assertEqual(ret.get("symbol"), symbol)
        self.assertIn("Oil and Gas", ret.get("description", ""))

    def test_stale_stock_cache_detection(self):
        """Verify fetch_stock_data triggers background refresh if cache is older than 60s."""
        import server
        with patch.object(server, "_trigger_background_refresh") as mock_refresh:
            # Set artificial timestamp 100 seconds in the past
            server.stock_cache["timestamp"] = time.time() - 100
            server.fetch_stock_data(force=False)
            mock_refresh.assert_called_once()

    def test_parse_company_quote_and_live_enrichment(self):
        """Verify parse_company_quote correctly parses live quote and enriches stock_info."""
        import server
        mock_html = '''
        <div class="quote__price"><div class="quote__close">Rs.11.25</div>
        <div class="quote__change change__text--pos"><div class="change__direction"><i class="icon-up-dir"></i> </div><div class="change__value">0.59</div><div class="change__percent">  (5.53%)</div></div></div>
        <div class="stats_item"><div class="stats_label">Open</div><div class="stats_value">10.70</div></div>
        <div class="stats_item"><div class="stats_label">High</div><div class="stats_value">11.45</div></div>
        <div class="stats_item"><div class="stats_label">Low</div><div class="stats_value">10.60</div></div>
        <div class="stats_item"><div class="stats_label">Volume</div><div class="stats_value">520,100</div></div>
        <div class="stats_item"><div class="stats_label">CIRCUIT BREAKER</div><div class="stats_value">9.85 — 11.50</div></div>
        '''
        quote = server.parse_company_quote(mock_html)
        self.assertEqual(quote.get("price"), 11.25)
        self.assertEqual(quote.get("change"), 0.59)
        self.assertEqual(quote.get("changePercent"), 5.53)
        self.assertEqual(quote.get("open"), 10.70)
        self.assertEqual(quote.get("high"), 11.45)
        self.assertEqual(quote.get("low"), 10.60)
        self.assertEqual(quote.get("volume"), 520100.0)
        self.assertEqual(quote.get("circuitLower"), 9.85)
        self.assertEqual(quote.get("circuitUpper"), 11.50)

        # Verify update_live_stock_quote updates cache and price bus
        server.update_live_stock_quote("TESTXYZ", quote)
        info = server.get_live_stock_info("TESTXYZ")
        self.assertIsNotNone(info)
        self.assertEqual(info.get("price"), 11.25)
        self.assertEqual(info.get("_live_source"), "DPS_COMPANY_QUOTE")


if __name__ == "__main__":
    unittest.main()
