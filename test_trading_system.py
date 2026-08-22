#!/usr/bin/env python3
"""
Unit & Integration Tests for PSX Trading System:
Risk Engine, Paper Broker, and Active Position Monitor
"""

import unittest
import os
from pathlib import Path
from psx_risk_engine import RiskEngine
from psx_paper_broker import PaperTradingBrokerAdapter
from psx_position_monitor import PositionMonitor

TEST_ACCOUNT_FILE = Path(__file__).parent / "cache" / "test_paper_account.json"


class TestRiskEngine(unittest.TestCase):
    def setUp(self):
        self.risk = RiskEngine()

    def test_position_sizing_1pct_risk(self):
        # 1,000,000 equity, Entry: 100, SL: 95 (5 PKR risk/share)
        # 1% risk budget = 10,000 PKR -> 10,000 / 5 = 2,000 shares
        # 25% max capital budget = 250,000 PKR -> 250,000 / 100 = 2,500 shares
        # Stricter is 2,000 shares
        res = self.risk.calculate_position_size(1000000.0, 100.0, 95.0)
        self.assertEqual(res["shares"], 2000)
        self.assertEqual(res["risk_amount"], 10000.0)
        self.assertEqual(res["capital_required"], 200000.0)

    def test_kill_switch(self):
        self.risk.trigger_kill_switch("Test emergency")
        self.assertTrue(self.risk.is_kill_switch_active)
        allowed, reason, _ = self.risk.validate_order_proposal("SYS", "Tech", 100.0, 95.0, {"equity": 1000000, "cash": 1000000})
        self.assertFalse(allowed)
        self.assertIn("Kill Switch", reason)
        
        self.risk.reset_kill_switch()
        self.assertFalse(self.risk.is_kill_switch_active)

    def test_max_positions_limit(self):
        account = {
            "equity": 1000000.0,
            "cash": 500000.0,
            "positions": [{"symbol": "A"}, {"symbol": "B"}, {"symbol": "C"}]
        }
        allowed, reason, _ = self.risk.validate_order_proposal("D", "Tech", 100.0, 95.0, account)
        self.assertFalse(allowed)
        self.assertIn("Max concurrent positions", reason)


class TestPaperBrokerAndMonitor(unittest.TestCase):
    def setUp(self):
        if TEST_ACCOUNT_FILE.exists():
            TEST_ACCOUNT_FILE.unlink()
        self.broker = PaperTradingBrokerAdapter(filepath=TEST_ACCOUNT_FILE)
        self.risk = RiskEngine()
        self.monitor = PositionMonitor(broker=self.broker, risk=self.risk)

    def tearDown(self):
        if TEST_ACCOUNT_FILE.exists():
            TEST_ACCOUNT_FILE.unlink()

    def test_buy_order_and_commission(self):
        res = self.broker.place_buy_order(
            symbol="SYS", name="Systems Limited", sector="Tech",
            shares=1000, price=100.0, stop_loss=95.0, take_profit_1=108.0, take_profit_2=115.0
        )
        self.assertTrue(res["success"])
        acct = self.broker.get_account_data({"SYS": 100.0})
        self.assertEqual(acct["positions_count"], 1)
        self.assertLess(acct["cash"], 900000.0) # Cost + commission deducted

    def test_stop_loss_trigger(self):
        self.broker.place_buy_order(
            symbol="SYS", name="Systems Limited", sector="Tech",
            shares=1000, price=100.0, stop_loss=95.0, take_profit_1=108.0, take_profit_2=115.0
        )
        # Price drops to 94.0 -> SL should trigger
        actions = self.monitor.process_price_ticks({"SYS": 94.0})
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "STOP_LOSS_EXIT")
        
        acct = self.broker.get_account_data()
        self.assertEqual(acct["positions_count"], 0)

    def test_tp1_trim_and_breakeven(self):
        self.broker.place_buy_order(
            symbol="SYS", name="Systems Limited", sector="Tech",
            shares=1000, price=100.0, stop_loss=95.0, take_profit_1=108.0, take_profit_2=115.0
        )
        # Price hits TP1 (109.0)
        actions = self.monitor.process_price_ticks({"SYS": 109.0})
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "TP1_TRIM_AND_BREAKEVEN")
        
        # Verify 500 shares remaining and stop loss moved to breakeven
        pos = self.broker.data["positions"]["SYS"]
        self.assertEqual(pos["shares"], 500)
        self.assertTrue(pos["tp1_hit"])
        self.assertGreaterEqual(pos["stop_loss"], 100.0)


if __name__ == "__main__":
    unittest.main()
