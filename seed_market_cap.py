#!/usr/bin/env python3
"""
One-time SHARES-OUTSTANDING seeder.

Run this ONCE on a machine with a normal (residential) internet connection —
e.g. your Mac. For each symbol it fetches market cap from NSE's official quote
API (yfinance fallback) and DIVIDES by the latest bhavcopy close to recover
**shares outstanding**, which it writes to data/shares_outstanding.csv.

Why shares, not market cap: shares outstanding change only on splits / bonus /
buybacks / new issues (a few times a year at most). So this file stays valid for
MONTHS, and every daily data_fetch.py run recomputes market cap locally as
`shares x today's close` — for the WHOLE universe, with NO network call, from any
IP. That's the fix for the market-cap blocking problem: fetch the slow-moving
number once here; compute the fast-moving number (price) yourself every day.

Re-run this quarterly (or after a big corporate action) to refresh shares.

Usage:
    python seed_market_cap.py                 # seed all equities in the universe
    python seed_market_cap.py --survivors     # only current Stage-2 survivors
    python seed_market_cap.py --symbols RELIANCE TCS INFY
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3

import pandas as pd

import config
import nse_source

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger("seed")
SHARES_CSV = os.path.join(config.DATA_DIR, "shares_outstanding.csv")


def _latest_close() -> dict[str, float]:
    """{symbol: latest close} from the cached bhavcopies."""
    hist, _ = nse_source.build_history_frames(3)
    return {s: float(df["Close"].iloc[-1]) for s, df in hist.items() if len(df)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--survivors", action="store_true", help="Only current Stage-2 survivors.")
    ap.add_argument("--symbols", nargs="*", help="Explicit symbol list.")
    args = ap.parse_args()

    closes = _latest_close()

    if args.symbols:
        symbols = [s.upper() for s in args.symbols]
    elif args.survivors:
        if not os.path.exists(config.DB_PATH):
            raise SystemExit("No screener.db yet — run data_fetch.py first, or use --symbols.")
        conn = sqlite3.connect(config.DB_PATH)
        df = pd.read_sql("SELECT symbol, stage1_pass, stage2_pass FROM metrics", conn)
        conn.close()
        symbols = df[(df.stage1_pass == 1) & (df.stage2_pass == 1)]["symbol"].tolist()
    else:
        symbols = sorted(closes.keys())
    log.info("Seeding shares outstanding for %d symbols...", len(symbols))

    caps = nse_source.fetch_market_caps(symbols)  # {sym: (mcap_inr, shares_or_None)}
    rows = []
    for s, (mc, shares) in caps.items():
        if shares and shares > 0:            # provider gave shares directly
            rows.append({"symbol": s, "shares": round(shares)})
        elif mc and closes.get(s):           # derive shares = market_cap / price
            rows.append({"symbol": s, "shares": round(mc / closes[s])})

    # Merge with any existing file so we never lose previously-seeded values.
    existing = {}
    if os.path.exists(SHARES_CSV):
        old = pd.read_csv(SHARES_CSV)
        existing = dict(zip(old["symbol"].str.upper(), old["shares"]))
    for r in rows:
        existing[r["symbol"].upper()] = r["shares"]

    out = pd.DataFrame(sorted(existing.items()), columns=["symbol", "shares"])
    out.to_csv(SHARES_CSV, index=False)
    log.info("Wrote %d shares-outstanding rows to %s (%d newly fetched this run)",
             len(out), SHARES_CSV, len(rows))
    if not rows:
        log.warning("Fetched 0 — you are likely on a blocked/datacenter IP. "
                    "Run this on your residential connection (your Mac).")


if __name__ == "__main__":
    main()
