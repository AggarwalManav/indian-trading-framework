# NSE Equity Screener — Pipeline Framework

Automated daily screening of **all active NSE equities** through a 3-stage
funnel, with a Streamlit dashboard that shows each stage as its own tab.

## The screen

| Stage | Rule |
|-------|------|
| **1 · Momentum** | Closed **up on ≥ 4 of the last 5** trading days |
| **2 · Volume**   | **Today's volume > the weekly average** (mean daily volume of the prior 5 days) |
| **3 · Market cap** | Market capitalisation **> ₹10,000 crore** |

A stock is a **final pick** only if it passes all three.

Every metric you asked for is fetched and stored: last price, 52-week high/low,
daily/weekly/monthly volume + weekly & monthly **average** volume, and the
**last 5 days** of closing prices.

## Files

| File | Purpose |
|------|---------|
| `config.py`      | All thresholds, paths, windows (edit rules here) |
| `pipeline.py`    | Pure metric + stage logic (unit-tested) |
| `data_fetch.py`  | Universe load → batch price download → screen → SQLite snapshot |
| `dashboard.py`   | Streamlit dashboard, one tab per stage |
| `test_pipeline.py` | Unit tests for the screening rules |
| `run_daily.sh`   | Wrapper for the scheduled daily refresh |
| `com.manav.tradingframework.plist` | macOS launchd job (daily) |

## Setup

```bash
cd ~/indian-trading-framework
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
# Sanity check (no network):
python test_pipeline.py

# Quick live test on the first 50 symbols:
python data_fetch.py --limit 50

# Full daily run (all active equities):
python data_fetch.py

# Launch the dashboard:
streamlit run dashboard.py
```

The full run takes a few minutes (batch price download for ~1,800 symbols, then
market-cap lookups only for Stage-2 survivors). Output lands in
`data/screener.db` plus `data/all_metrics.csv` and `data/final_picks.csv`.

## Daily automation (macOS)

```bash
chmod +x run_daily.sh
cp com.manav.tradingframework.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.manav.tradingframework.plist
```

Runs at 18:00 local time by default (after the 15:30 IST close). Edit `Hour`/`Minute`
in the plist for your timezone. Cron alternative:

```
0 18 * * 1-5  /Users/manavaggarwal/indian-trading-framework/run_daily.sh
```

## Per-stock detail page

Click **"view ↗"** on any row (or use the sidebar **🔍 Open a stock** picker) to
open that stock's own page: a candlestick + volume chart with MA5/MA20 overlays
and a shaded 52-week-low band, headline metrics + stage badges, a technical-signals
panel (price vs MAs, RSI, period return), and an extensible "Further analysis"
section (order book, delivery %, fundamentals). The chart history grows on its own
as more daily bhavcopies accumulate in `data/cache/bhav/`.

## Broker integration (Dhan) — optional

`broker_dhan.py` adds a pluggable [Dhan](https://dhan.co) adapter. It's **off by
default** — the screener runs fully on bhavcopy without it. Dhan was chosen for a
**30-day access token** (no daily TOTP login, so it suits an unattended job) and a
**public scrip-master** for instrument mapping.

| Capability | Needs token? | Status |
|-----------|:---:|--------|
| `load_equity_map()` — symbol → Dhan securityId | no | ✅ tested (2,690 NSE EQ equities) |
| `--source dhan` — daily OHLC from Dhan instead of bhavcopy | yes | falls back to bhavcopy on any failure |
| `ltp()` — live intraday last-traded price | yes | for during-market refresh |
| `place_order()` — order execution | yes | **dry-run by default**; never fires without `dry_run=False` |

**Market cap is NOT available from any broker API** (they serve price/volume/quote
only) — keep using `seed_market_cap.py` / `market_cap_override.csv` for Stage 3.

Setup: copy `data/broker_creds.env.example` → `data/broker_creds.env`, generate a
token at web.dhan.co (DhanHQ Trading APIs), paste in your Client ID + token.
Bhavcopy stays the tested default backbone (one bulk file beats ~2,700 per-ticker
calls for the full-universe daily screen); Dhan complements it for live quotes and
future execution.

## Free cloud deployment (access from anywhere)

The app is built to run free on **Streamlit Community Cloud** (dashboard) +
**GitHub Actions** (daily data refresh). No server, no cost.

**How it works:** GitHub Actions runs `data_fetch.py` after the NSE close each
weekday (`.github/workflows/daily.yml`, 14:30 UTC / 20:00 IST), commits the
refreshed `screener.db` + CSVs back to the repo, and Streamlit Cloud redeploys
automatically. Market cap keeps working in the cloud because it's computed from
the committed `shares_outstanding.csv` × bhavcopy close — no residential IP
needed at run time.

**Deploy steps:**
1. Push this repo to GitHub (see below).
2. Go to [share.streamlit.io](https://share.streamlit.io) → New app → pick this
   repo, main file `dashboard.py`, Python 3.12. Deploy → you get a public URL.
3. The daily workflow is already scheduled; trigger a first run manually from the
   repo's **Actions** tab → *Daily NSE screen* → *Run workflow*.

**Two caveats (honest):**
- **NSE geo-blocking:** GitHub/Streamlit runners are US IPs. NSE *usually* serves
  the bhavcopy archive to them, but can geo-block. If the Action can't download,
  the fallback is to run the refresh on your Mac (the `launchd` job) and let it
  push the DB to GitHub — your Indian IP is never blocked. The committed cache
  means the dashboard always has *some* data even if a refresh is skipped.
- **Never deploy the broker token or order execution publicly.** `broker_creds.env`
  is git-ignored; the hosted app is display + screening only. Keep `place_order`
  for local use.

**Push to GitHub:**
```bash
cd ~/indian-trading-framework
git init && git add . && git commit -m "NSE screener"
gh repo create indian-trading-framework --public --source=. --push
```

## Notes & caveats

- **Data source: official NSE.** Prices/volume come from the daily **UDiFF
  bhavcopy** (one bulk file, no rate limits); 52-week high/low from NSE's official
  adjusted report. No Yahoo scraping for prices. Bhavcopies are cached in
  `data/cache/`, so the daily job only fetches the one new file.
- **Market cap** = *shares outstanding × price*. We already have the price daily
  (bhavcopy close), and shares outstanding change only rarely (splits / bonus /
  buyback / new issues). So run `seed_market_cap.py` **once** on your Mac to write
  `data/shares_outstanding.csv` (`symbol,shares`); every daily run then recomputes
  market cap **locally for the whole universe, with no network call, from any IP**
  (`shares × today's close`). Re-seed quarterly. The NSE quote API (residential-IP
  only) is now just a fallback for survivors whose shares aren't seeded yet. A
  direct `data/market_cap_override.csv` (`symbol,market_cap_cr`) still works too
  and takes top priority, but goes stale as price moves — prefer the shares file.
- Weekly/monthly **averages exclude the current day**, so "volume > weekly
  average" is a genuine "heavier than usual" signal rather than a self-comparison.
- This is a screening tool, not investment advice. Verify picks before trading.
