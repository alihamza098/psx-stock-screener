#!/usr/bin/env python3
"""
PSX Multi-Lingual Natural Language Explainer Engine
---------------------------------------------------
Converts algorithmic signals, breakouts, indicators, and risk setups
into human-readable English and Urdu explanations with zero API costs.
"""

from typing import Dict, Any

URDU_SECTOR_NAMES = {
    "Cement": "سیمنٹ سیکٹر",
    "Commercial Banks": "کمرشل بینکنگ",
    "Oil & Gas Exploration Companies": "تیل و گیس کی تلاش",
    "Oil & Gas Marketing Companies": "تیل و گیس مارکیٹنگ",
    "Refinery": "ریفائنری سیکٹر",
    "Fertilizer": "کھاد و زرعی کیمیکلز",
    "Pharmaceuticals": "ادویات سازی",
    "Technology & Communication": "ٹیکنالوجی اور مواصلات",
    "Automobile Assembler": "آٹو موبائل مینوفیکچرنگ",
    "Engineering": "انجینئرنگ اور اسٹیل",
    "Textile Composite": "ٹیکسٹائل کمپوزٹ",
    "Food & Personal Care Products": "خوراک و ذاتی نگہداشت",
    "Power Generation & Distribution": "بجلی اور توانائی",
    "Property": "پراپرٹی اور ریئل اسٹیٹ"
}

def explain_candidate(cand: Dict[str, Any], lang: str = "en") -> str:
    """Generate a natural explanation of a swing setup in English or Urdu."""
    sym = cand.get("symbol", "")
    sec = cand.get("sector", "")
    grade = cand.get("grade", "A")
    conv = cand.get("conviction", 80)
    risk = cand.get("risk", {})
    entry = risk.get("entry", 0.0)
    stop = risk.get("stop", 0.0)
    tp1 = risk.get("takeProfit1") or risk.get("target", 0.0)
    risk_pct = risk.get("riskPct", 4.0)
    reward_pct = risk.get("rewardPctTp1", 8.0)
    rr = risk.get("rewardRiskRatio", 2.0)

    if lang == "ur":
        ur_sec = URDU_SECTOR_NAMES.get(sec, sec)
        return (
            f"{sym} ({ur_sec}) میں {grade} گریڈ سوئنگ ٹریڈ کا مضبوط سیٹ اپ پایا گیا ہے۔ "
            f"سیٹ اپ پر AI کا اعتماد {conv}% ہے۔ داخلہ زون: ₨{entry:.2f}۔ "
            f"پہلا متوقع ہدف ₨{tp1:.2f} (+{reward_pct:.1f}%) ہے جبکہ سرمایہ کے تحفظ کیلئے سٹاپ لاس ₨{stop:.2f} (-{risk_pct:.1f}%) "
            f"پر رکھا گیا ہے، جو {rr:.1f}x کا بہترین رسک ٹو ریوارڈ تناسب پیش کرتا ہے۔"
        )
    else:
        return (
            f"{sym} in {sec} exhibits a high-probability {grade}-grade setup with {conv}% AI conviction. "
            f"Entry is recommended at Rs {entry:.2f}. Initial target TP1 sits at Rs {tp1:.2f} (+{reward_pct:.1f}%) "
            f"with defined risk stop-loss at Rs {stop:.2f} (-{risk_pct:.1f}%), offering an attractive {rr:.1f}x Reward-to-Risk ratio."
        )

def explain_macro(macro: Dict[str, Any], lang: str = "en") -> str:
    rate = macro.get("sbp_policy_rate", 17.5)
    cycle = macro.get("cycle", "EASING")
    if lang == "ur":
        return f"اسٹیٹ بینک آف پاکستان کا موجودہ شرح سود {rate}% ہے جو کہ مانیٹری نرمی اور کٹوتی کے دور میں داخل ہو چکا ہے۔ اس سے سیمنٹ، آٹو، اور اسٹیل سیکٹرز کے مالی اخراجات میں نمایاں کمی متوقع ہے۔"
    return f"SBP policy rate currently stands at {rate}% in an easing cycle, providing strong valuation tailwinds for leveraged cyclicals like Cement, Steel, and Automotive."

generate_candidate_explanation = explain_candidate

if __name__ == "__main__":
    sample = {
        "symbol": "LUCK",
        "sector": "Cement",
        "grade": "A_PLUS",
        "conviction": 94,
        "risk": {"entry": 850.0, "stop": 816.0, "takeProfit1": 918.0, "riskPct": 4.0, "rewardPctTp1": 8.0, "rewardRiskRatio": 2.0}
    }
    print("EN:", explain_candidate(sample, "en"))
    print("UR:", explain_candidate(sample, "ur"))
