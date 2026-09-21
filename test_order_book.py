import unittest
from order_book_engine import (
    compute_depth_imbalance,
    compute_estimated_order_pressure,
    detect_spoofed_walls,
    analyze_order_book,
)


class TestOrderBookEngine(unittest.TestCase):
    def test_depth_imbalance_symmetric(self):
        bids = [{"price": 100.0, "volume": 10000}, {"price": 99.5, "volume": 5000}]
        asks = [{"price": 100.5, "volume": 10000}, {"price": 101.0, "volume": 5000}]
        imbalance = compute_depth_imbalance(bids, asks)
        self.assertEqual(imbalance, 0.0)

    def test_depth_imbalance_extremes(self):
        bids = [{"price": 100.0, "volume": 20000}]
        asks = []
        self.assertEqual(compute_depth_imbalance(bids, asks), 1.0)

        bids = []
        asks = [{"price": 101.0, "volume": 15000}]
        self.assertEqual(compute_depth_imbalance(bids, asks), -1.0)

        # 3:1 ratio
        bids = [{"price": 100.0, "volume": 30000}]
        asks = [{"price": 101.0, "volume": 10000}]
        # (30000 - 10000) / 40000 = 0.5
        self.assertEqual(compute_depth_imbalance(bids, asks), 0.5)

    def test_weighted_depth_imbalance(self):
        # Heavy volume on ask level 1 vs heavy volume on bid level 5
        bids = [
            {"price": 100.0, "volume": 1000},
            {"price": 99.0, "volume": 1000},
            {"price": 98.0, "volume": 1000},
            {"price": 97.0, "volume": 1000},
            {"price": 96.0, "volume": 20000},  # Deep level 5
        ]
        asks = [
            {"price": 101.0, "volume": 20000},  # Front level 1
            {"price": 102.0, "volume": 1000},
            {"price": 103.0, "volume": 1000},
            {"price": 104.0, "volume": 1000},
            {"price": 105.0, "volume": 1000},
        ]
        unweighted = compute_depth_imbalance(bids, asks, weighted=False)
        weighted = compute_depth_imbalance(bids, asks, weighted=True)
        # Total volumes are identical (24,000 vs 24,000), so unweighted is 0.0
        self.assertEqual(unweighted, 0.0)
        # But ask level 1 has much higher weight than bid level 5, so weighted is negative (sell bias)
        self.assertLess(weighted, -0.2)

    def test_spoofed_wall_detection(self):
        # Average volume across levels is ~5,000.
        # Snapshot 0: normal
        snap0 = {
            "bids": [{"price": 100.0, "volume": 5000}, {"price": 99.0, "volume": 5000}],
            "asks": [{"price": 101.0, "volume": 5000}, {"price": 102.0, "volume": 5000}],
        }
        # Snapshot 1: Massive 60,000 ask wall appears at 101.5 (12x average)
        snap1 = {
            "bids": [{"price": 100.0, "volume": 5000}, {"price": 99.0, "volume": 5000}],
            "asks": [{"price": 101.0, "volume": 5000}, {"price": 101.5, "volume": 60000}],
        }
        # Snapshot 2: Ask wall vanishes!
        snap2 = {
            "bids": [{"price": 100.0, "volume": 5000}, {"price": 99.0, "volume": 5000}],
            "asks": [{"price": 101.0, "volume": 5000}, {"price": 102.0, "volume": 5000}],
        }
        # Snapshot 3: Normal book continues
        snap3 = {
            "bids": [{"price": 100.0, "volume": 5000}, {"price": 99.0, "volume": 5000}],
            "asks": [{"price": 101.0, "volume": 5000}, {"price": 102.0, "volume": 5000}],
        }

        analysis = detect_spoofed_walls([snap0, snap1, snap2, snap3], wall_multiple_threshold=3.0)
        self.assertEqual(len(analysis["spoof_alerts"]), 1)
        alert = analysis["spoof_alerts"][0]
        self.assertEqual(alert["side"], "ASK")
        self.assertEqual(alert["price"], 101.5)
        self.assertEqual(alert["volume"], 60000)
        self.assertEqual(alert["type"], "potential_spoof_wall")

    def test_persistent_wall_detection(self):
        # A large bid wall at 99.0 that remains across snapshots
        snaps = []
        for i in range(4):
            snaps.append({
                "bids": [{"price": 100.0, "volume": 5000}, {"price": 99.0, "volume": 50000}],
                "asks": [{"price": 101.0, "volume": 5000}, {"price": 102.0, "volume": 5000}],
            })
        analysis = detect_spoofed_walls(snaps, wall_multiple_threshold=3.0)
        self.assertEqual(len(analysis["spoof_alerts"]), 0)
        self.assertEqual(len(analysis["persistent_walls"]), 1)
        wall = analysis["persistent_walls"][0]
        self.assertEqual(wall["side"], "BID")
        self.assertEqual(wall["price"], 99.0)
        self.assertEqual(wall["status"], "persistent")

    def test_analyze_order_book_fallback_estimated(self):
        res = analyze_order_book("OGDC", real_depth=None, price_change_pct=2.5)
        self.assertEqual(res["source"], "estimated")
        self.assertFalse(res["is_real_depth"])
        self.assertFalse(res["public_dps_has_depth"])
        self.assertEqual(res["label"], "Estimated pressure (from price change)")
        self.assertEqual(res["buy_ratio"], 62)
        self.assertEqual(res["sell_ratio"], 38)
        self.assertEqual(res["imbalance"], 0.24)

    def test_analyze_order_book_real_promoted(self):
        real_depth = {
            "bids": [{"price": 318.0, "volume": 40000}, {"price": 317.5, "volume": 20000}],
            "asks": [{"price": 318.5, "volume": 10000}, {"price": 319.0, "volume": 10000}],
        }
        res = analyze_order_book("OGDC", real_depth=real_depth, price_change_pct=0.5)
        self.assertEqual(res["source"], "real_l2")
        self.assertTrue(res["is_real_depth"])
        self.assertEqual(res["label"], "Market Depth Imbalance (Real L2 Top-5)")
        # (60000 - 20000) / 80000 = +0.50
        self.assertEqual(res["imbalance"], 0.5)
        # buy_ratio = (0.5 + 1)/2 * 100 = 75
        self.assertEqual(res["buy_ratio"], 75)
        self.assertEqual(res["sell_ratio"], 25)
        self.assertEqual(res["total_bid_depth"], 60000)
        self.assertEqual(res["total_ask_depth"], 20000)


if __name__ == "__main__":
    unittest.main()
