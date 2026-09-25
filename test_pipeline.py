"""
Unit tests for the pure screening logic. Run with:  python test_pipeline.py
No network needed — uses synthetic price history.
"""
import numpy as np
import pandas as pd

import config
import pipeline


def _make_hist(closes, volumes):
    idx = pd.date_range(end="2026-09-23", periods=len(closes), freq="B")
    return pd.DataFrame({
        "Open": closes, "High": [c * 1.01 for c in closes],
        "Low": [c * 0.99 for c in closes], "Close": closes, "Volume": volumes,
    }, index=idx)


def test_up_days_and_stage1():
    # 300 flat days, then a clean 5-day up-streak -> up_days == 5, passes stage1.
    closes = [100.0] * 300 + [101, 102, 103, 104, 105]
    vols = [1000] * len(closes)
    hist = _make_hist(closes, vols)
    m = pipeline.compute_metrics("TEST", "Test Co", hist)
    assert m is not None
    assert m["up_days_last5"] == 5, m["up_days_last5"]
    assert m["stage1_pass"] is True


def test_stage1_exactly_four():
    closes = [100.0] * 300 + [101, 102, 101.5, 103, 104]  # up,up,down,up,up -> 4 up
    hist = _make_hist(closes, [1000] * len(closes))
    m = pipeline.compute_metrics("T", "T", hist)
    assert m["up_days_last5"] == 4
    assert m["stage1_pass"] is True


def test_stage1_fails_with_three():
    closes = [100.0] * 300 + [101, 100, 101, 100, 101]  # up,down,up,down,up -> 3 up
    hist = _make_hist(closes, [1000] * len(closes))
    m = pipeline.compute_metrics("T", "T", hist)
    assert m["up_days_last5"] == 3
    assert m["stage1_pass"] is False


def test_stage2_volume():
    closes = [100.0] * 306
    vols = [1000] * 305 + [5000]  # today's volume spikes above weekly avg
    hist = _make_hist(closes, vols)
    m = pipeline.compute_metrics("T", "T", hist)
    assert m["vol_daily"] == 5000
    assert m["vol_weekly_avg"] == 1000  # prior 5 days, excludes today
    assert m["stage2_pass"] is True


def test_stage2_volume_fails():
    closes = [100.0] * 306
    vols = [5000] * 305 + [1000]  # today below weekly avg
    hist = _make_hist(closes, vols)
    m = pipeline.compute_metrics("T", "T", hist)
    assert m["stage2_pass"] is False


def test_market_cap_stage3():
    closes = [100.0] * 306
    hist = _make_hist(closes, [1000] * 306)
    m = pipeline.compute_metrics("T", "T", hist)
    # 12,000 crore -> passes; 8,000 crore -> fails
    pipeline.apply_market_cap(m, 12_000 * config.CRORE)
    assert m["market_cap_cr"] == 12_000
    assert m["stage3_pass"] is True
    pipeline.apply_market_cap(m, 8_000 * config.CRORE)
    assert m["stage3_pass"] is False


def test_external_52w_and_near_low():
    # Price sits 3% above an externally supplied 52-week low -> within 5% flag on.
    closes = [100.0] * 306
    hist = _make_hist(closes, [1000] * 306)
    m = pipeline.compute_metrics("T", "T", hist, high_52w=200.0, low_52w=97.0)
    assert m["high_52w"] == 200.0
    assert m["low_52w"] == 97.0
    assert m["pct_from_52w_low"] == round((100 / 97 - 1) * 100, 2)
    assert m["near_52w_low"] is True  # ~3.09% above low, within 5%


def test_not_near_low():
    closes = [100.0] * 306
    hist = _make_hist(closes, [1000] * 306)
    m = pipeline.compute_metrics("T", "T", hist, high_52w=200.0, low_52w=80.0)
    assert m["near_52w_low"] is False  # 25% above the low


def test_insufficient_history():
    hist = _make_hist([100, 101, 102], [1, 2, 3])
    assert pipeline.compute_metrics("T", "T", hist) is None


def test_52w_high_low():
    closes = list(np.linspace(50, 150, 300))  # rising series
    hist = _make_hist(closes, [1000] * 300)
    m = pipeline.compute_metrics("T", "T", hist)
    assert m["low_52w"] < m["high_52w"]
    assert m["last_price"] == round(closes[-1], 2)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
        passed += 1
    print(f"\n{passed}/{len(fns)} tests passed ✅")
