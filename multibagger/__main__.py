#!/usr/bin/env python3
"""
Multibagger CLI Entry Point
===========================
Usage:
  python3 -m multibagger scan [--ceiling 20.0] [--top 15]
  python3 -m multibagger backtest [--rebuild] [--min-multiple 5.0]
  python3 -m multibagger reference [--sort multiple|date]
  python3 -m multibagger research <TICKER> [--force]
"""

import sys
import json
import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(
        prog="python3 -m multibagger",
        description="PSX Multibagger Pattern Finder CLI (Speculative Setups — Pattern Match Only)"
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Scan command
    scan_parser = subparsers.add_parser("scan", help="Run daily candidate scan")
    scan_parser.add_argument("--ceiling", type=float, default=20.0, help="Max price ceiling (default: 20.0 PKR)")
    scan_parser.add_argument("--top", type=int, default=15, help="Number of candidates to output (default: 15)")

    # Backtest / Reference table command
    backtest_parser = subparsers.add_parser("backtest", help="Rebuild historical reference table")
    backtest_parser.add_argument("--min-multiple", type=float, default=5.0, help="Minimum run multiple (default: 5.0x)")
    backtest_parser.add_argument("--rebuild", action="store_true", help="Rescan historical timeseries")

    # Reference command
    ref_parser = subparsers.add_parser("reference", help="View historical reference table")
    ref_parser.add_argument("--sort", choices=["multiple", "date"], default="multiple", help="Sort order")

    # Research command
    res_parser = subparsers.add_parser("research", help="Deep research on a ticker")
    res_parser.add_argument("ticker", help="PSX ticker symbol (e.g. ITANZ, THCCL, AATM)")
    res_parser.add_argument("--force", action="store_true", help="Force refresh research sources")

    args = parser.parse_args()

    if args.command == "scan" or args.command is None:
        from .scanner import run_multibagger_scan, get_latest_candidates
        # Try loading stock cache
        stocks = []
        cache_file = Path("cache/stocks_cache.json")
        if cache_file.exists():
            try:
                with open(cache_file, "r") as f:
                    stocks = json.load(f).get("data", [])
            except Exception:
                stocks = []
        
        ceiling = getattr(args, "ceiling", 20.0) if args.command == "scan" else 20.0
        top_n = getattr(args, "top", 15) if args.command == "scan" else 15
        
        print(f"\n=======================================================")
        print(f"⚡ PSX MULTIBAGGER DAILY SCANNER (Ceiling: {ceiling} PKR)")
        print(f"=======================================================")
        candidates = run_multibagger_scan(stocks, price_ceiling=ceiling, top_n=top_n)
        if not candidates:
            cached = get_latest_candidates()
            candidates = cached.get("candidates", [])

        print(f"Found {len(candidates)} ranked candidates:\n")
        for i, c in enumerate(candidates, 1):
            analog = c.get("nearest_analog", "—")
            sim = c.get("similarity_pct", 0)
            tier = c.get("confidence_tier", "early_signal")
            print(f"#{i:02d} [{c['ticker']}] Score: {c['score']}/100 | Price: ₨{c['price']:.2f} | Analog: {analog} ({sim}%) [{tier}]")
            print(f"     Sector: {c.get('sector', 'Other')} | Float: {c.get('float_shares', 0):,}")
            if c.get("reasons"):
                for r in c["reasons"][:2]:
                    print(f"     • {r}")
            if c.get("historical_hit_rate"):
                print(f"     📊 Hit-Rate: {c['historical_hit_rate']}")
            print()

    elif args.command == "reference":
        from .historical_backtest import get_historical_reference_table
        rows = get_historical_reference_table(sort_by=args.sort)
        print(f"\n=======================================================")
        print(f"📚 PSX HISTORICAL MULTIBAGGER REFERENCE TABLE ({len(rows)} cases)")
        print(f"=======================================================\n")
        print(f"{'TICKER':<8} {'MULTIPLE':<10} {'PRICE RUN (PKR)':<20} {'START DATE':<12} {'SECTOR':<26} {'TRIGGERS'}")
        print("-" * 105)
        for r in rows:
            run_str = f"₨{r['start_price']:.2f} -> ₨{r['peak_price']:.2f}"
            tags = ", ".join(r.get("trigger_tags", []))
            print(f"{r['ticker']:<8} {r['multiple_achieved']:>5.1f}x     {run_str:<20} {r['run_start_date']:<12} {r['sector'][:25]:<26} {tags}")
        print()

    elif args.command == "backtest":
        from .historical_backtest import run_historical_backtest, get_historical_reference_table
        print(f"\n[Multibagger] Running backtest scan (threshold: {args.min_multiple}x)...")
        added = run_historical_backtest(min_multiple=args.min_multiple)
        print(f"[Multibagger] Done. Processed/added: {added} cases.")
        rows = get_historical_reference_table()
        print(f"[Multibagger] Total reference cases in library: {len(rows)}")

    elif args.command == "research":
        from .research.synthesizer import synthesize_research
        sym = args.ticker.upper().strip()
        print(f"\n[Multibagger] Launching Deep Research for '{sym}' (force={args.force})...")
        report = synthesize_research(sym, force_refresh=args.force)
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
