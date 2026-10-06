#!/usr/bin/env python3
"""Offline tests for psx_fundamentals (point-in-time data, QVM scoring) and the qvm_monthly strategy."""

import tempfile
import unittest
from pathlib import Path

import psx_backtester as bt
import psx_fundamentals as pf
from psx_costs import CostModel
from test_backtester import bars_from


def annual(*rows):
    """rows: (fy, sales, eps) newest first."""
    return [{"fy": fy, "sales": s, "eps": e} for fy, s, e in rows]


class TestStore(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = pf.FundamentalsStore(Path(self.tmp.name) / "f.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_point_in_time_availability(self):
        self.store.save("abc", [{"period": "2024", "sales": 100, "eps": 2.0, "is_quarterly": False},
                                {"period": "2025", "sales": 120, "eps": 2.5, "is_quarterly": False},
                                {"period": "Q1 2026", "sales": 40, "eps": 0.9, "is_quarterly": True}])
        self.assertEqual([r["fy"] for r in self.store.annual("ABC")], [2025, 2024])
        self.assertEqual([r["fy"] for r in self.store.as_of("ABC", "2026-04-29")], [2024])
        self.assertEqual([r["fy"] for r in self.store.as_of("ABC", "2026-04-30")], [2025, 2024])
        self.assertEqual(self.store.as_of("ABC", "2025-01-01"), [])

    def test_refresh_parses_dps_company_page(self):
        html = '''<div id="financialTab"><div class="tabs__panel" data-name="Annual"><table>
            <tr><th>Item</th><th class="right">2025</th><th class="right">2024</th></tr>
            <tr><td>Sales</td><td>1,200</td><td>1,000</td></tr>
            <tr><td>EPS</td><td>3.10</td><td>(0.50)</td></tr></table></div>
          </div>'''
        res = pf.refresh_fundamentals(["XYZ"], lambda url, **kw: html, pause_s=0, store=self.store)
        self.assertEqual(res["failed"], 0)
        self.assertEqual(self.store.annual("XYZ"), annual((2025, 1200.0, 3.1), (2024, 1000.0, -0.5)))


class TestScoring(unittest.TestCase):

    def test_raw_factors(self):
        f = pf.raw_factors(annual((2025, 133.1, 4.0), (2024, 121, 3.0), (2023, 110, 2.0), (2022, 100, 1.0)), 40.0, 0.25)
        self.assertAlmostEqual(f["value_earnings_yield"], 0.1)
        self.assertEqual(f["quality_consistency"], 1.0)
        self.assertAlmostEqual(f["growth_sales"], 0.1, places=6)
        self.assertAlmostEqual(f["growth_eps"], 1 / 3)
        self.assertEqual(f["momentum_12_1"], 0.25)

    def _snap(self, eps, price=50.0, mom=0.1, sector="Cement", adv=1e7, years=4):
        return {"price": price, "avg_daily_value": adv, "mom_12_1": mom, "sector": sector,
                "annual": annual(*[(2025 - k, 100.0 * (1.1 ** -k), eps * (0.9 ** k)) for k in range(years)])}

    def test_eligibility_never_rewards_missing_data(self):
        snaps = {"GOOD": self._snap(5.0), "LOSS": self._snap(-1.0), "THIN": self._snap(5.0, years=2),
                 "ILLIQ": self._snap(5.0, adv=1000), "NEW": self._snap(5.0, mom=None), "PENNY": self._snap(0.5, price=2)}
        ranked = pf.score_universe(snaps)
        self.assertEqual([r["symbol"] for r in ranked], ["GOOD"])

    def test_stale_results_excluded(self):
        snaps = {"FRESH": self._snap(5.0), "OLD": self._snap(5.0)}
        snaps["OLD"]["annual"] = [dict(r, fy=r["fy"] - 4) for r in snaps["OLD"]["annual"]]  # latest FY2021
        self.assertEqual([r["symbol"] for r in pf.score_universe(snaps, as_of="2026-06-01")], ["FRESH"])

    def test_cheaper_and_stronger_ranks_higher(self):
        snaps = {f"S{i}": self._snap(eps=2.0 + i, mom=0.05 * i) for i in range(6)}
        ranked = pf.score_universe(snaps)
        self.assertEqual(ranked[0]["symbol"], "S5")
        self.assertEqual(ranked[-1]["symbol"], "S0")
        self.assertTrue(all(0 <= r["score"] <= 100 for r in ranked))
        self.assertEqual(ranked[0]["coverage_pct"], 100)

    def test_value_is_sector_relative_for_big_sectors(self):
        cfg = pf.load_config()
        snaps = {f"B{i}": self._snap(eps=10 + i, sector="Banks") for i in range(5)}
        snaps.update({f"T{i}": self._snap(eps=1 + i * 0.1, sector="Tech") for i in range(5)})
        ranked = {r["symbol"]: r for r in pf.score_universe(snaps, cfg)}
        # cheapest tech stock gets the top value percentile even though banks have far higher E/P
        self.assertEqual(ranked["T4"]["percentiles"]["value_earnings_yield"], 100.0)
        self.assertEqual(ranked["B4"]["percentiles"]["value_earnings_yield"], 100.0)

    def test_sector_cap(self):
        cfg = pf.load_config()
        ranked = [{"symbol": f"S{i}", "sector": "Banks" if i < 6 else "Cement", "score": 100 - i} for i in range(12)]
        picked = pf.select_portfolio(ranked, cfg)
        self.assertEqual(sum(1 for p in picked if p["sector"] == "Banks"), 3)
        self.assertLessEqual(len(picked), cfg["portfolio"]["top_n"])


class TestQvmStrategy(unittest.TestCase):

    def test_monthly_rotation_without_lookahead(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = pf.FundamentalsStore(Path(tmp.name) / "f.db")
        data, sectors = {}, {}
        for k in range(8):
            sym = f"S{k}"
            closes = [50.0 * (1 + 0.0004 * (k + 1)) ** d for d in range(700)]
            data[sym] = bars_from(closes, vol=500000)
            sectors[sym] = "Sector%d" % (k % 4)
            store.save(sym, [{"period": str(fy), "sales": 100.0 * 1.05 ** (fy - 2020), "eps": 3.0 + 0.2 * k,
                              "is_quarterly": False} for fy in range(2020, 2025)])
        # "LATE" has only FY2025 results that look great — must not be used before 2026-04-30
        data["LATE"] = bars_from([50.0] * 700, vol=500000)
        sectors["LATE"] = "Sector9"
        store.save("LATE", [{"period": str(fy), "sales": 100.0, "eps": 9.0 if fy == 2025 else -1.0,
                             "is_quarterly": False} for fy in range(2021, 2026)])
        strat = bt.QualityValueMomentum(store=store, sectors=sectors)
        res = bt.simulate(strat, data, settings={"capital_pkr": 500000.0, "min_trade_value_pkr": 1000.0},
                          costs=CostModel({"slippage": {"base_bps_per_side": 0}}), close_at_end=False)
        held_dates = [t["entry_date"] for t in res["trades"] + res["open_positions"] if t["symbol"] == "LATE"]
        self.assertTrue(all(d >= "2026-04-30" for d in held_dates), held_dates)
        self.assertTrue(res["open_positions"], "model portfolio holds positions")
        self.assertLessEqual(len(res["open_positions"]), strat.top_n)


if __name__ == "__main__":
    unittest.main()
