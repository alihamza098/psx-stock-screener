#!/usr/bin/env python3
"""Offline tests for psx_costs and psx_backtester (engine mechanics, PSX rules, verdicts).

Price series here are generated only to exercise the engine; no result in this file says
anything about real PSX performance.
"""

import datetime
import random
import unittest

import psx_backtester as bt
from psx_costs import CostModel

NO_SLIP = {"slippage": {"base_bps_per_side": 0}}


def make_dates(n, start=datetime.date(2024, 1, 1)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


def bars_from(closes, opens=None, vol=100000, real_hl=None):
    dates = make_dates(len(closes))
    out = []
    for i, c in enumerate(closes):
        o = opens[i] if opens else (closes[i - 1] if i else c)
        b = {"date": dates[i], "open": float(o), "close": float(c), "volume": float(vol),
             "high": max(o, c), "low": min(o, c), "hlEstimated": True}
        if real_hl and i in real_hl:
            b["high"], b["low"] = real_hl[i]
            b["hlEstimated"] = False
        out.append(b)
    return out


class EnterOn(bt.Strategy):
    """Test strategy: go long on given bar indexes with a fixed stop/target."""
    name = "enter_on"
    warmup = 1

    def __init__(self, on, stop_pct=0.05, target_pct=None, max_hold=50, weight=None):
        self.on, self.stop_pct, self.target_pct, self.max_hold, self.weight = set(on), stop_pct, target_pct, max_hold, weight

    def entry(self, symbol, i, bars, st):
        if i not in self.on:
            return None
        c = bars[i]["close"]
        sig = {"side": "long", "stop": c * (1 - self.stop_pct),
               "target": c * (1 + self.target_pct) if self.target_pct else None,
               "max_hold": self.max_hold, "score": 1.0}
        if self.weight:
            sig["weight_pct"] = self.weight
        return sig


class TestCosts(unittest.TestCase):

    def test_ktrade_commission(self):
        c = CostModel(NO_SLIP)
        # >= Rs 20: 0.15% of value, plus 15% SST
        self.assertAlmostEqual(c.commission(100, 1000), 0.15 * 1000 * 1.15)
        # below Rs 20: 3 paisa per share minimum
        self.assertAlmostEqual(c.commission(10, 1000), 0.03 * 1000 * 1.15)
        self.assertEqual(c.commission(100, 1000, charge=False), 0.0)

    def test_round_trip_and_day_trade(self):
        c = CostModel(NO_SLIP)
        swing = c.round_trip_pct(100, 1000)
        day = c.round_trip_pct(100, 1000, day_trade=True)
        self.assertAlmostEqual(swing, 2 * 0.15 * 1.15 + 2 * 0.00065, places=4)
        self.assertLess(day, swing)

    def test_circuit_band_and_cgt(self):
        c = CostModel()
        self.assertEqual(c.circuit_band(100), (92.5, 107.5))
        self.assertEqual(c.circuit_band(5), (4.0, 6.0))  # Rs 1 minimum band
        self.assertEqual(c.cgt(1000), 150.0)
        self.assertEqual(c.cgt(-500), 0.0)
        self.assertFalse(c.can_short("OGDC"))
        self.assertTrue(CostModel({"market_rules": {"short_selling_eligible_symbols": ["ogdc"]}}).can_short("OGDC"))


class TestEngine(unittest.TestCase):

    def setUp(self):
        self.costs = CostModel(NO_SLIP)
        self.cfg = {"capital_pkr": 500000.0, "risk_per_trade_pct": 1.0, "max_position_pct": 20.0,
                    "max_positions": 5, "min_trade_value_pkr": 1000.0}

    def test_signal_fills_next_open_no_lookahead(self):
        closes = [100] * 5 + [101, 102, 103, 104, 105]
        opens = [100] * 5 + [100.5, 101.5, 102.5, 103.5, 104.5]
        res = bt.simulate(EnterOn({5}), {"AAA": bars_from(closes, opens)}, settings=self.cfg, costs=self.costs)
        t = res["trades"][0]
        self.assertEqual(t["entry_date"], make_dates(10)[6])  # signal on bar 5's close → bar 6's open
        self.assertAlmostEqual(t["entry"], 101.5)

    def test_position_size_respects_risk_and_cap(self):
        closes = [100] * 10
        res = bt.simulate(EnterOn({2}, stop_pct=0.05), {"AAA": bars_from(closes)}, settings=self.cfg, costs=self.costs)
        qty = res["trades"][0]["qty"]
        # 1% of 500k = 5,000 risk / Rs 5 per share = 1,000 shares = Rs 100k = exactly the 20% cap
        self.assertEqual(qty, 1000)

    def test_no_buy_into_upper_circuit_lock(self):
        closes = [100, 100, 100, 107.5, 107.5, 107.5]
        opens = [100, 100, 100, 107.5, 107.5, 107.5]
        res = bt.simulate(EnterOn({2}), {"AAA": bars_from(closes, opens)}, settings=self.cfg, costs=self.costs)
        self.assertEqual(res["trades"], [], "locked limit-up day has no sellers")

    def test_lower_circuit_lock_delays_stop_exit(self):
        # entry 100 on bar 2 (stop 95). Bar 3 closes 94 → stop exit queued for bar 4's open,
        # but bar 4 is pinned limit-down (open = close = 94 - 7.05) → no buyers; fill on bar 5.
        closes = [100, 100, 100, 94, 86.95, 85]
        opens = [100, 100, 100, 99, 86.95, 85]
        res = bt.simulate(EnterOn({1}), {"AAA": bars_from(closes, opens)}, settings=self.cfg, costs=self.costs)
        t = res["trades"][0]
        self.assertEqual(t["exit_reason"], "stop")
        self.assertEqual(t["exit_date"], make_dates(6)[5])
        self.assertAlmostEqual(t["exit"], 85)

    def test_intrabar_stop_with_real_range(self):
        closes = [100, 100, 100, 99, 99]
        res = bt.simulate(EnterOn({1}), {"AAA": bars_from(closes, real_hl={3: (100.5, 94.0)})},
                          settings=self.cfg, costs=self.costs)
        t = res["trades"][0]
        self.assertEqual((t["exit_reason"], t["exit"], t["exit_date"]), ("stop", 95.0, make_dates(5)[3]))

    def test_t_plus_2_settlement_limits_cash(self):
        cfg = dict(self.cfg, max_position_pct=100.0, risk_per_trade_pct=100.0, max_positions=1)
        closes = [100] * 12
        # buy with ~all cash on bar 2, target exit via max_hold, re-enter next day: proceeds not settled
        strat = EnterOn({1, 4, 5, 6, 7}, stop_pct=0.5, max_hold=1, weight=99)
        res = bt.simulate(strat, {"AAA": bars_from(closes)}, settings=cfg, costs=self.costs)
        dates = make_dates(12)
        by_date = {t["entry_date"]: t["qty"] for t in res["trades"]}
        self.assertGreater(by_date[dates[2]], 4900)
        # first exit fills on bar 4's open but the proceeds only settle on bar 6 (T+2):
        # the bar-5 entry can only use the ~1% cash left over
        self.assertLess(by_date[dates[5]], 100)
        self.assertGreater(by_date[dates[8]], 4800)  # bar-4 proceeds settled by then

    def test_costs_make_flat_market_lose(self):
        closes = [100] * 40
        res = bt.simulate(EnterOn(set(range(1, 35)), max_hold=1), {"AAA": bars_from(closes)},
                          settings=self.cfg, costs=CostModel())
        self.assertGreater(res["metrics"]["trades"], 5)
        self.assertLess(res["metrics"]["net_pnl_pkr"], 0)
        self.assertTrue(all(t["net_pnl"] < 0 for t in res["trades"]))


class TestResearch(unittest.TestCase):

    def test_verdicts(self):
        cfg = dict(bt.DEFAULT_SETTINGS, min_trades_for_verdict=10)
        bh = {"total_return_pct": 10.0, "sharpe": 1.0}
        base = {"trades": 50, "expectancy_pkr": 100.0, "profit_factor": 1.5, "total_return_pct": 12.0, "sharpe": 0.5}
        self.assertEqual(bt.verdict(base, bh, cfg)[0], "PASS")
        self.assertEqual(bt.verdict(dict(base, trades=5), bh, cfg)[0], "INSUFFICIENT_DATA")
        self.assertEqual(bt.verdict(dict(base, expectancy_pkr=-1), bh, cfg)[0], "FAIL")
        self.assertEqual(bt.verdict(dict(base, profit_factor=1.05), bh, cfg)[0], "FAIL")
        self.assertEqual(bt.verdict(dict(base, total_return_pct=5, sharpe=0.9), bh, cfg)[0], "FAIL")

    def test_end_to_end_report_on_random_walk_universe(self):
        rng = random.Random(7)
        data = {}
        for k in range(12):
            px, closes, opens, = 50.0 + 10 * k, [], []
            for _ in range(320):
                o = px * (1 + rng.gauss(0, 0.004))
                px = max(1.0, o * (1 + rng.gauss(0.0003, 0.02)))
                opens.append(round(o, 2))
                closes.append(round(px, 2))
            data[f"S{k:02d}"] = bars_from(closes, opens, vol=rng.randint(50000, 500000))
        report = bt.run_research(data, settings={"min_trades_for_verdict": 20})
        self.assertTrue(report["success"])
        names = [s["name"] for s in report["strategies"]]
        self.assertEqual(set(names), set(bt.STRATEGIES))
        for s in report["strategies"]:
            self.assertNotEqual(s["verdict"], "ERROR", s.get("reason"))
            self.assertIn(s["verdict"], ("PASS", "FAIL", "INSUFFICIENT_DATA"))
            self.assertIn("out_of_sample", s)
        self.assertIn("out_of_sample", report["baselines"])


if __name__ == "__main__":
    unittest.main()
