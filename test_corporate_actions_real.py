#!/usr/bin/env python3
"""Corporate actions come from DPS payouts (never sample data)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import psx_corporate_actions as ca


def row(text, ex="14/10/2026", sym="ABC", closure="16/10/2026 - 23/10/2026"):
    return {"symbol": sym, "dividendAmount": text, "exDividendDate": ex, "bookClosure": closure}


class TestPayoutParsing(unittest.TestCase):

    def test_formats(self):
        cases = {
            "50%Final Cash": ("DIVIDEND", 5.0, 0.0),
            "Rs. 8.25 Cash": ("DIVIDEND", 8.25, 0.0),
            "20% Bonus": ("BONUS", 0.0, 20.0),
            "30%1st Int. Cash - 50% Bonus": ("DIVIDEND", 3.0, 50.0),
            "20% Final Cash - 20% Final Cash": ("DIVIDEND", 2.0, 0.0),
            "802nd Int. Cash": ("DIVIDEND", None, 0.0),   # garbled on DPS → unknown, not 0
        }
        for text, want in cases.items():
            a = ca.parse_payout_entry(row(text))
            self.assertEqual((a["action_type"], a["payout_pkr"], a["bonus_pct"]), want, text)
        a = ca.parse_payout_entry(row("50%Final Cash"))
        self.assertEqual((a["ex_date"], a["book_closure_start"], a["book_closure_end"]),
                         ("2026-10-14", "2026-10-16", "2026-10-23"))
        self.assertIsNone(ca.parse_payout_entry(row("20.04% Right")))
        self.assertIsNone(ca.parse_payout_entry(row("50%Final Cash", ex="")))

    def test_load_actions_uses_payouts_and_never_samples(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "payouts.json"
            missing = Path(d) / "none.json"
            with mock.patch.object(ca, "PAYOUTS_PATH", missing), mock.patch.object(ca, "DB_PATH", missing):
                self.assertEqual(ca._load_actions(), [])
            p.write_text(json.dumps({"data": {"dividendCalendar": [row("50%Final Cash"), row("20.04% Right", sym="R")]}}))
            with mock.patch.object(ca, "PAYOUTS_PATH", p), mock.patch.object(ca, "DB_PATH", missing):
                self.assertEqual([a["symbol"] for a in ca._load_actions()], ["ABC"])


if __name__ == "__main__":
    unittest.main()
