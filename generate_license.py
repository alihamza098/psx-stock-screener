#!/usr/bin/env python3
"""
PSX Screener Pro — License Key Generator Tool
Use this tool to generate valid 30-day or 365-day license keys for customers after payment.
Usage:
    python3 generate_license.py                (Generates a 30-day license)
    python3 generate_license.py --days 365     (Generates a 1-year license)
"""

import sys
import json
import random
import string
import time
from pathlib import Path

LICENSE_FILE = Path(__file__).parent / "licenses.json"

def generate_key():
    part1 = "".join(random.choices(string.digits, k=4))
    part2 = "".join(random.choices(string.digits, k=4))
    return f"PSX-PRO-{part1}-{part2}"

def main():
    days = 30
    if "--days" in sys.argv:
        try:
            idx = sys.argv.index("--days")
            days = int(sys.argv[idx + 1])
        except Exception:
            days = 30

    db = {}
    if LICENSE_FILE.exists():
        try:
            with open(LICENSE_FILE, "r") as f:
                db = json.load(f)
        except Exception:
            db = {}

    key = generate_key()
    db[key] = {
        "valid": True,
        "days": days,
        "used": False,
        "email": None,
        "name": None,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S PKT", time.localtime(time.time() + 5*3600))
    }

    with open(LICENSE_FILE, "w") as f:
        json.dump(db, f, indent=2)

    print("=" * 60)
    print("🔑 PSX SCREENER PRO — LICENSE KEY GENERATED")
    print("=" * 60)
    print(f"License Key: {key}")
    print(f"Valid For:   {days} Days")
    print(f"Status:      Active (Ready to send to buyer)")
    print("=" * 60)

if __name__ == "__main__":
    main()
