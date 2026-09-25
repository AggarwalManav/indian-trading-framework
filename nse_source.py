"""
Official NSE data source — replaces yfinance for prices/volume/52-week levels.

Why this is more reliable than Yahoo:
  * Prices & volume for the ENTIRE market come from ONE daily file (the UDiFF
    "bhavcopy", ~200 KB), served by NSE's archive host with no cookies and no
    per-ticker rate limits.
  * 52-week high/low come from NSE's official (corporate-action adjusted) daily
    report — no need to backfill a year of history.
  * Only market cap still needs a per-symbol call, and we make that only for the
    handful of stocks that survive Stages 1 & 2 (see fetch_market_caps).

All downloads are cached on disk (data/cache/) and reused, so a re-run or the
daily job only fetches what's new.
"""
from __future__ import annotations

import io
import logging
import os
import time
import zipfile
from datetime import date, timedelta

import pandas as pd
import requests

import config

log = logging.getLogger("screener.nse")

BHAV_DIR = os.path.join(config.CACHE_DIR, "bhav")
REPORT_DIR = os.path.join(config.CACHE_DIR, "reports")
for _d in (BHAV_DIR, REPORT_DIR):
    os.makedirs(_d, exist_ok=True)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

BHAV_URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip"
WK52_URL = "https://nsearchives.nseindia.com/content/CM_52_wk_High_low_{ddmmyyyy}.csv"


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_HEADERS)
    return s


def _get(session, url, timeout=25):
    last = None
    for attempt in range(1, config.NETWORK_RETRIES + 1):
        try:
            r = session.get(url, timeout=timeout)
            if r.status_code == 200 and len(r.content) > 200:
                return r
            last = f"HTTP {r.status_code} len={len(r.content)}"
        except Exception as e:
            last = str(e)
        time.sleep(config.RETRY_BACKOFF_SECONDS * attempt)
    log.debug("GET failed %s (%s)", url, last)
    return None


# ----------------------------------------------------------------------------
# Bhavcopy (daily OHLCV for the whole market)
# ----------------------------------------------------------------------------
def _bhav_cache_path(d: date) -> str:
    return os.path.join(BHAV_DIR, f"bhav_{d.isoformat()}.csv")


def download_bhavcopy(d: date, session=None) -> pd.DataFrame | None:
    """Return the cash-market equity rows of one day's bhavcopy, or None if the
    market was closed that day (file not published). Cached to disk."""
    cache = _bhav_cache_path(d)
    if os.path.exists(cache):
        try:
            return pd.read_csv(cache)
        except Exception:
            pass
    session = session or _session()
    url = BHAV_URL.format(ymd=d.strftime("%Y%m%d"))
    r = _get(session, url)
    if r is None:
        return None
    try:
        z = zipfile.ZipFile(io.BytesIO(r.content))
        raw = pd.read_csv(io.BytesIO(z.read(z.namelist()[0])))
    except Exception as e:
        log.warning("Bhavcopy parse failed for %s: %s", d, e)
        return None
    raw.columns = [c.strip() for c in raw.columns]
    # Keep only cash-market equities of the configured series.
    eq = raw[raw["SctySrs"].astype(str).str.strip().isin(config.KEEP_SERIES)].copy()
    out = pd.DataFrame({
        "date": d.isoformat(),
        "symbol": eq["TckrSymb"].astype(str).str.strip(),
        "name": eq.get("FinInstrmNm", eq["TckrSymb"]).astype(str).str.strip(),
        "open": pd.to_numeric(eq["OpnPric"], errors="coerce"),
        "high": pd.to_numeric(eq["HghPric"], errors="coerce"),
        "low": pd.to_numeric(eq["LwPric"], errors="coerce"),
        "close": pd.to_numeric(eq["ClsPric"], errors="coerce"),
        "prev_close": pd.to_numeric(eq["PrvsClsgPric"], errors="coerce"),
        "volume": pd.to_numeric(eq["TtlTradgVol"], errors="coerce"),
    })
    out = out.dropna(subset=["close"]).reset_index(drop=True)
    out.to_csv(cache, index=False)
    return out


def recent_bhavcopies(n_trading_days: int, session=None) -> list[pd.DataFrame]:
    """Walk back from today collecting the last `n_trading_days` published
    bhavcopies. Weekends/holidays are simply absent (404) and skipped."""
    session = session or _session()
    frames: list[pd.DataFrame] = []
    d = date.today()
    misses = 0
    # Look back generously; stop after too many consecutive misses (long holiday).
    max_lookback = n_trading_days * 3 + 20
    for _ in range(max_lookback):
        df = download_bhavcopy(d, session)
        if df is not None and not df.empty:
            frames.append(df)
            misses = 0
            if len(frames) >= n_trading_days:
                break
        else:
            misses += 1
        d -= timedelta(days=1)
    frames.sort(key=lambda f: f["date"].iloc[0])
    if frames:
        log.info("Loaded %d bhavcopies (%s .. %s)",
                 len(frames), frames[0]["date"].iloc[0], frames[-1]["date"].iloc[0])
    return frames


def load_cached_history_long() -> pd.DataFrame:
    """Concatenate every cached bhavcopy into one long OHLCV frame.

    Used by the dashboard's per-stock detail page. The window grows on its own
    as more daily bhavcopies accumulate in the cache."""
    files = sorted(f for f in os.listdir(BHAV_DIR) if f.startswith("bhav_") and f.endswith(".csv"))
    frames = []
    for f in files:
        try:
            frames.append(pd.read_csv(os.path.join(BHAV_DIR, f)))
        except Exception:
            continue
    if not frames:
        return pd.DataFrame()
    big = pd.concat(frames, ignore_index=True)
    big["date"] = pd.to_datetime(big["date"])
    return big.sort_values(["symbol", "date"]).reset_index(drop=True)


def build_history_frames(n_trading_days: int) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """Assemble per-symbol OHLCV history from recent bhavcopies.

    Returns ({symbol: DataFrame indexed by date}, {symbol: company name})."""
    frames = recent_bhavcopies(n_trading_days)
    if not frames:
        raise RuntimeError("Could not download any bhavcopy — check network / NSE availability.")
    big = pd.concat(frames, ignore_index=True)
    names = big.groupby("symbol")["name"].last().to_dict()
    big["date"] = pd.to_datetime(big["date"])
    hist: dict[str, pd.DataFrame] = {}
    for sym, g in big.groupby("symbol"):
        g = g.sort_values("date").set_index("date")
        hist[sym] = g[["open", "high", "low", "close", "volume"]].rename(
            columns={"open": "Open", "high": "High", "low": "Low",
                     "close": "Close", "volume": "Volume"}
        )
    return hist, names


# ----------------------------------------------------------------------------
# Official 52-week high / low report
# ----------------------------------------------------------------------------
def fetch_52wk_high_low(session=None) -> dict[str, tuple[float, float]]:
    """Return {symbol: (high_52w, low_52w)} from NSE's official adjusted report.
    Empty dict if unavailable (caller falls back to computing from history)."""
    session = session or _session()
    d = date.today()
    for _ in range(8):
        url = WK52_URL.format(ddmmyyyy=d.strftime("%d%m%Y"))
        cache = os.path.join(REPORT_DIR, f"wk52_{d.isoformat()}.csv")
        text = None
        if os.path.exists(cache):
            with open(cache) as fh:
                text = fh.read()
        else:
            r = _get(session, url)
            if r is not None:
                text = r.text
                with open(cache, "w") as fh:
                    fh.write(text)
        if text:
            parsed = _parse_52wk(text)
            if parsed:
                log.info("52-week high/low report loaded for %s (%d symbols)", d, len(parsed))
                return parsed
        d -= timedelta(days=1)
    log.warning("52-week report unavailable; will compute from history instead.")
    return {}


def _parse_52wk(text: str) -> dict[str, tuple[float, float]]:
    lines = text.splitlines()
    # The file starts with a disclaimer line; find the real header row.
    header_idx = next((i for i, ln in enumerate(lines)
                       if "SYMBOL" in ln.upper() and ("HIGH" in ln.upper())), None)
    if header_idx is None:
        return {}
    try:
        df = pd.read_csv(io.StringIO("\n".join(lines[header_idx:])))
    except Exception:
        return {}
    df.columns = [c.strip() for c in df.columns]
    cols = {c.upper(): c for c in df.columns}
    sym_col = cols.get("SYMBOL")
    # Prefer adjusted columns; fall back to any high/low column.
    high_col = next((cols[k] for k in cols if "HIGH" in k and "ADJUST" in k),
                    next((cols[k] for k in cols if "HIGH" in k), None))
    low_col = next((cols[k] for k in cols if "LOW" in k and "ADJUST" in k),
                   next((cols[k] for k in cols if "LOW" in k), None))
    if not (sym_col and high_col and low_col):
        return {}
    out: dict[str, tuple[float, float]] = {}
    for _, row in df.iterrows():
        try:
            sym = str(row[sym_col]).strip()
            hi = float(row[high_col]); lo = float(row[low_col])
            if sym and hi > 0 and lo > 0:
                out[sym] = (hi, lo)
        except Exception:
            continue
    return out


# ----------------------------------------------------------------------------
# Market cap (only for Stage-2 survivors; official NSE first, yfinance fallback)
# ----------------------------------------------------------------------------
def fetch_market_caps(symbols: list[str]) -> dict[str, tuple[float | None, float | None]]:
    """Return {symbol: (market_cap_inr, shares)} for a SMALL set of symbols.

    Tries NSE's official quote API (works from Indian/residential IPs), then
    falls back to yfinance. Both are per-ticker but the set is tiny, so no
    rate-limit risk."""
    result: dict[str, tuple] = {}
    if not symbols:
        return result

    # 0) Bulletproof override: a user-supplied CSV (symbol,market_cap_cr) is
    #    authoritative and needs no network at all. Works from any machine.
    override = _load_mcap_override()
    remaining = []
    for s in symbols:
        if s in override:
            result[s] = (override[s] * config.CRORE, None)
        else:
            remaining.append(s)
    if override:
        log.info("Market cap: %d/%d from override CSV", len(symbols) - len(remaining), len(symbols))
    symbols = remaining
    if not symbols:
        return result

    session = _session()
    # Prime NSE session cookies (best-effort).
    try:
        session.get("https://www.nseindia.com/get-quotes/equity?symbol=RELIANCE", timeout=15)
    except Exception:
        pass

    nse_ok = 0
    for i, sym in enumerate(symbols, 1):
        mc, shares = _nse_market_cap(session, sym)
        if mc:
            nse_ok += 1
        result[sym] = (mc, shares)
        if i % 25 == 0:
            log.info("  market cap %d/%d (nse ok: %d)", i, len(symbols), nse_ok)
        time.sleep(0.4)  # polite

    # Fallback via yfinance for any that NSE couldn't provide.
    missing = [s for s, (mc, _) in result.items() if not mc]
    if missing:
        log.info("Market cap: %d/%d via NSE; trying yfinance fallback for %d",
                 nse_ok, len(symbols), len(missing))
        _yf_fallback(missing, result)
    else:
        log.info("Market cap: all %d via official NSE", nse_ok)
    return result


def _load_mcap_override() -> dict[str, float]:
    """Optional data/market_cap_override.csv with columns symbol,market_cap_cr."""
    path = os.path.join(config.DATA_DIR, "market_cap_override.csv")
    if not os.path.exists(path):
        return {}
    try:
        df = pd.read_csv(path)
        df.columns = [c.strip().lower() for c in df.columns]
        sym = "symbol"
        cap = "market_cap_cr" if "market_cap_cr" in df.columns else df.columns[1]
        return {str(r[sym]).strip().upper(): float(r[cap])
                for _, r in df.iterrows() if pd.notna(r[cap])}
    except Exception as e:
        log.warning("Could not read market_cap_override.csv: %s", e)
        return {}


def load_shares_outstanding() -> dict[str, float]:
    """Optional data/shares_outstanding.csv (columns: symbol,shares).

    Shares outstanding change rarely (only splits/bonus/buyback/new issues), so
    this file stays valid for months. Market cap is then computed DAILY and
    locally as shares x today's bhavcopy close — no network, works from any IP,
    for the WHOLE universe. Seed/refresh it with seed_market_cap.py."""
    path = os.path.join(config.DATA_DIR, "shares_outstanding.csv")
    if not os.path.exists(path):
        return {}
    try:
        df = pd.read_csv(path)
        df.columns = [c.strip().lower() for c in df.columns]
        col = "shares" if "shares" in df.columns else df.columns[1]
        return {str(r["symbol"]).strip().upper(): float(r[col])
                for _, r in df.iterrows() if pd.notna(r[col]) and float(r[col]) > 0}
    except Exception as e:
        log.warning("Could not read shares_outstanding.csv: %s", e)
        return {}


def _nse_market_cap(session, symbol: str) -> tuple[float | None, float | None]:
    url = f"https://www.nseindia.com/api/quote-equity?symbol={requests.utils.quote(symbol)}&section=trade_info"
    try:
        r = session.get(url, timeout=15)
        if r.status_code != 200:
            return None, None
        d = r.json()
        ti = d.get("marketDeptOrderBook", {}).get("tradeInfo", {})
        mc = ti.get("totalMarketCap")  # NSE reports this in ₹ lakh
        if mc:
            mc_inr = float(mc) * 1e5  # lakh -> rupees
            return mc_inr, None
    except Exception:
        pass
    return None, None


def _yf_fallback(symbols: list[str], result: dict) -> None:
    try:
        import yfinance as yf
    except Exception:
        return
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def one(sym):
        try:
            fi = yf.Ticker(sym + config.YF_SUFFIX).fast_info
            mc = fi.get("market_cap") if hasattr(fi, "get") else fi["market_cap"]
            return sym, mc
        except Exception:
            return sym, None

    with ThreadPoolExecutor(max_workers=config.MAX_MCAP_WORKERS) as ex:
        for fut in as_completed({ex.submit(one, s): s for s in symbols}):
            sym, mc = fut.result()
            if mc:
                result[sym] = (float(mc), None)
