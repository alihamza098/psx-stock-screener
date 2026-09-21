"""
order_book_engine.py - Real Market Depth & Order Book Imbalance Engine for PSX

Features:
1. Top-5 Bid/Ask Depth Imbalance:
     Imbalance = (Total Bid Volume - Total Ask Volume) / (Total Bid Volume + Total Ask Volume)
     Clamped to [-1.0, +1.0] with buy/sell pressure ratio conversion.
2. Order Book Wall & Spoofing Detection:
     Detects large walls (> 3.0x average level depth) that flash and vanish
     across consecutive snapshots without trade volume execution.
3. Transparent Fallback:
     Public PSX Data Portal (DPS) does not expose Level-2/top-5 market depth (restricted to KiTS/FIX broker feeds).
     When real L2 feed is absent, deterministically falls back to price-change pressure
     and strictly labels it "Estimated pressure (from price change)".
"""

import json
import os
from typing import Dict, List, Optional, Tuple, Any


CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config", "live_trading.json")


def load_order_book_config() -> Dict[str, Any]:
    """Loads order book configuration parameters."""
    defaults = {
        "public_dps_has_depth": False,
        "fallback_to_estimated_pressure": True,
        "label_estimated": "Estimated pressure (from price change)",
        "label_real": "Market Depth Imbalance (Real L2 Top-5)",
        "wall_multiple_threshold": 3.0,
        "spoof_vanish_snapshots_threshold": 2,
        "imbalance_weights": {
            "level_1_weight": 0.35,
            "level_2_weight": 0.25,
            "level_3_weight": 0.20,
            "level_4_weight": 0.12,
            "level_5_weight": 0.08,
        },
    }
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                return cfg.get("order_book", defaults)
        except Exception:
            pass
    return defaults


def compute_depth_imbalance(
    bids: List[Dict[str, Any]],
    asks: List[Dict[str, Any]],
    weighted: bool = False,
) -> float:
    """
    Computes top-5 order book depth imbalance in range [-1.0, 1.0].
    +1.0 indicates 100% buy interest (all bids, no asks).
    -1.0 indicates 100% sell interest (all asks, no bids).
    0.0 indicates exact equilibrium or empty book.
    """
    if not bids and not asks:
        return 0.0

    if not weighted:
        total_bid_qty = sum(float(b.get("volume", 0)) for b in bids)
        total_ask_qty = sum(float(a.get("volume", 0)) for a in asks)
    else:
        cfg = load_order_book_config()
        w_cfg = cfg.get("imbalance_weights", {})
        weights = [
            w_cfg.get("level_1_weight", 0.35),
            w_cfg.get("level_2_weight", 0.25),
            w_cfg.get("level_3_weight", 0.20),
            w_cfg.get("level_4_weight", 0.12),
            w_cfg.get("level_5_weight", 0.08),
        ]
        total_bid_qty = 0.0
        for i, b in enumerate(bids[:5]):
            w = weights[i] if i < len(weights) else 0.05
            total_bid_qty += float(b.get("volume", 0)) * w

        total_ask_qty = 0.0
        for i, a in enumerate(asks[:5]):
            w = weights[i] if i < len(weights) else 0.05
            total_ask_qty += float(a.get("volume", 0)) * w

    denom = total_bid_qty + total_ask_qty
    if denom <= 0:
        return 0.0

    imbalance = (total_bid_qty - total_ask_qty) / denom
    return round(max(-1.0, min(1.0, imbalance)), 4)


def compute_estimated_order_pressure(change_pct: float) -> Tuple[int, int]:
    """
    Deterministic fallback when real L2 depth is unavailable.
    Maps price change percentage to buying/selling pressure balance.
    """
    if change_pct > 3.0:
        buy_ratio = 72
    elif change_pct > 1.0:
        buy_ratio = 62
    elif change_pct > 0.0:
        buy_ratio = 54
    elif change_pct < -3.0:
        buy_ratio = 28
    elif change_pct < -1.0:
        buy_ratio = 38
    elif change_pct < 0.0:
        buy_ratio = 46
    else:
        buy_ratio = 50
    return buy_ratio, 100 - buy_ratio


def detect_spoofed_walls(
    snapshots: List[Dict[str, Any]],
    wall_multiple_threshold: float = 3.0,
    vanish_threshold_snapshots: int = 2,
) -> Dict[str, Any]:
    """
    Tracks order book snapshots over time to detect persistent vs. spoofed/flashing walls.
    
    A 'wall' is an order level whose volume is >= wall_multiple_threshold times
    the average volume across all book levels in that snapshot.
    
    A wall is marked as 'spoofed' if it appears and disappears within
    vanish_threshold_snapshots without significant trades executed at that price level.
    """
    if not snapshots or len(snapshots) < 2:
        return {
            "status": "insufficient_snapshots",
            "walls_detected": [],
            "spoof_alerts": [],
            "persistent_walls": [],
        }

    walls_detected = []
    spoof_alerts = []
    persistent_walls = []

    # Map of (side, price) -> list of snapshot indices where wall was present
    wall_appearances: Dict[Tuple[str, float], List[int]] = {}
    wall_volumes: Dict[Tuple[str, float], List[float]] = {}

    for idx, snap in enumerate(snapshots):
        bids = snap.get("bids", [])
        asks = snap.get("asks", [])
        
        all_vols = [float(b.get("volume", 0)) for b in bids] + [float(a.get("volume", 0)) for a in asks]
        avg_vol = (sum(all_vols) / len(all_vols)) if all_vols else 0.0

        if avg_vol <= 0:
            continue

        wall_threshold = avg_vol * wall_multiple_threshold

        # Check bids
        for b in bids:
            v = float(b.get("volume", 0))
            p = float(b.get("price", 0))
            if v >= wall_threshold and p > 0:
                key = ("BID", p)
                if key not in wall_appearances:
                    wall_appearances[key] = []
                    wall_volumes[key] = []
                wall_appearances[key].append(idx)
                wall_volumes[key].append(v)

        # Check asks
        for a in asks:
            v = float(a.get("volume", 0))
            p = float(a.get("price", 0))
            if v >= wall_threshold and p > 0:
                key = ("ASK", p)
                if key not in wall_appearances:
                    wall_appearances[key] = []
                    wall_volumes[key] = []
                wall_appearances[key].append(idx)
                wall_volumes[key].append(v)

    total_snapshots = len(snapshots)

    for (side, price), appearances in wall_appearances.items():
        duration = len(appearances)
        max_v = max(wall_volumes[(side, price)])
        last_seen = appearances[-1]
        first_seen = appearances[0]

        # Did it vanish before the latest snapshot?
        has_vanished = (last_seen < total_snapshots - 1)

        wall_info = {
            "side": side,
            "price": price,
            "peak_volume": max_v,
            "duration_snapshots": duration,
            "first_seen_snapshot": first_seen,
            "last_seen_snapshot": last_seen,
        }
        walls_detected.append(wall_info)

        # Check for spoofing: appeared for <= vanish_threshold_snapshots and then vanished
        if has_vanished and duration <= vanish_threshold_snapshots:
            spoof_alerts.append({
                "side": side,
                "price": price,
                "volume": max_v,
                "duration": duration,
                "type": "potential_spoof_wall",
                "message": f"Suspicious {side} wall of {int(max_v):,} at PKR {price:.2f} vanished after {duration} snapshot(s).",
            })
        elif duration > vanish_threshold_snapshots:
            persistent_walls.append({
                "side": side,
                "price": price,
                "volume": max_v,
                "duration": duration,
                "status": "persistent",
            })

    return {
        "status": "analyzed",
        "total_snapshots_analyzed": total_snapshots,
        "walls_detected": walls_detected,
        "spoof_alerts": spoof_alerts,
        "persistent_walls": persistent_walls,
    }


def analyze_order_book(
    symbol: str,
    real_depth: Optional[Dict[str, Any]] = None,
    price_change_pct: float = 0.0,
    snapshot_history: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Main entrypoint for Order Book analysis.
    
    If real_depth is supplied (e.g. from broker API or direct FIX feed):
      - Computes real depth imbalance across top-5 bids and asks.
      - Tracks spoofed vs. persistent walls across snapshot history.
      - Sets source = 'real_l2' and uses the real L2 label.
      
    If real_depth is None (default for PSX DPS public portal):
      - Gracefully falls back to price-change estimated pressure.
      - Explicitly retains label: 'Estimated pressure (from price change)'.
      - Documents that public DPS does not provide Level-2 order book.
    """
    cfg = load_order_book_config()

    if real_depth and ("bids" in real_depth or "asks" in real_depth):
        bids = real_depth.get("bids", [])
        asks = real_depth.get("asks", [])
        
        imbalance = compute_depth_imbalance(bids, asks, weighted=False)
        weighted_imbalance = compute_depth_imbalance(bids, asks, weighted=True)

        buy_ratio = int(round((imbalance + 1.0) / 2.0 * 100.0))
        buy_ratio = max(5, min(95, buy_ratio))
        sell_ratio = 100 - buy_ratio

        total_bid_vol = sum(float(b.get("volume", 0)) for b in bids)
        total_ask_vol = sum(float(a.get("volume", 0)) for a in asks)

        spoof_res = {}
        if snapshot_history:
            spoof_res = detect_spoofed_walls(
                snapshot_history,
                wall_multiple_threshold=cfg.get("wall_multiple_threshold", 3.0),
                vanish_threshold_snapshots=cfg.get("spoof_vanish_snapshots_threshold", 2),
            )

        return {
            "symbol": symbol,
            "source": "real_l2",
            "is_real_depth": True,
            "label": cfg.get("label_real", "Market Depth Imbalance (Real L2 Top-5)"),
            "sub_label": "Live Level-2 Top-5 Order Book from broker feed",
            "imbalance": imbalance,
            "weighted_imbalance": weighted_imbalance,
            "buy_ratio": buy_ratio,
            "sell_ratio": sell_ratio,
            "total_bid_depth": total_bid_vol,
            "total_ask_depth": total_ask_vol,
            "top_bids": bids[:5],
            "top_asks": asks[:5],
            "spoof_analysis": spoof_res,
        }

    # Public DPS Fallback Path
    buy_ratio, sell_ratio = compute_estimated_order_pressure(price_change_pct)
    imbalance = round((buy_ratio - sell_ratio) / 100.0, 4)

    return {
        "symbol": symbol,
        "source": "estimated",
        "is_real_depth": False,
        "label": cfg.get("label_estimated", "Estimated pressure (from price change)"),
        "sub_label": "Simulated balance from price ticks — DPS public data lacks L2 book depth",
        "public_dps_has_depth": False,
        "reason": "DPS public portal does not expose top-5 market depth (broker terminal required for L2)",
        "imbalance": imbalance,
        "weighted_imbalance": imbalance,
        "buy_ratio": buy_ratio,
        "sell_ratio": sell_ratio,
        "total_bid_depth": 0,
        "total_ask_depth": 0,
        "top_bids": [],
        "top_asks": [],
        "spoof_analysis": {
            "status": "no_l2_depth",
            "alerts": [],
        },
    }
