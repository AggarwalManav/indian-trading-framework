"""
Pure, side-effect-free screening logic.

These functions take a single stock's OHLCV history (a pandas DataFrame indexed
by ascending date with columns Open/High/Low/Close/Volume) and produce the
metrics + stage flags the framework screens on. Keeping them pure makes them
trivial to unit-test and keeps the meaning of each rule unambiguous.
"""
from __future__ import annotations

import math
from typing import Optional

import pandas as pd

import config


def _clean(hist: pd.DataFrame) -> pd.DataFrame:
    hist = hist.copy()
    # Standardise column names (yfinance sometimes lowercases / adds Adj Close).
    hist = hist.rename(columns={c: c.title() for c in hist.columns})
    keep = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in hist.columns]
    hist = hist[keep]
    hist = hist.dropna(subset=["Close", "Volume"]).sort_index()
    return hist


def compute_metrics(
    symbol: str,
    name: str,
    hist: pd.DataFrame,
    high_52w: Optional[float] = None,
    low_52w: Optional[float] = None,
) -> Optional[dict]:
    """
    Compute every metric the user asked for, plus Stage 1 / Stage 2 flags.

    `high_52w` / `low_52w` may be supplied from NSE's official 52-week report;
    if omitted they are computed from the supplied history.

    Returns None if there is not enough history to be meaningful.
    """
    hist = _clean(hist)
    # Need at least 6 closes to have 5 day-over-day changes.
    if len(hist) < 6:
        return None

    close = hist["Close"]
    high = hist["High"] if "High" in hist else close
    low = hist["Low"] if "Low" in hist else close
    vol = hist["Volume"]

    last_price = float(close.iloc[-1])
    prev_close = float(close.iloc[-2])
    price_date = close.index[-1].strftime("%Y-%m-%d")

    # --- Stage 1: up on >= UP_DAYS_REQUIRED of the last UP_DAYS_WINDOW days ---
    diffs = close.diff().dropna()
    last_diffs = diffs.tail(config.UP_DAYS_WINDOW)
    up_days = int((last_diffs > 0).sum())
    stage1_pass = up_days >= config.UP_DAYS_REQUIRED

    # --- 52-week high / low (official report if given, else from history) ---
    if high_52w is None:
        high_52w = float(high.tail(config.TRADING_DAYS_52W).max())
    if low_52w is None:
        low_52w = float(low.tail(config.TRADING_DAYS_52W).min())
    high_52w = float(high_52w)
    low_52w = float(low_52w)

    # --- Volumes ---
    vol_daily = float(vol.iloc[-1])
    # Weekly/monthly AVERAGES exclude the current day so the Stage-2 comparison
    # ("today heavier than usual") is not biased by including today itself.
    w = config.WEEKLY_WINDOW
    m = config.MONTHLY_WINDOW
    vol_weekly_avg = float(vol.iloc[-(w + 1):-1].mean()) if len(vol) > w else float(vol.iloc[:-1].mean())
    vol_monthly_avg = float(vol.iloc[-(m + 1):-1].mean()) if len(vol) > m else float(vol.iloc[:-1].mean())
    # Weekly/monthly totals (traded volume over the period, including today).
    vol_weekly_total = float(vol.tail(w).sum())
    vol_monthly_total = float(vol.tail(m).sum())

    # --- Stage 2: current volume > weekly average volume ---
    stage2_pass = bool(vol_daily > vol_weekly_avg) and vol_weekly_avg > 0

    # --- Last 5 days of prices (most recent last) ---
    tail5 = close.tail(5)
    last5_closes = [round(float(x), 2) for x in tail5]
    last5_dates = [d.strftime("%Y-%m-%d") for d in tail5.index]

    pct_change_1d = round((last_price / prev_close - 1) * 100, 2) if prev_close else None
    pct_from_52w_high = round((last_price / high_52w - 1) * 100, 2) if high_52w else None
    # Distance above the 52-week low, and the "within 5% of the low" screen.
    pct_from_52w_low = round((last_price / low_52w - 1) * 100, 2) if low_52w else None
    near_52w_low = bool(pct_from_52w_low is not None and pct_from_52w_low <= config.NEAR_52W_LOW_PCT)

    return {
        "symbol": symbol,
        "name": name,
        "price_date": price_date,
        "last_price": round(last_price, 2),
        "prev_close": round(prev_close, 2),
        "pct_change_1d": pct_change_1d,
        "high_52w": round(high_52w, 2),
        "low_52w": round(low_52w, 2),
        "pct_from_52w_high": pct_from_52w_high,
        "pct_from_52w_low": pct_from_52w_low,
        "near_52w_low": near_52w_low,
        "vol_daily": vol_daily,
        "vol_weekly_avg": round(vol_weekly_avg, 2),
        "vol_monthly_avg": round(vol_monthly_avg, 2),
        "vol_weekly_total": vol_weekly_total,
        "vol_monthly_total": vol_monthly_total,
        "up_days_last5": up_days,
        "last5_closes": ",".join(str(x) for x in last5_closes),
        "last5_dates": ",".join(last5_dates),
        "vol_vs_weekly_ratio": round(vol_daily / vol_weekly_avg, 2) if vol_weekly_avg else None,
        # market cap fields filled later, only for Stage-2 survivors
        "market_cap": None,
        "market_cap_cr": None,
        "shares_outstanding": None,
        "stage1_pass": bool(stage1_pass),
        "stage2_pass": bool(stage2_pass),
        "stage3_pass": None,   # unknown until market cap fetched
        "final_pass": False,
    }


def apply_market_cap(row: dict, market_cap: Optional[float]) -> dict:
    """Fill in Stage 3 (market cap > threshold) and the final verdict."""
    if market_cap and not (isinstance(market_cap, float) and math.isnan(market_cap)):
        row["market_cap"] = float(market_cap)
        row["market_cap_cr"] = round(float(market_cap) / config.CRORE, 2)
        row["stage3_pass"] = bool(
            row["market_cap_cr"] >= config.MARKET_CAP_CRORE_THRESHOLD
        )
    else:
        row["market_cap"] = None
        row["market_cap_cr"] = None
        row["stage3_pass"] = False

    row["final_pass"] = bool(
        row.get("stage1_pass") and row.get("stage2_pass") and row.get("stage3_pass")
    )
    return row
