"""
Dhan broker adapter — optional, token-authenticated data source + execution.

Why Dhan: free API, a 30-day access token (no daily TOTP login, ideal for an
unattended job), a PUBLIC scrip-master CSV (no auth) for instrument mapping, and
clean order placement for when you want to go live.

What is tested from any machine (no token needed):
  * load_equity_map()  — symbol -> Dhan securityId, from the public scrip master.

What needs YOUR token (set up once, see data/broker_creds.env.example):
  * historical_ohlc()  — daily candles for one security.
  * ltp()              — live last-traded price for a batch (intraday).
  * build_history_frames() — drop-in replacement for nse_source's function.
  * place_order()      — DRY-RUN by default; never fires without explicit opt-in.

Design: this MODULE never runs automatically. data_fetch.py defaults to the
efficient NSE bhavcopy (one bulk file); pass `--source dhan` to use this instead
(it falls back to bhavcopy on any failure). Bhavcopy stays the tested backbone.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import date, timedelta

import pandas as pd
import requests

import config

log = logging.getLogger("screener.dhan")

SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
API_BASE = "https://api.dhan.co/v2"
CREDS_FILE = os.path.join(config.DATA_DIR, "broker_creds.env")
SCRIP_CACHE = os.path.join(config.CACHE_DIR, "dhan_scrip_master.csv")
EXCHANGE_SEGMENT = "NSE_EQ"


# ----------------------------------------------------------------------------
# Credentials
# ----------------------------------------------------------------------------
def _creds() -> tuple[str, str] | None:
    """Return (client_id, access_token) from env or data/broker_creds.env, else None."""
    cid = os.environ.get("DHAN_CLIENT_ID")
    tok = os.environ.get("DHAN_ACCESS_TOKEN")
    if (not cid or not tok) and os.path.exists(CREDS_FILE):
        vals = {}
        with open(CREDS_FILE) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    vals[k.strip()] = v.strip().strip('"').strip("'")
        cid = cid or vals.get("DHAN_CLIENT_ID")
        tok = tok or vals.get("DHAN_ACCESS_TOKEN")
    if cid and tok:
        return cid, tok
    return None


def is_enabled() -> bool:
    return _creds() is not None


def _headers() -> dict:
    creds = _creds()
    if not creds:
        raise RuntimeError("Dhan credentials not set. See data/broker_creds.env.example")
    cid, tok = creds
    return {"access-token": tok, "client-id": cid,
            "Content-Type": "application/json", "Accept": "application/json"}


# ----------------------------------------------------------------------------
# Instrument map (public, no token) — TESTED
# ----------------------------------------------------------------------------
def load_equity_map(refresh: bool = False) -> tuple[dict[str, int], dict[str, str]]:
    """Return ({symbol: security_id}, {symbol: name}) for NSE EQ-series equities."""
    if refresh or not os.path.exists(SCRIP_CACHE):
        # ~33 MB file — stream to disk with requests (pandas/urllib truncates it).
        os.makedirs(os.path.dirname(SCRIP_CACHE), exist_ok=True)
        with requests.get(SCRIP_MASTER_URL, stream=True, timeout=120) as r:
            r.raise_for_status()
            tmp = SCRIP_CACHE + ".tmp"
            with open(tmp, "wb") as fh:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    fh.write(chunk)
            os.replace(tmp, SCRIP_CACHE)
    df = pd.read_csv(SCRIP_CACHE, low_memory=False)
    eq = df[(df["EXCH_ID"] == "NSE") & (df["SEGMENT"] == "E") &
            (df["INSTRUMENT"] == "EQUITY") & (df["SERIES"] == "EQ")]
    sym = eq["UNDERLYING_SYMBOL"].astype(str).str.strip()
    id_map = dict(zip(sym, eq["SECURITY_ID"].astype(int)))
    name_map = dict(zip(sym, eq["SYMBOL_NAME"].astype(str).str.strip()))
    log.info("Dhan scrip master: %d NSE EQ equities mapped", len(id_map))
    return id_map, name_map


# ----------------------------------------------------------------------------
# Historical candles (needs token)
# ----------------------------------------------------------------------------
def historical_ohlc(security_id: int, from_date: str, to_date: str) -> pd.DataFrame | None:
    """Daily OHLCV for one security via Dhan /charts/historical."""
    body = {
        "securityId": str(security_id),
        "exchangeSegment": EXCHANGE_SEGMENT,
        "instrument": "EQUITY",
        "fromDate": from_date,
        "toDate": to_date,
    }
    try:
        r = requests.post(f"{API_BASE}/charts/historical", json=body,
                          headers=_headers(), timeout=30)
        if r.status_code != 200:
            log.debug("historical %s -> HTTP %s", security_id, r.status_code)
            return None
        d = r.json()
        if not d.get("timestamp"):
            return None
        df = pd.DataFrame({
            "date": pd.to_datetime(d["timestamp"], unit="s"),
            "Open": d["open"], "High": d["high"], "Low": d["low"],
            "Close": d["close"], "Volume": d["volume"],
        }).set_index("date")
        return df
    except Exception as e:
        log.debug("historical %s error: %s", security_id, e)
        return None


def build_history_frames(n_trading_days: int) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """Drop-in replacement for nse_source.build_history_frames using Dhan.
    Per-symbol calls (token-authenticated, so no IP blocking). Throttled."""
    id_map, name_map = load_equity_map()
    start = (date.today() - timedelta(days=n_trading_days * 3 + 15)).isoformat()
    end = date.today().isoformat()
    hist: dict[str, pd.DataFrame] = {}
    for i, (sym, sid) in enumerate(id_map.items(), 1):
        df = historical_ohlc(sid, start, end)
        if df is not None and len(df) >= 6:
            hist[sym] = df.tail(n_trading_days)
        if i % 100 == 0:
            log.info("  Dhan history %d/%d", i, len(id_map))
        time.sleep(0.2)  # respect rate limits
    return hist, {s: name_map.get(s, s) for s in hist}


# ----------------------------------------------------------------------------
# Live quotes (needs token) — for intraday "current price/volume" refresh
# ----------------------------------------------------------------------------
def ltp(symbols: list[str]) -> dict[str, float]:
    """Last-traded price for a batch of symbols via Dhan /marketfeed/ltp."""
    id_map, _ = load_equity_map()
    sids = [id_map[s] for s in symbols if s in id_map]
    if not sids:
        return {}
    body = {EXCHANGE_SEGMENT: sids}
    try:
        r = requests.post(f"{API_BASE}/marketfeed/ltp", json=body,
                          headers=_headers(), timeout=20)
        if r.status_code != 200:
            return {}
        data = r.json().get("data", {}).get(EXCHANGE_SEGMENT, {})
        rev = {v: k for k, v in id_map.items()}
        return {rev[int(sid)]: info.get("last_price")
                for sid, info in data.items() if int(sid) in rev}
    except Exception as e:
        log.debug("ltp error: %s", e)
        return {}


# ----------------------------------------------------------------------------
# Order placement (needs token) — SAFE: dry-run unless explicitly confirmed
# ----------------------------------------------------------------------------
def place_order(symbol: str, qty: int, side: str = "BUY",
                order_type: str = "MARKET", price: float = 0.0,
                product: str = "CNC", dry_run: bool = True) -> dict:
    """Place an equity order. DRY-RUN by default — returns the payload without
    sending. Pass dry_run=False to actually transmit (do this consciously)."""
    id_map, _ = load_equity_map()
    if symbol not in id_map:
        return {"error": f"unknown symbol {symbol}"}
    creds = _creds()
    payload = {
        "dhanClientId": creds[0] if creds else None,
        "transactionType": side.upper(),
        "exchangeSegment": EXCHANGE_SEGMENT,
        "productType": product,
        "orderType": order_type.upper(),
        "validity": "DAY",
        "securityId": str(id_map[symbol]),
        "quantity": int(qty),
        "price": float(price),
    }
    if dry_run:
        return {"dry_run": True, "would_send": payload}
    r = requests.post(f"{API_BASE}/orders", json=payload, headers=_headers(), timeout=20)
    return {"status_code": r.status_code, "response": r.json() if r.content else None}
