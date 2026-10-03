from __future__ import annotations

import pandas as pd

from core_models import MarketSnapshot, WaveState
from core_senior import SeniorWaveDetector, _rating, _targets


def _h4(rows: list[tuple[float, float, float]], start: str = "2026-01-01T00:00:00Z") -> pd.DataFrame:
    base = pd.Timestamp(start)
    return pd.DataFrame([
        {
            "timestamp": base + pd.Timedelta(hours=4 * i),
            "open": close,
            "high": high,
            "low": low,
            "close": close,
            "volume": 1.0,
            "n": 4,
        }
        for i, (high, low, close) in enumerate(rows)
    ])


def test_gram_global_child_is_promoted_to_nested_w3_2():
    det = SeniorWaveDetector()
    h4 = _h4([
        (1.32, 1.286, 1.31),
        (1.42, 1.31, 1.40),
        (1.55, 1.39, 1.52),
        (1.70, 1.50, 1.68),
        (1.74, 1.66, 1.72),
        (1.70, 1.58, 1.62),
        (1.60, 1.50, 1.53),
        (1.54, 1.460, 1.49),
        (1.53, 1.47, 1.50),
    ])
    parent = {
        "origin": 0.80,
        "origin_ts": h4["timestamp"].iloc[0] - pd.Timedelta(days=20),
        "high": 1.90,
        "high_ts": h4["timestamp"].iloc[0] - pd.Timedelta(days=10),
        "w2_low": 1.286,
        "w2_ts": h4["timestamp"].iloc[0],
        "retrace": 0.55,
        "w2_locked": True,
        "impulse_pct": 1.375,
    }
    child = {
        "origin": 1.286,
        "origin_ts": h4["timestamp"].iloc[0],
        "high": 1.740,
        "high_ts": h4["timestamp"].iloc[4],
        "w2_low": 1.460,
        "w2_ts": h4["timestamp"].iloc[7],
        "retrace": (1.740 - 1.460) / (1.740 - 1.286),
        "w2_locked": False,
        "impulse_pct": 1.740 / 1.286 - 1,
    }
    nested = det._lineage_nested(h4, parent, [parent, child])
    assert nested is not None
    assert abs(nested["origin"] - 1.286) < 1e-12
    assert abs(nested["high"] - 1.740) < 1e-12
    assert abs(nested["low"] - 1.460) < 1e-12
    assert nested["source"] == "GLOBAL_LINEAGE"


def test_bch_micro_subwave_is_ignored_before_senior_w3_1():
    rows = [
        (220, 213, 218),
        (235, 218, 232),
        (244, 226, 238),
        (239, 224, 232),
        (260, 232, 257),
        (285, 252, 282),
        (305, 278, 302),
        (317.7, 303, 315),
        (315, 300, 307),
        (307, 296.10, 301),
        (309, 298, 304),
        (312, 300, 308),
        (314, 304, 311),
        (325, 305, 321),
        (340, 318, 335),
        (330, 296.00, 310),
        (318, 300, 311),
    ]
    h4 = _h4(rows)
    parent = {
        "origin": 180.0,
        "high": 300.0,
        "w2_low": 212.9,
        "w2_ts": h4["timestamp"].iloc[0] - pd.Timedelta(hours=4),
    }
    nested = SeniorWaveDetector()._detect_nested(h4, parent)
    assert nested is not None
    assert abs(nested["high"] - 317.7) < 1e-9
    assert abs(nested["low"] - 296.0) < 1e-9
    assert nested["source"] == "H4_HIERARCHY"


def test_w3_2_tracking_reanchors_after_w3_1_breakout_without_moving_high():
    prev = WaveState(
        symbol="BCH_USDT",
        exchange="mexc_futures",
        wave_type="W3-(2)",
        status="CONFIRMED",
        origin=212.9,
        impulse_high=317.7,
        working_low=296.1,
        strict_origin=212.9,
        parent_w2_low=212.9,
        w3_1_high=317.7,
        impulse_start_ts="2026-09-01T00:00:00+00:00",
        impulse_high_ts="2026-09-10T00:00:00+00:00",
        working_low_ts="2026-09-12T00:00:00+00:00",
        retrace_depth=(317.7 - 296.1) / (317.7 - 212.9),
        current_price=320.0,
        rating=8.8,
        last_complete4h_bucket="2026-09-12T00:00:00+00:00",
    )
    ts = pd.date_range("2026-09-12T04:00:00Z", periods=12, freq="1h")
    hourly = pd.DataFrame({
        "timestamp": ts,
        "open": [320, 322, 325, 327, 315, 310, 304, 300, 301, 302, 305, 308],
        "high": [323, 326, 329, 332, 318, 313, 307, 303, 304, 306, 309, 312],
        "low": [318, 320, 323, 325, 310, 305, 299, 295.5, 297, 299, 302, 305],
        "close": [322, 325, 327, 330, 312, 308, 302, 300, 302, 305, 307, 310],
        "volume": [1.0] * 12,
    })
    snap = MarketSnapshot("BCH_USDT", "mexc_futures", 1.0, 310.0, 305.0, hourly, pd.DataFrame())
    out = SeniorWaveDetector().track(prev, snap, top_n=300)
    assert out.wave_type == "W3-(2)"
    assert out.impulse_high == 317.7
    assert out.w3_1_high == 317.7
    assert out.working_low == 295.5
    expected = _targets(295.5, 317.7 - 212.9)
    assert all(abs(a - b) < 1e-9 for a, b in zip(out.targets, expected, strict=True))


def test_parquet_reference_rating_caps_match_bch_and_gram():
    bch = _rating(
        retrace=(317.70 - 296.10) / (317.70 - 212.90),
        growth_pct=(310.10 / 296.10 - 1.0) * 100.0,
        strict_distance_pct=(296.10 / 212.90 - 1.0) * 100.0,
        fib_status="> .382 · 2/3 C4H",
        liquidity_rank=52,
        top_n=300,
        wave_type="W3-(2)",
        t1_upside_pct=(400.90 / 310.10 - 1.0) * 100.0,
    )
    gram = _rating(
        retrace=(1.740 - 1.460) / (1.740 - 1.286),
        growth_pct=(1.489 / 1.460 - 1.0) * 100.0,
        strict_distance_pct=(1.460 / 1.286 - 1.0) * 100.0,
        fib_status="> .618 · 3/3 C4H",
        liquidity_rank=100,
        top_n=300,
        wave_type="W3-(2)",
        t1_upside_pct=(1.914 / 1.489 - 1.0) * 100.0,
    )
    assert bch == 8.8
    assert gram == 8.4
