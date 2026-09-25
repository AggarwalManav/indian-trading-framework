#!/usr/bin/env python3
"""
Daily data fetch + screening engine for Indian (NSE) equities.

Data source: OFFICIAL NSE reports (no Yahoo, no per-ticker price scraping).
  1. Download the last ~30 daily bhavcopies (one bulk file each) -> OHLCV history.
  2. Download NSE's official 52-week high/low report.
  3. Compute all metrics + Stage 1 (up 4/5 days) + Stage 2 (vol > weekly avg)
     + the "within 5% of 52-week low" screen.
  4. Fetch market cap ONLY for Stage-2 survivors (official NSE quote API, with
     yfinance fallback) -> Stage 3 (mcap > 10k cr).
  5. Write a full snapshot to SQLite (data/screener.db) for the dashboard.

Usage:
    python data_fetch.py                 # full daily run (NSE bhavcopy)
    python data_fetch.py --limit 200     # only screen first 200 symbols (testing)
    python data_fetch.py --no-mcap       # skip market cap (fast, Stage 3 blank)
    python data_fetch.py --source dhan   # use Dhan broker OHLC (needs a token;
                                         #   falls back to bhavcopy on any failure)
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import time
from datetime import date

import pandas as pd

import config
import nse_source
import pipeline

# ----------------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------------
LOG_FILE = os.path.join(config.LOG_DIR, f"fetch_{date.today().isoformat()}.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("screener")


# ----------------------------------------------------------------------------
# Persistence
# ----------------------------------------------------------------------------
def _mcap_seed_age_days() -> float | None:
    """Age (in days) of the newest market-cap seed file, or None if neither exists.
    Used to warn when shares outstanding may be stale (re-seed ~quarterly)."""
    import time as _t
    ages = []
    for name in ("shares_outstanding.csv", "market_cap_override.csv"):
        p = os.path.join(config.DATA_DIR, name)
        if os.path.exists(p):
            ages.append((_t.time() - os.path.getmtime(p)) / 86400)
    return round(min(ages), 1) if ages else None


def write_snapshot(rows: list[dict], run_stats: dict) -> None:
    df = pd.DataFrame(rows)
    conn = sqlite3.connect(config.DB_PATH)
    try:
        df.to_sql("metrics", conn, if_exists="replace", index=False)
        pd.DataFrame([run_stats]).to_sql("run_meta", conn, if_exists="replace", index=False)
        conn.commit()
    finally:
        conn.close()
    df.to_csv(os.path.join(config.DATA_DIR, "all_metrics.csv"), index=False)
    if not df.empty:
        df[df["final_pass"].astype(bool)].to_csv(
            os.path.join(config.DATA_DIR, "final_picks.csv"), index=False)
        df[df["near_52w_low"].astype(bool)].to_csv(
            os.path.join(config.DATA_DIR, "near_52w_low.csv"), index=False)
    log.info("Snapshot written to %s (%d rows)", config.DB_PATH, len(df))


# ----------------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------------
def _build_history(source: str):
    """Return (hist, names, source_label). 'dhan' needs a token and falls back
    to bhavcopy on any failure so the daily job never breaks."""
    if source == "dhan":
        try:
            import broker_dhan
            if not broker_dhan.is_enabled():
                log.warning("--source dhan requested but no Dhan token found "
                            "(data/broker_creds.env). Falling back to bhavcopy.")
            else:
                log.info("Building history from Dhan broker OHLC...")
                hist, names = broker_dhan.build_history_frames(config.HISTORY_TRADING_DAYS)
                if hist:
                    return hist, names, "Dhan broker OHLC"
                log.warning("Dhan returned no data. Falling back to bhavcopy.")
        except Exception as e:
            log.warning("Dhan source failed (%s). Falling back to bhavcopy.", e)
    hist, names = nse_source.build_history_frames(config.HISTORY_TRADING_DAYS)
    return hist, names, "NSE bhavcopy + 52wk report"


def run(limit: int | None = None, do_mcap: bool = True, source: str = "bhavcopy"):
    t0 = time.time()

    # ---- Prices/volume history ----
    hist, names, source_label = _build_history(source)
    symbols = sorted(hist.keys())
    if limit:
        symbols = symbols[:limit]
        log.info("Limiting run to first %d symbols", limit)
    log.info("Universe from bhavcopy: %d equities", len(symbols))

    # ---- Official 52-week high/low ----
    wk52 = nse_source.fetch_52wk_high_low()

    # ---- Stage 0/1/2 + near-low from history ----
    rows: list[dict] = []
    for sym in symbols:
        hi, lo = wk52.get(sym, (None, None))
        m = pipeline.compute_metrics(sym, names.get(sym, sym), hist[sym],
                                     high_52w=hi, low_52w=lo)
        if m is not None:
            rows.append(m)
    log.info("Computed metrics for %d symbols", len(rows))

    by_symbol = {r["symbol"]: r for r in rows}
    stage1 = [r for r in rows if r["stage1_pass"]]
    stage2 = [r for r in stage1 if r["stage2_pass"]]
    near_low = [r for r in rows if r["near_52w_low"]]
    log.info("Stage 1 (up %d/%d days): %d | Stage 2 (vol > wk avg): %d | within 5%% of 52w low: %d",
             config.UP_DAYS_REQUIRED, config.UP_DAYS_WINDOW, len(stage1), len(stage2), len(near_low))

    # ---- Stage 3: market cap ----
    # Preferred path: shares outstanding x today's close, computed locally for the
    # WHOLE universe (no network, works from any IP; shares seeded once by
    # seed_market_cap.py). Network quote lookup is only a fallback for survivors
    # whose shares we don't have yet.
    if do_mcap:
        shares_map = nse_source.load_shares_outstanding()          # symbol -> shares
        mcap_override = nse_source._load_mcap_override()            # symbol -> mcap_cr (direct)
        local_n = 0
        for r in rows:
            sh = shares_map.get(r["symbol"])
            if sh and r.get("last_price"):
                # Best: shares x TODAY's close -> daily-fresh mcap.
                r["shares_outstanding"] = sh
                pipeline.apply_market_cap(r, sh * r["last_price"])
                local_n += 1
            elif r["symbol"] in mcap_override:
                # Fallback: a directly-supplied (possibly stale) mcap figure.
                pipeline.apply_market_cap(r, mcap_override[r["symbol"]] * config.CRORE)
                local_n += 1
        if shares_map or mcap_override:
            log.info("Market cap: %d/%d from local seed files (shares x close / override)",
                     local_n, len(rows))

        missing_survivors = [r["symbol"] for r in stage2 if r.get("market_cap_cr") is None]
        if missing_survivors:
            log.info("Fetching market cap over network for %d survivors missing seed",
                     len(missing_survivors))
            caps = nse_source.fetch_market_caps(missing_survivors)
            for sym, (mc, shares) in caps.items():
                if sym in by_symbol and mc:
                    by_symbol[sym]["shares_outstanding"] = shares
                    pipeline.apply_market_cap(by_symbol[sym], mc)
    else:
        log.info("Skipping market cap (--no-mcap); Stage 3 left blank.")

    final = [r for r in stage2 if r.get("final_pass")]
    survivors_missing_mcap = sum(1 for r in stage2 if r.get("market_cap_cr") is None)
    mcap_covered = sum(1 for r in rows if r.get("market_cap_cr") is not None)
    log.info("Stage 3 (mcap > %d cr) -> FINAL PICKS: %d  (mcap coverage: %d/%d, survivors missing mcap: %d)",
             config.MARKET_CAP_CRORE_THRESHOLD, len(final), mcap_covered, len(rows), survivors_missing_mcap)

    run_stats = {
        "run_at": pd.Timestamp.now().isoformat(),
        "data_source": source_label,
        "universe_count": len(symbols),
        "with_data": len(rows),
        "stage1_count": len(stage1),
        "stage2_count": len(stage2),
        "near_52w_low_count": len(near_low),
        "final_count": len(final),
        "survivors_missing_mcap": survivors_missing_mcap,
        "mcap_covered": mcap_covered,
        "mcap_coverage_pct": round(100 * mcap_covered / len(rows), 1) if rows else 0.0,
        "mcap_seed_age_days": _mcap_seed_age_days(),
        "elapsed_seconds": round(time.time() - t0, 1),
        "up_days_required": config.UP_DAYS_REQUIRED,
        "up_days_window": config.UP_DAYS_WINDOW,
        "mcap_threshold_cr": config.MARKET_CAP_CRORE_THRESHOLD,
        "near_low_pct": config.NEAR_52W_LOW_PCT,
    }
    write_snapshot(list(by_symbol.values()), run_stats)
    log.info("DONE in %.1fs. Final picks: %s",
             run_stats["elapsed_seconds"], ", ".join(r["symbol"] for r in final) or "(none)")
    return run_stats


def main():
    ap = argparse.ArgumentParser(description="Fetch official NSE data and run the screener.")
    ap.add_argument("--limit", type=int, default=None, help="Only screen first N symbols (testing).")
    ap.add_argument("--no-mcap", action="store_true", help="Skip market-cap lookups (fast).")
    ap.add_argument("--source", choices=["bhavcopy", "dhan"], default="bhavcopy",
                    help="Price/volume source. 'dhan' needs a token; falls back to bhavcopy.")
    args = ap.parse_args()
    run(limit=args.limit, do_mcap=not args.no_mcap, source=args.source)


if __name__ == "__main__":
    main()
