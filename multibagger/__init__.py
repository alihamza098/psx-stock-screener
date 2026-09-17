"""
Multibagger Pattern Finder Package
===================================
Pattern-matching screener identifying low-priced PSX stocks (<20 PKR) that exhibit
setup characteristics of historical 5x-70x runners (name/sector changes, capital raises,
revenue turnarounds, volume spikes, low float).

Notice: Pattern-matching and setup discovery only. Never issues buy/sell recommendations.
"""

try:
    from .scoring import calculate_multibagger_score
except ImportError:
    pass

try:
    from .scanner import run_multibagger_scan, get_latest_candidates
except ImportError:
    pass

try:
    from .historical_backtest import run_historical_backtest, get_historical_reference_table
except ImportError:
    pass
