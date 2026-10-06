#!/usr/bin/env python3
"""
PSX Transaction Cost & Market-Rule Model
========================================
One source of truth for what a trade really costs on PSX (config/costs.json):

  commission  = max(pct_of_value * price, min_per_share) * qty      (KTrade: 0.15% or 3 paisa/share)
              + sales tax on commission                             (Sindh SST 15%)
              (same-day round trips are charged on one side only)
  regulatory  = SECP levy + CDC/NCCPL charges (pct of traded value)
  slippage    = base bps per side, applied to the fill price
  CGT         = flat % of net realised gains (applied at report level)

Also exposes PSX market rules: circuit limits, lot size, settlement lag, short eligibility.
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

CONFIG_PATH = Path(__file__).parent / "config" / "costs.json"

_DEFAULTS: Dict[str, Any] = {
    "commission": {"pct_of_value": 0.0015, "min_per_share_pkr": 0.03,
                   "sales_tax_on_commission_pct": 0.15, "day_trade_one_side_only": True},
    "regulatory": {"secp_levy_pct_of_value": 0.0000065, "cdc_nccpl_pct_of_value": 0.0},
    "slippage": {"base_bps_per_side": 10, "max_pct_of_avg_daily_volume": 5.0},
    "tax": {"capital_gains_tax_pct": 15.0},
    "market_rules": {"circuit_limit_pct": 7.5, "circuit_limit_min_pkr": 1.0, "lot_size": 1,
                     "settlement_days": 2, "short_selling_eligible_symbols": []},
}


def _merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = json.loads(json.dumps(base))
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


@lru_cache(maxsize=1)
def _load_file() -> Dict[str, Any]:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


class CostModel:
    def __init__(self, overrides: Optional[Dict[str, Any]] = None):
        self.cfg = _merge(_merge(_DEFAULTS, _load_file()), overrides or {})
        c, r = self.cfg["commission"], self.cfg["regulatory"]
        self.pct = float(c["pct_of_value"])
        self.min_ps = float(c["min_per_share_pkr"])
        self.sst = float(c["sales_tax_on_commission_pct"])
        self.day_one_side = bool(c["day_trade_one_side_only"])
        self.reg_pct = float(r["secp_levy_pct_of_value"]) + float(r["cdc_nccpl_pct_of_value"])
        self.slip = float(self.cfg["slippage"]["base_bps_per_side"]) / 10000.0
        self.max_adv_pct = float(self.cfg["slippage"]["max_pct_of_avg_daily_volume"])
        self.cgt_pct = float(self.cfg["tax"]["capital_gains_tax_pct"])
        m = self.cfg["market_rules"]
        self.circuit_pct = float(m["circuit_limit_pct"])
        self.circuit_min = float(m["circuit_limit_min_pkr"])
        self.lot = max(1, int(m["lot_size"]))
        self.settlement_days = int(m["settlement_days"])
        self.short_eligible = {s.upper() for s in m.get("short_selling_eligible_symbols", [])}

    # ── costs ───────────────────────────────────────────────────────────────
    def commission(self, price: float, qty: float, charge: bool = True) -> float:
        """Broker commission incl. sales tax for one side. charge=False → 0 (free side of a day trade)."""
        if not charge or qty <= 0:
            return 0.0
        per_share = max(self.pct * price, self.min_ps)
        return per_share * qty * (1.0 + self.sst)

    def regulatory(self, price: float, qty: float) -> float:
        return self.reg_pct * price * qty

    def fill_price(self, price: float, side: str) -> float:
        """Price after slippage. side: 'buy' pays up, 'sell' receives less."""
        return price * (1.0 + self.slip) if side == "buy" else price * (1.0 - self.slip)

    def side_cost(self, price: float, qty: float, charge_commission: bool = True) -> float:
        return self.commission(price, qty, charge_commission) + self.regulatory(price, qty)

    def round_trip_pct(self, price: float, qty: float = 1000, day_trade: bool = False) -> float:
        """All-in round-trip cost (commission + levies + slippage) as % of value — for display."""
        value = price * qty
        sell_charged = not (day_trade and self.day_one_side)
        fees = self.side_cost(price, qty) + self.side_cost(price, qty, sell_charged)
        return (fees / value + 2 * self.slip) * 100.0 if value else 0.0

    def cgt(self, net_realised_gain: float) -> float:
        return max(0.0, net_realised_gain) * self.cgt_pct / 100.0

    # ── market rules ────────────────────────────────────────────────────────
    def circuit_band(self, ref_price: float):
        spread = max(self.circuit_min, ref_price * self.circuit_pct / 100.0)
        return max(0.01, ref_price - spread), ref_price + spread

    def round_lot(self, qty: float) -> int:
        return int(qty // self.lot) * self.lot

    def can_short(self, symbol: str) -> bool:
        return symbol.upper() in self.short_eligible


_default: Optional[CostModel] = None


def get_cost_model() -> CostModel:
    global _default
    if _default is None:
        _default = CostModel()
    return _default
