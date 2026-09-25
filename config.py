"""
Central configuration for the Indian equity screening framework.

All tunable knobs live here so the fetch script, the pipeline logic and the
dashboard all agree on the same thresholds and file locations.
"""
from __future__ import annotations

import os
from datetime import date, timedelta

# ----------------------------------------------------------------------------
# Paths
# ----------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(DATA_DIR, "logs")
CACHE_DIR = os.path.join(DATA_DIR, "cache")        # per-day raw downloads (resumable)

DB_PATH = os.path.join(DATA_DIR, "screener.db")     # SQLite snapshot the dashboard reads
UNIVERSE_CSV = os.path.join(DATA_DIR, "universe.csv")

for _d in (DATA_DIR, LOG_DIR, CACHE_DIR):
    os.makedirs(_d, exist_ok=True)

# ----------------------------------------------------------------------------
# Universe (list of active equities)
# ----------------------------------------------------------------------------
# Official NSE master list of all listed equities.
NSE_EQUITY_LIST_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
# Series to keep. "EQ" = normal rolling settlement equity. "BE" = trade-to-trade.
KEEP_SERIES = {"EQ"}
YF_SUFFIX = ".NS"   # yfinance suffix for NSE tickers

# ----------------------------------------------------------------------------
# History window
# ----------------------------------------------------------------------------
# We need >= 252 trading days for a clean 52-week high/low, plus buffer.
HISTORY_LOOKBACK_DAYS = 420
TRADING_DAYS_52W = 252
WEEKLY_WINDOW = 5      # trading days in a "week"
MONTHLY_WINDOW = 21    # trading days in a "month"

# ----------------------------------------------------------------------------
# Screening thresholds (the user's rules)
# ----------------------------------------------------------------------------
# Stage 1: increased on at least this many of the last N day-over-day moves.
UP_DAYS_REQUIRED = 4
UP_DAYS_WINDOW = 5

# Stage 2: current (latest) daily volume must exceed the weekly average volume.
#   (weekly average = mean daily volume over the PRIOR week, excluding today,
#    so the comparison is a genuine "today is heavier than usual" signal.)

# Stage 3: market capitalisation must exceed this many crore.
MARKET_CAP_CRORE_THRESHOLD = 10_000
CRORE = 1e7  # 1 crore = 10,000,000

# Extra screen (independent of the main funnel): stocks trading within this
# percentage of their 52-week low.
NEAR_52W_LOW_PCT = 5.0

# How many recent trading days of bhavcopy history to assemble. 52-week
# high/low come from NSE's official report, so we only need enough days for the
# momentum test (6) and the monthly volume average (21) plus buffer.
HISTORY_TRADING_DAYS = 30

# ----------------------------------------------------------------------------
# Fetch behaviour
# ----------------------------------------------------------------------------
CHUNK_SIZE = 150            # tickers per yfinance batch download
CHUNK_SLEEP_SECONDS = 1.0   # polite pause between chunks
MAX_MCAP_WORKERS = 12       # threads for per-ticker market-cap lookups
NETWORK_RETRIES = 3
RETRY_BACKOFF_SECONDS = 3


def history_start_end():
    """Return (start, end) dates for the price-history download."""
    today = date.today()
    start = today - timedelta(days=HISTORY_LOOKBACK_DAYS)
    end = today + timedelta(days=1)  # yfinance 'end' is exclusive
    return start.isoformat(), end.isoformat()
