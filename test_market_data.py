#!/usr/bin/env python3
"""Offline tests for psx_market_data and the real-data chart/history paths in server.py."""

import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import psx_market_data as md

PKT = md.PKT


def pkt_ts(y, m, d, hh, mm, ss=0):
    return int(datetime.datetime(y, m, d, hh, mm, ss, tzinfo=PKT).timestamp())


class TestParsers(unittest.TestCase):

    def test_parse_intraday_timeseries(self):
        t0 = pkt_ts(2026, 10, 5, 9, 40)
        payload = {"status": 1, "data": [
            [t0 + 60, 101.0, 500],
            [t0, 100.0, 1000],
            [t0, 100.5, 200],        # same second -> last price, summed volume
            [t0 + 120, -1, 10],      # invalid price dropped
            ["bad"],                 # malformed dropped
        ]}
        rows = md.parse_intraday_timeseries(json.dumps(payload))
        self.assertEqual(rows, [(t0, 100.5, 1200.0), (t0 + 60, 101.0, 500.0)])
        self.assertEqual(md.parse_intraday_timeseries({"status": 0}), [])
        self.assertEqual(md.parse_intraday_timeseries("not json"), [])

    def test_parse_market_watch_html(self):
        html = """
        <table><tr><th>Index</th></tr><tr><td>KSE100</td></tr></table>
        <table>
          <thead><tr><th>SYMBOL</th><th>SECTOR</th><th>LDCP</th><th>OPEN</th><th>HIGH</th>
                 <th>LOW</th><th>CURRENT</th><th>CHANGE</th><th>VOLUME</th></tr></thead>
          <tbody>
            <tr><td>OGDC</td><td>0820</td><td>317.49</td><td>317.80</td><td>318.38</td>
                <td>316.00</td><td>317.00</td><td>-0.49</td><td>364,000</td></tr>
            <tr><td>BAD</td><td>x</td><td>10</td><td>10</td><td>9</td>
                <td>11</td><td>10</td><td>0</td><td>5</td></tr>
          </tbody>
        </table>"""
        rows = md.parse_market_watch_html(html)
        self.assertEqual(len(rows), 1, "row with high < low must be dropped, not repaired")
        r = rows[0]
        self.assertEqual(r["symbol"], "OGDC")
        self.assertEqual((r["open"], r["high"], r["low"], r["price"]), (317.8, 318.38, 316.0, 317.0))
        self.assertEqual(r["volume"], 364000.0)
        self.assertEqual(r["ldcp"], 317.49)
        self.assertEqual(md.parse_market_watch_html("<html>404</html>"), [])


class TestStore(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = md.MarketDataStore(Path(self.tmp.name) / "md.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_cumulative_volume_becomes_increments(self):
        t0 = pkt_ts(2026, 10, 5, 10, 0)
        self.store.record_snapshot([{"symbol": "ABC", "price": 10, "volume": 1000}], ts=t0, source="mw", cumulative_volume=True)
        self.store.record_snapshot([{"symbol": "ABC", "price": 11, "volume": 1600}], ts=t0 + 60, source="mw", cumulative_volume=True)
        # restart: a fresh store must recover the running total from the DB
        fresh = md.MarketDataStore(self.store.db_path)
        fresh.record_snapshot([{"symbol": "ABC", "price": 12, "volume": 2000}], ts=t0 + 120, source="mw", cumulative_volume=True)
        vols = [t["volume"] for t in fresh.get_ticks("ABC")]
        self.assertEqual(vols, [1000.0, 600.0, 400.0])

    def test_daily_observation_merges_monotonically(self):
        d = "2026-10-05"
        self.store.upsert_daily_observation("abc", d, open_=10, high=10.5, low=9.8, close=10.2, volume=100)
        self.store.upsert_daily_observation("ABC", d, open_=99, high=10.4, low=9.5, close=10.1, volume=250)
        bar = self.store.get_daily_bars("ABC")[d]
        self.assertEqual(bar["open"], 10)      # first open kept
        self.assertEqual(bar["high"], 10.5)    # never shrinks
        self.assertEqual(bar["low"], 9.5)      # widens
        self.assertEqual(bar["close"], 10.1)   # latest
        self.assertEqual(bar["volume"], 250)

    def test_build_bars_from_real_ticks_only(self):
        t_open = pkt_ts(2026, 10, 5, 9, 32)  # Monday session open
        self.store.record_intraday_trades("XYZ", [
            (t_open + 60, 100.0, 10), (t_open + 300, 102.0, 20), (t_open + 840, 99.0, 5),  # first 15m
            (t_open + 960, 101.0, 7),                                                      # second 15m
        ])
        bars = self.store.build_bars("XYZ", "15M", days=30, now=t_open + 3600)
        self.assertEqual(len(bars), 2)
        b0, b1 = bars
        self.assertEqual((b0["open"], b0["high"], b0["low"], b0["close"], b0["volume"]), (100.0, 102.0, 99.0, 99.0, 35.0))
        self.assertEqual(b0["time"], "09:32")
        self.assertEqual(b1["time"], "09:47")
        self.assertEqual(b1["ticks"], 1)
        # no ticks => no bars (never invented)
        self.assertEqual(self.store.build_bars("NONE", "1H", now=t_open + 3600), [])

    def test_richest_source_wins_per_day(self):
        t = pkt_ts(2026, 10, 5, 10, 0)
        self.store.record_snapshot([{"symbol": "DUP", "price": 50}], ts=t, source="screener")
        self.store.record_intraday_trades("DUP", [(t + 5, 50.5, 100)])
        ticks = self.store.get_ticks("DUP")
        self.assertEqual([x["source"] for x in ticks], ["dps_int"])


class TestServerRealData(unittest.TestCase):
    """fetch_stock_timeframe_series / fetch_stock_history must never fabricate prices."""

    @classmethod
    def setUpClass(cls):
        import server
        cls.server = server

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = md.MarketDataStore(Path(self.tmp.name) / "md.db")
        self.p_store = mock.patch.object(md, "get_store", return_value=self.store)
        self.p_store.start()
        self.server._DPS_TIMESERIES_CACHE.clear()
        self.server._STOCK_HISTORY_CACHE.clear()
        # three sessions: (ts, close, volume, open), newest first like DPS
        self.eod = [
            [pkt_ts(2026, 10, 2, 0, 0), 105.0, 3000, 103.0],
            [pkt_ts(2026, 10, 1, 0, 0), 102.0, 2000, 100.0],
            [pkt_ts(2026, 9, 30, 0, 0), 100.0, 1000, 101.0],
        ]

    def tearDown(self):
        self.p_store.stop()
        self.tmp.cleanup()

    def _fake_fetch(self, url, timeout=None, retries=None):
        if "/timeseries/eod/" in url:
            return json.dumps({"status": 1, "data": self.eod})
        raise OSError("offline")

    def test_daily_candles_use_real_range_or_flag_estimate(self):
        self.store.upsert_daily_observation("TST", "2026-10-01", open_=100, high=104.0, low=98.5, close=102)
        with mock.patch.object(self.server, "fetch_url", side_effect=self._fake_fetch):
            candles = self.server.fetch_stock_timeframe_series("TST", "1D", 10)
        self.assertEqual([c["dateStr"] for c in candles], ["2026-09-30", "2026-10-01", "2026-10-02"])
        real = candles[1]
        self.assertEqual((real["high"], real["low"], real["hlEstimated"]), (104.0, 98.5, False))
        est = candles[2]
        self.assertEqual((est["high"], est["low"], est["hlEstimated"]), (105.0, 103.0, True))
        for c in candles:
            self.assertGreater(c["vwap"], 0)
            self.assertGreater(c["circuit_upper"], c["circuit_lower"])

    def test_intraday_timeframes_are_empty_without_observed_ticks(self):
        with mock.patch.object(self.server, "fetch_url", side_effect=self._fake_fetch):
            for tf in ("4H", "1H", "15M"):
                self.assertEqual(self.server.fetch_stock_timeframe_series("TST", tf, 50), [])

    def test_history_change_is_vs_previous_close_and_never_synthetic(self):
        with mock.patch.object(self.server, "fetch_url", side_effect=self._fake_fetch), \
             mock.patch.object(self.server, "HISTORY_CACHE_DIR", Path(self.tmp.name)):
            days = self.server.fetch_stock_history("TST")
            self.assertEqual(days[0]["date"], "2026-10-02")
            self.assertAlmostEqual(days[0]["change"], 3.0)             # 105 - 102 (prev close), not 105 - 103
            self.assertAlmostEqual(days[0]["changePct"], 2.94, places=2)

            self.server._STOCK_HISTORY_CACHE.clear()
            with mock.patch.object(self.server, "fetch_url", side_effect=OSError("offline")):
                self.assertIsNone(self.server.fetch_stock_history("NOHIST"))


if __name__ == "__main__":
    unittest.main()
