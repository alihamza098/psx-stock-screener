#!/usr/bin/env python3
"""Offline tests for psx_trade_desk: swing forward test & order plan, intraday setups,
risk guard, LIVE/PAPER gating and legacy alert switch."""

import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import psx_backtester as bt
import psx_market_data as md
import psx_trade_desk as desk
from psx_costs import CostModel
from test_backtester import EnterOn, bars_from

PKT = md.PKT
NO_SLIP = {"slippage": {"base_bps_per_side": 0}}


def cfg_with(**over):
    cfg = desk.load_config()
    cfg = json.loads(json.dumps(cfg))
    for k, v in over.items():
        if isinstance(v, dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


class AlwaysLong(EnterOn):
    name = "always"
    description = "test"

    def __init__(self):
        super().__init__(on=range(0, 10000), stop_pct=0.05, max_hold=3)


class TestSwingDesk(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = desk.DeskDB(Path(self.tmp.name) / "d.db")
        self.state = Path(self.tmp.name) / "swing.json"
        closes = [100 + i * 0.5 for i in range(60)]
        self.full = {"AAA": bars_from(closes, vol=500000), "BBB": bars_from([50 + i * 0.2 for i in range(60)], vol=800000)}
        self.sent = []
        self.patches = [
            mock.patch.dict(bt.STRATEGIES, {"always": AlwaysLong}),
            mock.patch.object(desk, "load_config", return_value=cfg_with(swing={"strategies": ["always"]})),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _run(self, upto, verdict="FAIL"):
        data = {s: b[:upto] for s, b in self.full.items()}
        report = {"strategies": [{"name": "always", "verdict": verdict, "reason": "test"}]}
        with mock.patch.object(bt, "load_report", return_value=report):
            return desk.run_swing_eod(data=data, db=self.db, costs=CostModel(NO_SLIP),
                                      send=lambda m: self.sent.append(m) or True, state_path=self.state)

    def test_paper_strategy_plans_but_never_alerts(self):
        st = self._run(40)
        s = st["strategies"][0]
        self.assertEqual(s["status"], desk.PAPER)
        self.assertEqual(s["go_live"], self.full["AAA"][39]["date"])
        self.assertTrue(s["plan_entries"], "a signal at the go-live close becomes tomorrow's plan")
        p = s["plan_entries"][0]
        self.assertGreater(p["est_qty"], 0)
        self.assertAlmostEqual(p["limit"], round(p["ref_close"] * 1.01, 2))
        self.assertEqual(self.sent, [])
        self.assertEqual(json.loads(self.state.read_text())["as_of"], st["as_of"])

    def test_forward_test_continues_from_go_live_and_alerts_once_when_live(self):
        self._run(40, verdict="PASS")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Order plan", self.sent[0])
        st = self._run(42, verdict="PASS")
        s = st["strategies"][0]
        self.assertEqual(s["status"], desk.LIVE)
        self.assertTrue(s["open_positions"], "planned entry filled at the next open")
        self.assertEqual(s["open_positions"][0]["entry_date"], self.full["AAA"][40]["date"])
        self.assertEqual(len(self.sent), 1, "nothing new to do → no alert")
        st = self._run(44, verdict="PASS")  # 3-day max hold reached → exit queued for the open
        self.assertTrue(st["strategies"][0]["plan_exits"])
        self.assertEqual(len(self.sent), 2)
        self.assertIn("SELL", self.sent[1])
        self._run(44, verdict="PASS")  # same session again → no duplicate alert
        self.assertEqual(len(self.sent), 2)

    def test_status_rules(self):
        cfg = cfg_with()
        self.assertEqual(desk.swing_status("x", cfg, None)[0], desk.PAPER)
        cfg["alerts"]["force_live"] = ["x"]
        self.assertEqual(desk.swing_status("x", cfg, None)[0], desk.LIVE)
        cfg["alerts"]["disabled"] = ["x"]
        self.assertEqual(desk.swing_status("x", cfg, None)[0], desk.DISABLED)
        icfg = cfg_with()
        good = {"trades": 50, "expectancy_pkr": 120.0, "profit_factor": 1.4}
        self.assertEqual(desk.intraday_status("orb", icfg, good)[0], desk.LIVE)
        self.assertEqual(desk.intraday_status("orb", icfg, dict(good, trades=10))[0], desk.PAPER)
        self.assertEqual(desk.intraday_status("orb", icfg, dict(good, expectancy_pkr=-1))[0], desk.PAPER)


def ts(h, m, s=0, day=datetime.date(2026, 10, 5)):  # a Monday
    return int(datetime.datetime(day.year, day.month, day.day, h, m, s, tzinfo=PKT).timestamp())


class TestIntradaySetups(unittest.TestCase):
    icfg = desk.load_config()["intraday"]

    def _range_ticks(self):
        return [{"ts": ts(9, 33), "price": 100.0}, {"ts": ts(9, 38), "price": 101.0},
                {"ts": ts(9, 43), "price": 99.5}, {"ts": ts(9, 50), "price": 100.8}]

    def test_orb_long_short_and_no_chase(self):
        band = (92.5, 107.5)
        sig = desk.detect_orb("X", 101.3, self._range_ticks(), ts(9, 32), self.icfg, band, can_short=False)
        self.assertEqual((sig["side"], sig["stop"]), ("long", 99.5))
        self.assertAlmostEqual(sig["target"], 101.3 + 2 * 1.8)
        self.assertIsNone(desk.detect_orb("X", 103.0, self._range_ticks(), ts(9, 32), self.icfg, band, False), "no chasing")
        self.assertIsNone(desk.detect_orb("X", 99.2, self._range_ticks(), ts(9, 32), self.icfg, band, False), "short needs eligibility")
        short = desk.detect_orb("X", 99.2, self._range_ticks(), ts(9, 32), self.icfg, band, True)
        self.assertEqual((short["side"], short["stop"]), ("short", 101.0))

    def test_orb_respects_circuit_room(self):
        # upper circuit 102 leaves < 1.5R of room
        self.assertIsNone(desk.detect_orb("X", 101.3, self._range_ticks(), ts(9, 32), self.icfg, (94.0, 102.0), False))

    def test_vwap_reclaim(self):
        mk = lambda t, c, lo: {"timestamp": t, "open": c, "high": c + 0.2, "low": lo, "close": c, "volume": 1000}
        bars = [mk(ts(9, 32), 101, 100.8), mk(ts(9, 37), 101, 100.8), mk(ts(9, 42), 99.6, 99.4),
                mk(ts(9, 47), 99.5, 99.3), mk(ts(9, 52), 99.6, 99.4), mk(ts(9, 57), 100.6, 99.8)]
        sig = desk.detect_vwap_reclaim("X", 100.7, bars, ts(10, 3), self.icfg, (92.5, 107.5))
        self.assertIsNotNone(sig)
        self.assertEqual(sig["stop"], 99.3)


class TestIntradayDesk(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = md.MarketDataStore(Path(self.tmp.name) / "md.db")
        self.db = desk.DeskDB(Path(self.tmp.name) / "d.db")
        self.sent = []
        self.desk = desk.IntradayDesk(db=self.db, store=self.store, costs=CostModel(NO_SLIP),
                                      send=lambda m: self.sent.append(m) or True)
        for t, p in [(ts(9, 33), 100.0), (ts(9, 38), 101.0), (ts(9, 43), 99.5), (ts(9, 55), 100.9)]:
            self.store.record_snapshot([{"symbol": "OGDC", "price": p}], ts=t)
        self.stock = {"symbol": "OGDC", "price": 101.3, "ldcp": 100.0, "todayVolume": 3_000_000,
                      "avgVolume30d": 2_000_000, "change": 1.3}
        self.p = mock.patch.object(desk, "load_config", return_value=cfg_with())
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def now(self, h, m):
        return datetime.datetime(2026, 10, 5, h, m, tzinfo=PKT)

    def test_paper_entry_then_target_exit_without_alerts(self):
        out = self.desk.tick([self.stock], now=self.now(10, 0))
        entries = [e for e in out["events"] if e["type"] == "entry"]
        self.assertEqual(len(entries), 1)
        t = entries[0]["trade"]
        self.assertEqual((t["symbol"], t["side"], t["status_at_signal"]), ("OGDC", "long", desk.PAPER))
        # 0.5% of 500k = 2,500 risk / 1.8 per share → 1,388 shares, capped at 25% (125k / 101.3 = 1,233)
        self.assertEqual(t["qty"], 1233)
        self.assertEqual(self.sent, [], "PAPER strategies are never alerted")

        out = self.desk.tick([dict(self.stock, price=105.2)], now=self.now(10, 30))
        exits = [e for e in out["events"] if e["type"] == "exit"]
        self.assertEqual(exits[0]["reason"], "target")
        closed = self.db.trades("state='CLOSED'")[0]
        self.assertAlmostEqual(closed["exit"], 101.3 + 3.6)
        self.assertGreater(closed["net_pnl"], 0)

    def test_live_strategy_alerts_and_time_exit(self):
        with mock.patch.object(desk, "load_config", return_value=cfg_with(alerts={"force_live": ["orb"]})):
            self.desk.tick([self.stock], now=self.now(10, 0))
            self.assertEqual(len(self.sent), 1)
            self.assertIn("BUY 1,233 OGDC", self.sent[0])
            self.desk.tick([dict(self.stock, price=101.6)], now=self.now(15, 16))
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.db.trades("state='CLOSED'")[0]["exit_reason"], "time_exit")

    def test_no_entries_without_volume_or_after_cutoff_or_when_locked(self):
        self.assertFalse(self.desk.tick([dict(self.stock, todayVolume=None)], now=self.now(10, 0))["events"])
        self.assertFalse(self.desk.tick([self.stock], now=self.now(14, 45))["events"])
        self.assertFalse(self.desk.tick([self.stock], now=self.now(9, 40))["events"], "opening range not complete")
        for _ in range(2):  # two big losers today → daily loss lock (1.5% of 500k = 7,500)
            tid = self.db.open_trade({"date": "2026-10-05", "strategy": "orb", "symbol": "ZZZ", "side": "long",
                                      "status_at_signal": "PAPER", "entry_time": "09:50", "entry": 100, "stop": 95,
                                      "target": 110, "qty": 1000, "entry_fee": 0, "reason": "t"})
            self.db.close_trade(tid, "10:00", 96, "stop", -4000.0, -4.0)
        risk = self.desk.risk_state(desk.load_config(), "2026-10-05")
        self.assertTrue(risk["locked"])
        self.assertFalse([e for e in self.desk.tick([self.stock], now=self.now(10, 0))["events"] if e["type"] == "entry"])


class TestLegacyAlertSwitch(unittest.TestCase):

    def test_legacy_signal_alerts_off_by_default(self):
        import psx_telegram_bot as tg
        with mock.patch.object(tg, "_send_message", side_effect=AssertionError("must not send")), \
             mock.patch.object(tg, "is_enabled", return_value=True):
            self.assertFalse(tg.legacy_signals_enabled())
            self.assertFalse(tg.alert_weekly_scan_candidate({"symbol": "X", "grade": "A_PLUS"}))
            self.assertFalse(tg.alert_intraday_close("X", 1, 1, 1, 1, True))


if __name__ == "__main__":
    unittest.main()
