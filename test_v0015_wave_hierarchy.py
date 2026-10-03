from __future__ import annotations

import pandas as pd

from core_models import MarketSnapshot, WaveState
from core_senior import SeniorWaveDetector, _rating, _zones


def _bch_like_h4() -> tuple[pd.DataFrame, dict]:
    """Synthetic senior map matching the BCH failure seen in the 01:04 parquet review."""
    base = pd.Timestamp("2026-09-01T00:00:00Z")
    highs = [216, 230, 245, 260, 280, 300, 312, 317.7, 316, 312, 307, 303, 300, 299, 305, 311]
    lows = [210, 220, 235, 250, 270, 290, 303, 311, 307, 303, 299, 296.1, 297, 298, 300, 305]
    closes = [215, 228, 243, 258, 278, 298, 310, 316, 312, 308, 303, 299, 301, 304, 308, 311]
    h4 = pd.DataFrame(
        [
            {
                "timestamp": base + pd.Timedelta(hours=4 * i),
                "open": close - 1,
                "high": high,
                "low": low,
                "close": close,
                "volume": 4.0,
                "n": 4,
            }
            for i, (high, low, close) in enumerate(zip(highs, lows, closes, strict=True))
        ]
    )
    parent = {
        "w2_low": 212.9,
        "w2_ts": base - pd.Timedelta(hours=4),
        "high": 300.0,
    }
    return h4, parent


def test_shallow_but_senior_nested_w3_2_is_not_mislabelled_as_global_w2():
    detector = SeniorWaveDetector()
    h4, parent = _bch_like_h4()

    nested = detector._detect_nested(h4, parent)

    assert nested is not None
    assert nested["high"] == 317.7
    assert nested["low"] == 296.1
    assert 0.20 <= nested["retrace"] < 0.25


def test_bch_like_nested_targets_and_entry_zones_match_active_working_low():
    working_low = 296.1
    w3_1_high = 317.7
    parent_w2 = 212.9

    base, sweep = _zones(working_low, w3_1_high, parent_w2)
    impulse = w3_1_high - parent_w2
    targets = [working_low + m * impulse for m in (1.0, 1.618, 2.618, 4.236)]

    # The old bug projected entry limits back near the parent W2 (~212).  The active
    # retest zone must instead sit next to the confirmed nested low (~296).
    assert base[0] == working_low
    assert 300.0 < base[1] < 303.0
    assert 294.0 < sweep[0] <= working_low
    assert sweep[1] == working_low

    assert abs(targets[0] - 400.9) < 1e-9
    assert abs(targets[1] - 465.6664) < 1e-9
    assert abs(targets[2] - 570.4664) < 1e-9
    assert abs(targets[3] - 740.0328) < 1e-9


def test_nested_fresh_convex_setup_rates_like_a_premium_opportunity():
    rating = _rating(
        retrace=(317.7 - 296.1) / (317.7 - 212.9),
        growth_pct=(311.09 / 296.1 - 1) * 100,
        strict_distance_pct=(296.1 / 212.9 - 1) * 100,
        fib_status="> .236 · 3/3 C4H",
        liquidity_rank=20,
        top_n=300,
        wave_type="W3-(2)",
        t1_upside_pct=(400.9 / 311.09 - 1) * 100,
    )
    assert 9.0 <= rating <= 9.6


def test_same_symbol_accompaniment_can_promote_w2_to_w3_2():
    previous = WaveState(
        symbol="BCHUSDT",
        exchange="binance_spot",
        wave_type="W2",
        status="CONFIRMED",
        origin=180.0,
        impulse_high=300.0,
        working_low=212.9,
        strict_origin=180.0,
        current_price=311.0,
        rating=8.0,
        last_complete4h_bucket=None,
    )
    promoted = WaveState(
        symbol="BCHUSDT",
        exchange="binance_spot",
        wave_type="W3-(2)",
        status="CONFIRMED",
        origin=212.9,
        impulse_high=317.7,
        working_low=296.1,
        strict_origin=212.9,
        parent_w2_low=212.9,
        w3_1_high=317.7,
        current_price=311.0,
        rating=9.3,
    )

    class Detector(SeniorWaveDetector):
        def detect(self, snapshot, liquidity_rank, top_n):  # noqa: ANN001
            return WaveState.from_dict(promoted.to_dict())

    ts = pd.date_range("2026-10-01T00:00:00Z", periods=8, freq="1h")
    h1 = pd.DataFrame(
        {
            "timestamp": ts,
            "open": [305.0] * 8,
            "high": [313.0] * 8,
            "low": [300.0] * 8,
            "close": [311.0] * 8,
            "volume": [1.0] * 8,
        }
    )
    snapshot = MarketSnapshot(
        "BCHUSDT", "binance_spot", 1.0, 311.0, 309.0, h1, pd.DataFrame()
    )

    result = Detector().track(previous, snapshot, 300)

    assert result.wave_type == "W3-(2)"
    assert result.working_low == 296.1
    assert result.last_event == "PROMOTED W2 → W3-(2)"


def test_end_to_end_detector_prefers_active_nested_structure_over_stale_parent_w2():
    import numpy as np

    start = pd.Timestamp("2026-07-01T00:00:00Z")
    anchors = [
        (0, 220.0),
        (10, 180.0),   # global W1 origin
        (20, 300.0),   # global W1 high
        (25, 212.9),   # global W2
        (35, 317.7),   # W3-(1)
        (40, 296.1),   # W3-(2), only ~20.6% retrace of W3-(1)
        (50, 311.0),
        (59, 311.0),
    ]
    hours = 60 * 24
    x = np.arange(hours) / 24
    prices = np.interp(
        x,
        np.array([day for day, _ in anchors], dtype=float),
        np.array([price for _, price in anchors], dtype=float),
    )
    timestamps = pd.date_range(start, periods=hours, freq="1h")
    opens = np.r_[prices[0], prices[:-1]]
    hourly = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": opens,
            "high": np.maximum(opens, prices) + 0.05,
            "low": np.minimum(opens, prices) - 0.05,
            "close": prices,
            "volume": 1.0,
        }
    )
    daily = (
        hourly.set_index("timestamp")
        .resample("1d")
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        )
        .reset_index()
    )
    snapshot = MarketSnapshot(
        "BCHUSDT",
        "binance_spot",
        1_000_000_000.0,
        float(prices[-1]),
        float(hourly["low"].iloc[-1]),
        hourly,
        daily,
    )

    result = SeniorWaveDetector().detect(snapshot, liquidity_rank=20, top_n=300)

    assert result is not None
    assert result.wave_type == "W3-(2)"
    assert 295.0 < result.working_low < 297.0
    assert 212.0 < (result.parent_w2_low or 0) < 214.0
    assert 317.0 < (result.w3_1_high or 0) < 319.0
    assert result.targets[0] > 395.0
    assert result.rating is not None and result.rating >= 9.0
