#!/usr/bin/env python3
"""
Multibagger Pattern Finder — Scoring Engine
============================================
Isolated, fully unit-testable scoring algorithm identifying low-priced PSX stocks (<20 PKR)
sharing setup characteristics of past 10x-70x runners.

Formula:
  Score = 25 * has_name_or_sector_change_last_12mo   (0 or 1)
        + 20 * has_capital_increase_last_12mo        (0 or 1)
        + 20 * volume_zscore_normalized              (0–1, capped at 3 std dev)
        + 15 * (consecutive_qoq_growth_quarters / 3) (0–1)
        + 10 * (1 if price <= price_ceiling else 0)
        + 10 * sector_momentum_score                 (0–1)
  Total = 0 to 100 points.

Notice: This model produces setup discovery scores and pattern matching reasons only.
Never issues buy or sell recommendations.
"""

import math
from typing import Dict, Any, List, Optional, Tuple


def calculate_volume_zscore(volumes: List[float]) -> Tuple[float, float, float]:
    """
    Compute volume Z-score comparing the 20-day average to the 90-day baseline.
    Returns (z_score_normalized_0_to_1, raw_z_score, ratio_20d_vs_90d).
    
    volume_zscore_normalized:
      (avg_20d - avg_90d) / std_90d, clipped to [0, 3], scaled to [0, 1].
    """
    if not volumes or len(volumes) < 20:
        return 0.0, 0.0, 1.0

    # Ensure clean float values
    clean_vols = [max(0.0, float(v or 0)) for v in volumes]
    n = len(clean_vols)

    # 20-day window
    v20_slice = clean_vols[-20:]
    avg_20d = sum(v20_slice) / len(v20_slice)

    # If we have prior history before current 20d, use that as the baseline
    if n >= 40:
        baseline_slice = clean_vols[-110:-20] if n >= 110 else clean_vols[:-20]
    else:
        baseline_slice = clean_vols

    avg_90d = sum(baseline_slice) / len(baseline_slice)
    ratio = (avg_20d / avg_90d) if avg_90d > 0 else 1.0

    if len(baseline_slice) > 1:
        variance = sum((x - avg_90d) ** 2 for x in baseline_slice) / (len(baseline_slice) - 1)
        std_90d = math.sqrt(variance)
    else:
        std_90d = 0.0

    if std_90d <= 0.0:
        if avg_20d > avg_90d:
            raw_z = 3.0
        else:
            raw_z = 0.0
    else:
        raw_z = (avg_20d - avg_90d) / std_90d

    # Clip to [0, 3] and scale to [0, 1]
    clipped_z = max(0.0, min(3.0, raw_z))
    norm_z = clipped_z / 3.0

    return round(norm_z, 4), round(raw_z, 2), round(ratio, 2)


def calculate_sector_momentum(sector_return_30d: float, all_sector_returns_30d: List[float]) -> float:
    """
    Normalize the stock's sector index 30-day return against all sector indices into [0, 1].
    If all returns are identical or list is empty, returns 0.5 (neutral).
    """
    if not all_sector_returns_30d:
        return 0.5

    clean_rets = [float(r) for r in all_sector_returns_30d if r is not None]
    if not clean_rets:
        return 0.5

    min_ret = min(clean_rets)
    max_ret = max(clean_rets)

    if math.isclose(min_ret, max_ret, abs_tol=1e-6):
        return 0.5

    norm = (sector_return_30d - min_ret) / (max_ret - min_ret)
    return round(max(0.0, min(1.0, norm)), 4)


def calculate_multibagger_score(
    price: float,
    has_name_or_sector_change: bool,
    has_capital_increase: bool,
    volumes: List[float],
    consecutive_qoq_growth: int,
    sector_return_30d: float = 0.0,
    all_sector_returns_30d: Optional[List[float]] = None,
    price_ceiling: float = 20.0,
    free_float_shares: Optional[int] = None,
    free_float_pct: Optional[float] = None,
    name_change_details: Optional[str] = None,
    capital_increase_details: Optional[str] = None
) -> Dict[str, Any]:
    """
    Calculate 0–100 Multibagger Pattern Score, reasoning bullet points, and warning flags.
    
    Returns:
      {
        "score": int,
        "raw_score": float,
        "breakdown": {
          "name_change_pts": float,
          "capital_increase_pts": float,
          "volume_zscore_pts": float,
          "turnaround_pts": float,
          "price_ceiling_pts": float,
          "sector_momentum_pts": float
        },
        "reasons": List[str],
        "flags": List[str],
        "metrics": {
          "volume_zscore_norm": float,
          "volume_zscore_raw": float,
          "volume_ratio_20d_90d": float,
          "turnaround_quarters": int,
          "sector_momentum_norm": float
        }
      }
    """
    reasons = []
    flags = []

    # 1. Name or Sector Change (25 pts)
    if has_name_or_sector_change:
        name_pts = 25.0
        det_str = f" ({name_change_details})" if name_change_details else ""
        reasons.append(f"Sector/business change disclosed within last 12mo{det_str}")
    else:
        name_pts = 0.0

    # 2. Capital Increase (20 pts)
    if has_capital_increase:
        cap_pts = 20.0
        det_str = f" ({capital_increase_details})" if capital_increase_details else ""
        reasons.append(f"Authorised/paid-up capital increase or rights issue disclosed within last 12mo{det_str}")
    else:
        cap_pts = 0.0
        reasons.append("No capital increase disclosed yet — may precede one")
        flags.append("no_capital_increase_yet")

    # 3. Volume Z-Score Expansion (20 pts)
    norm_z, raw_z, vol_ratio = calculate_volume_zscore(volumes)
    vol_pts = round(20.0 * norm_z, 2)
    if raw_z >= 1.0:
        reasons.append(f"Volume surge: 20-day average is {vol_ratio}x 90-day baseline (Z-Score: +{raw_z:.1f}σ)")
    elif vol_ratio > 1.2:
        reasons.append(f"Volume accumulation: 20-day volume trending {vol_ratio}x above 90-day baseline")

    # 4. Turnaround Revenue Growth (15 pts)
    growth_capped = max(0, min(3, consecutive_qoq_growth))
    turnaround_norm = growth_capped / 3.0
    turnaround_pts = round(15.0 * turnaround_norm, 2)
    if consecutive_qoq_growth >= 2:
        reasons.append(f"Turnaround confirmed: {consecutive_qoq_growth} consecutive quarters of QoQ revenue expansion")
    elif consecutive_qoq_growth == 1:
        reasons.append("Early revenue inflection: 1 quarter of QoQ revenue growth recorded")
        flags.append("single_quarter_inflection")
    else:
        flags.append("no_turnaround_confirmed_yet")

    # 5. Low Price Ceiling (10 pts)
    if price > 0.0 and price <= price_ceiling:
        price_pts = 10.0
        reasons.append(f"Low price base: Rs {price:.2f} (under Rs {price_ceiling:.0f} target ceiling)")
    else:
        price_pts = 0.0

    # 6. Sector Momentum (10 pts)
    all_rets = all_sector_returns_30d if all_sector_returns_30d is not None else [sector_return_30d]
    sec_norm = calculate_sector_momentum(sector_return_30d, all_rets)
    sec_pts = round(10.0 * sec_norm, 2)
    if sec_norm >= 0.70:
        reasons.append(f"Sector tailwind: Sector momentum ranked in upper decile (Score: {sec_norm * 10:.1f}/10)")

    # Flags evaluation
    if free_float_shares is not None and free_float_shares > 0:
        if free_float_shares < 50_000_000:
            flags.append("low_float")
    elif free_float_pct is not None and free_float_pct > 0:
        if free_float_pct < 35.0:
            flags.append("low_float")

    if price < 5.0:
        flags.append("penny_stock_caution")

    if volumes and len(volumes) >= 20:
        v20_avg = sum(volumes[-20:]) / 20.0
        if v20_avg < 25_000:
            flags.append("thin_liquidity")

    raw_total = name_pts + cap_pts + vol_pts + turnaround_pts + price_pts + sec_pts
    final_score = int(round(max(0.0, min(100.0, raw_total))))

    return {
        "score": final_score,
        "raw_score": round(raw_total, 2),
        "breakdown": {
            "name_change_pts": name_pts,
            "capital_increase_pts": cap_pts,
            "volume_zscore_pts": vol_pts,
            "turnaround_pts": turnaround_pts,
            "price_ceiling_pts": price_pts,
            "sector_momentum_pts": sec_pts
        },
        "reasons": reasons,
        "flags": flags,
        "metrics": {
            "volume_zscore_norm": norm_z,
            "volume_zscore_raw": raw_z,
            "volume_ratio_20d_90d": vol_ratio,
            "turnaround_quarters": consecutive_qoq_growth,
            "sector_momentum_norm": sec_norm
        }
    }
