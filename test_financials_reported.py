#!/usr/bin/env python3
"""Financial statements endpoint returns only reported DPS figures — nothing estimated."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import psx_fundamentals as pf
import server


class TestReportedFinancials(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = pf.FundamentalsStore(Path(self.tmp.name) / "f.db")
        self.store.save("ABC", [
            {"period": "2025", "sales": 1200.0, "eps": 4.0, "is_quarterly": False},
            {"period": "2024", "sales": 1000.0, "eps": 2.0, "is_quarterly": False},
            {"period": "Q1 2026", "sales": 330.0, "eps": 1.1, "is_quarterly": True},
            {"period": "Q4 2025", "sales": 300.0, "eps": 1.0, "is_quarterly": True},
            {"period": "Q1 2025", "sales": 300.0, "eps": -0.5, "is_quarterly": True},
        ])
        stocks = [{"symbol": "ABC", "name": "Abc Ltd", "sector": "Cement", "price": 40.0, "mcap": 1e9, "pe": 10}]
        self.patches = [mock.patch.object(pf, "get_store", return_value=self.store),
                        mock.patch.object(server, "fetch_stock_data", return_value=(stocks, 0))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_reported_figures_only(self):
        with mock.patch.object(pf, "refresh_fundamentals", side_effect=AssertionError("data already cached")):
            fin = server.fetch_financial_statements("abc")
        self.assertFalse(fin["statementsAvailable"])
        for k in ("balanceSheet", "incomeStatement", "cashFlowStatement"):
            self.assertNotIn(k, fin)
        self.assertEqual([a["period"] for a in fin["annual"]], ["2025", "2024"])
        self.assertEqual(fin["annual"][0]["sales_growth_pct"], 20.0)
        self.assertEqual(fin["annual"][0]["eps_growth_pct"], 100.0)
        self.assertEqual([q["period"] for q in fin["quarterly"]], ["Q1 2026", "Q4 2025", "Q1 2025"])
        q1 = fin["quarterly"][0]  # compared with Q1 2025, not the previous quarter
        self.assertEqual(q1["sales_growth_pct"], 10.0)
        self.assertNotIn("eps_growth_pct", q1)
        self.assertEqual(q1["eps_change"], "turnaround")
        self.assertNotIn("sales_growth_pct", fin["quarterly"][1])  # no Q4 2024 to compare
        self.assertEqual(fin["earningsYieldPct"], 10.0)

    def test_unknown_symbol_is_not_replaced_by_another_company(self):
        self.assertIsNone(server.fetch_financial_statements("ZZZZ"))
        self.assertIsNone(server.fetch_financial_statements("AB"))  # no substring matching


if __name__ == "__main__":
    unittest.main()
