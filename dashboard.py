#!/usr/bin/env python3
"""
Streamlit dashboard for the NSE equity screener.

Visualises the funnel as a pipeline with one tab per stage:
    Overview | Universe (raw data) | Stage 1 | Stage 2 | Stage 3 | Final Picks

Run with:  streamlit run dashboard.py
"""
from __future__ import annotations

import os
import sqlite3

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

import config
import nse_source

st.set_page_config(page_title="NSE Equity Screener", layout="wide", page_icon="📈")


@st.cache_data(ttl=300)
def load_data():
    if not os.path.exists(config.DB_PATH):
        return None, None
    conn = sqlite3.connect(config.DB_PATH)
    try:
        metrics = pd.read_sql("SELECT * FROM metrics", conn)
        try:
            meta = pd.read_sql("SELECT * FROM run_meta", conn).iloc[0].to_dict()
        except Exception:
            meta = {}
    finally:
        conn.close()
    # Normalise boolean-ish columns coming back from SQLite as 0/1.
    for col in ("stage1_pass", "stage2_pass", "stage3_pass", "final_pass", "near_52w_low"):
        if col in metrics:
            metrics[col] = metrics[col].fillna(0).astype(bool)
    return metrics, meta


def sparkline_df(df: pd.DataFrame) -> pd.DataFrame:
    """Turn the stored 'last5_closes' string into a list column for st line charts."""
    if "last5_closes" in df:
        df = df.copy()
        df["last_5_days"] = df["last5_closes"].apply(
            lambda s: [float(x) for x in str(s).split(",")] if pd.notna(s) and s else []
        )
    return df


DISPLAY_COLS = [
    "symbol", "name", "last_price", "pct_change_1d", "up_days_last5",
    "high_52w", "low_52w", "pct_from_52w_high", "pct_from_52w_low",
    "vol_daily", "vol_weekly_avg", "vol_monthly_avg", "vol_vs_weekly_ratio",
    "market_cap_cr", "last_5_days",
]

COL_CONFIG = {
    "open": st.column_config.LinkColumn("Open", display_text="view ↗", width="small"),
    "last_5_days": st.column_config.LineChartColumn("Last 5 days", width="small"),
    "last_price": st.column_config.NumberColumn("Price ₹", format="%.2f"),
    "pct_change_1d": st.column_config.NumberColumn("1d %", format="%.2f%%"),
    "pct_from_52w_high": st.column_config.NumberColumn("vs 52w high %", format="%.2f%%"),
    "pct_from_52w_low": st.column_config.NumberColumn("above 52w low %", format="%.2f%%"),
    "vol_daily": st.column_config.NumberColumn("Vol (today)", format="%d"),
    "vol_weekly_avg": st.column_config.NumberColumn("Vol wk avg", format="%d"),
    "vol_monthly_avg": st.column_config.NumberColumn("Vol mo avg", format="%d"),
    "vol_vs_weekly_ratio": st.column_config.NumberColumn("Vol/wk-avg", format="%.2fx"),
    "market_cap_cr": st.column_config.NumberColumn("Mkt cap (₹ cr)", format="%.0f"),
    "up_days_last5": st.column_config.NumberColumn("Up days (of 5)"),
}


def show_table(df: pd.DataFrame, cols=None):
    df = sparkline_df(df)
    # A clickable link that routes to the per-stock detail page (?symbol=XXX).
    df["open"] = df["symbol"].apply(lambda s: f"?symbol={s}")
    cols = cols or [c for c in DISPLAY_COLS if c in df.columns]
    cols = ["open"] + [c for c in cols if c != "open"]
    st.dataframe(
        df[cols],
        use_container_width=True,
        hide_index=True,
        column_config={k: v for k, v in COL_CONFIG.items() if k in cols},
    )


# ----------------------------------------------------------------------------
# Per-stock detail page
# ----------------------------------------------------------------------------
@st.cache_data(ttl=600)
def load_history_long() -> pd.DataFrame:
    return nse_source.load_cached_history_long()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def render_detail(symbol: str, metrics: pd.DataFrame):
    symbol = symbol.upper()
    st.markdown(f"### 📄 {symbol}")
    row = metrics[metrics["symbol"] == symbol]
    row = row.iloc[0].to_dict() if not row.empty else {}

    top = st.columns([1, 6])
    if top[0].button("← Back to screener"):
        st.session_state["stock_picker"] = ""
        st.query_params.clear()
        st.rerun()
    if row.get("name"):
        top[1].markdown(f"**{row['name']}**")

    hist = load_history_long()
    h = hist[hist["symbol"] == symbol].sort_values("date") if not hist.empty else pd.DataFrame()

    # --- Headline metrics ---
    if row:
        m = st.columns(5)
        m[0].metric("Last price ₹", f"{row.get('last_price', float('nan')):,.2f}",
                    f"{row.get('pct_change_1d', 0):+.2f}%")
        m[1].metric("52w high", f"{row.get('high_52w', float('nan')):,.2f}",
                    f"{row.get('pct_from_52w_high', 0):+.2f}%")
        m[2].metric("52w low", f"{row.get('low_52w', float('nan')):,.2f}",
                    f"{row.get('pct_from_52w_low', 0):+.2f}% above")
        m[3].metric("Vol vs wk avg", f"{row.get('vol_vs_weekly_ratio', float('nan'))}x")
        mc = row.get("market_cap_cr")
        m[4].metric("Mkt cap (₹ cr)", f"{mc:,.0f}" if pd.notna(mc) else "—")

        badges = []
        badges.append("🟢 Up 4/5" if row.get("stage1_pass") else "⚪ Momentum")
        badges.append("🟢 Vol>wk avg" if row.get("stage2_pass") else "⚪ Volume")
        badges.append("🟢 >10k cr" if row.get("stage3_pass") else "⚪ Mkt cap")
        if row.get("final_pass"):
            badges.append("✅ FINAL PICK")
        if row.get("near_52w_low"):
            badges.append("📉 Near 52w low")
        st.write(" · ".join(badges))

    if h.empty or len(h) < 2:
        st.info("Not enough cached price history yet to draw a chart. It grows as daily "
                "bhavcopies accumulate — run `python data_fetch.py` on more trading days.")
        return

    # --- Price + volume chart with moving-average overlays ---
    h = h.copy()
    h["MA5"] = h["close"].rolling(5).mean()
    h["MA20"] = h["close"].rolling(20).mean()

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03,
                        row_heights=[0.72, 0.28],
                        subplot_titles=(f"{symbol} — price", "Volume"))
    fig.add_trace(go.Candlestick(x=h["date"], open=h["open"], high=h["high"],
                                 low=h["low"], close=h["close"], name="OHLC",
                                 increasing_line_color="#16a34a", decreasing_line_color="#dc2626"),
                  row=1, col=1)
    fig.add_trace(go.Scatter(x=h["date"], y=h["MA5"], name="MA5",
                             line=dict(color="#2563eb", width=1.3)), row=1, col=1)
    fig.add_trace(go.Scatter(x=h["date"], y=h["MA20"], name="MA20",
                             line=dict(color="#f59e0b", width=1.3)), row=1, col=1)

    # Visual indicator: 52-week low line + the "within X% of low" band.
    lo = row.get("low_52w")
    hi = row.get("high_52w")
    pct = float(config.NEAR_52W_LOW_PCT)
    if lo and pd.notna(lo):
        fig.add_hline(y=lo, line=dict(color="#dc2626", width=1, dash="dot"),
                      annotation_text="52w low", row=1, col=1)
        fig.add_hrect(y0=lo, y1=lo * (1 + pct / 100), line_width=0,
                      fillcolor="#dc2626", opacity=0.08, row=1, col=1)
    if hi and pd.notna(hi):
        fig.add_hline(y=hi, line=dict(color="#16a34a", width=1, dash="dot"),
                      annotation_text="52w high", row=1, col=1)

    vol_colors = np.where(h["close"] >= h["open"], "#16a34a", "#dc2626")
    fig.add_trace(go.Bar(x=h["date"], y=h["volume"], name="Volume",
                         marker_color=vol_colors), row=2, col=1)
    fig.update_layout(height=620, margin=dict(l=10, r=10, t=40, b=10),
                      xaxis_rangeslider_visible=False, legend=dict(orientation="h"),
                      template="plotly_white")
    st.plotly_chart(fig, use_container_width=True)

    # --- Technical signals (further analysis; extend here) ---
    st.subheader("📊 Technical signals")
    close = h["close"]
    tcols = st.columns(4)
    ma5, ma20 = h["MA5"].iloc[-1], h["MA20"].iloc[-1]
    last = close.iloc[-1]
    tcols[0].metric("Price vs MA5", "above" if last >= ma5 else "below" if pd.notna(ma5) else "—")
    tcols[1].metric("Price vs MA20", "above" if pd.notna(ma20) and last >= ma20 else
                    "below" if pd.notna(ma20) else "—")
    if len(close) >= 15:
        rsi = _rsi(close).iloc[-1]
        state = "overbought" if rsi >= 70 else "oversold" if rsi <= 30 else "neutral"
        tcols[2].metric("RSI(14)", f"{rsi:.0f}", state)
    else:
        tcols[2].metric("RSI(14)", "—", "need 15+ days")
    ret = (last / close.iloc[0] - 1) * 100
    tcols[3].metric(f"Return ({len(close)}d cached)", f"{ret:+.1f}%")

    # --- Extensible deeper-analysis area ---
    with st.expander("📚 Further analysis (order book, delivery %, fundamentals)"):
        st.markdown(
            "Placeholders wired for extension:\n"
            "- **Order-book depth / bid-ask** — live, from NSE quote API (`section=trade_info`); "
            "needs a residential IP.\n"
            "- **Delivery %** — from NSE's `sec_bhavdata_full` report (per day).\n"
            "- **Fundamentals** (P/E, book value) — from a fundamentals feed.\n\n"
            "Add each as a function returning a DataFrame and render it here."
        )
    with st.expander("🔎 Raw stored metrics"):
        if row:
            st.dataframe(pd.DataFrame([row]).T.rename(columns={0: "value"}),
                         use_container_width=True)


# ----------------------------------------------------------------------------
metrics, meta = load_data()

st.title("📈 NSE Equity Screener — Pipeline Dashboard")

if metrics is None or metrics.empty:
    st.warning("No data yet. Run `python data_fetch.py` first to populate the database.")
    st.stop()

if st.button("🔄 Reload data"):
    st.cache_data.clear()
    st.rerun()

# Sidebar: jump straight to any stock's detail page.
_sel = st.sidebar.selectbox("🔍 Open a stock", [""] + sorted(metrics["symbol"].tolist()),
                            index=0, key="stock_picker")
if _sel:
    st.query_params["symbol"] = _sel

# Route: if a symbol is selected, show its detail page instead of the funnel.
_symbol = st.query_params.get("symbol")
if _symbol:
    render_detail(_symbol, metrics)
    st.stop()

if meta:
    c = st.columns(7)
    c[0].metric("Universe", meta.get("universe_count", "—"))
    c[1].metric("With data", meta.get("with_data", "—"))
    c[2].metric(f"Stage 1 (up {meta.get('up_days_required','4')}/{meta.get('up_days_window','5')})",
                meta.get("stage1_count", "—"))
    c[3].metric("Stage 2 (volume)", meta.get("stage2_count", "—"))
    c[4].metric(f"Final (>{meta.get('mcap_threshold_cr','10000')}cr)", meta.get("final_count", "—"))
    c[5].metric(f"Near 52w low (≤{meta.get('near_low_pct','5')}%)", meta.get("near_52w_low_count", "—"))
    c[6].metric("Last run", str(meta.get("run_at", "—"))[:16])
    st.caption(f"Data source: {meta.get('data_source', 'NSE')}")

    # Market-cap freshness / coverage guard.
    cov = meta.get("mcap_coverage_pct")
    covered = meta.get("mcap_covered")
    age = meta.get("mcap_seed_age_days")
    if cov is not None:
        msg = f"**Market cap:** {covered}/{meta.get('with_data','?')} stocks covered ({cov}%)."
        if age is None:
            st.warning(msg + " No seed file found — run `python seed_market_cap.py` "
                       "on your Mac to populate `data/shares_outstanding.csv`.")
        elif age > 90:
            st.warning(msg + f" Seed is **{age:.0f} days old** — shares outstanding may be "
                       "stale; re-run `python seed_market_cap.py` (quarterly refresh).")
        elif cov < 60:
            st.warning(msg + " Low coverage — re-seed to cover more of the universe.")
        else:
            st.caption(msg + f" Seed age: {age:.0f}d.")

s1 = metrics[metrics["stage1_pass"]]
s2 = s1[s1["stage2_pass"]]
s3 = s2[s2["stage3_pass"]] if "stage3_pass" in s2 else s2
final = metrics[metrics["final_pass"]]
near_low = metrics[metrics["near_52w_low"]] if "near_52w_low" in metrics else metrics.iloc[0:0]

tabs = st.tabs([
    "🏁 Overview",
    f"🌐 Universe ({len(metrics)})",
    f"1️⃣ Momentum · up 4/5 ({len(s1)})",
    f"2️⃣ Volume · > wk avg ({len(s2)})",
    f"3️⃣ Market cap · >10k cr ({len(s3)})",
    f"✅ Final picks ({len(final)})",
    f"📉 Near 52w low ({len(near_low)})",
])

# --- Overview: the funnel ---
with tabs[0]:
    st.subheader("Screening funnel")
    funnel = pd.DataFrame({
        "Stage": ["Universe (with data)", "1 · Up 4/5 days",
                  "2 · Volume > weekly avg", "3 · Mkt cap > 10k cr (Final)"],
        "Stocks": [len(metrics), len(s1), len(s2), len(final)],
    })
    st.bar_chart(funnel.set_index("Stage"), horizontal=True)
    st.dataframe(funnel, hide_index=True, use_container_width=True)
    st.markdown(
        """
        **Pipeline rules**
        1. **Momentum** — closed *up* on at least **4 of the last 5** trading days.
        2. **Volume** — **today's volume > the weekly average** (mean daily volume of the prior 5 days).
        3. **Market cap** — market capitalisation **> ₹10,000 crore**.

        A stock is a **final pick** only if it passes all three. Market cap is fetched
        only for Stage-2 survivors, so the *Universe* tab may show blank market caps.
        """
    )

with tabs[1]:
    st.subheader("All active equities with fetched data")
    st.caption("Every metric requested: price, 52w high/low, daily/weekly/monthly volume, "
               "weekly & monthly averages, last 5 days of prices.")
    q = st.text_input("Filter by symbol or name", key="u_q").strip().lower()
    view = metrics
    if q:
        view = view[view["symbol"].str.lower().str.contains(q) |
                     view["name"].str.lower().str.contains(q)]
    show_table(view)

with tabs[2]:
    st.subheader("Stage 1 — increased on 4 of the last 5 days")
    show_table(s1.sort_values("up_days_last5", ascending=False))

with tabs[3]:
    st.subheader("Stage 2 — current volume greater than weekly average")
    show_table(s2.sort_values("vol_vs_weekly_ratio", ascending=False))

with tabs[4]:
    st.subheader("Stage 3 — market capitalisation greater than ₹10,000 crore")
    show_table(s3.sort_values("market_cap_cr", ascending=False))

with tabs[5]:
    st.subheader("✅ Final picks — passed all three filters")
    if final.empty:
        st.info("No stocks passed all three filters in the latest run.")
    else:
        show_table(final.sort_values("market_cap_cr", ascending=False))
        st.download_button(
            "⬇️ Download final picks (CSV)",
            final.to_csv(index=False).encode(),
            file_name="final_picks.csv",
            mime="text/csv",
        )

with tabs[6]:
    pct = meta.get("near_low_pct", 5) if meta else 5
    st.subheader(f"📉 Trading within {pct}% of their 52-week low")
    st.caption("A separate watchlist (independent of the momentum/volume/market-cap funnel) — "
               "stocks whose latest price is at most this far above their official 52-week low.")
    if near_low.empty:
        st.info("No stocks are within range of their 52-week low in the latest run.")
    else:
        show_table(near_low.sort_values("pct_from_52w_low"))
        st.download_button(
            "⬇️ Download near-52w-low list (CSV)",
            near_low.to_csv(index=False).encode(),
            file_name="near_52w_low.csv",
            mime="text/csv",
        )
