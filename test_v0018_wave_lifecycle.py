# Historical v0018 synthetic geometry tests. These do NOT verify the named assets against parquet.
# Real archive regressions are in test_v0019_regressions.py.
from __future__ import annotations

import pandas as pd

from core_senior import SeniorWaveDetector, _rating, _targets


def _h4(rows: list[tuple[float, float, float]]) -> pd.DataFrame:
    base = pd.Timestamp("2026-01-01T00:00:00Z")
    return pd.DataFrame(
        [
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
        ]
    )


def test_detector_freezes_first_qualified_senior_w3_1_after_w3_2_exists():
    # Parent W2=100. Once the senior 130 -> 118 correction exists, 130 is the
    # frozen W3-(1) projection high. Later continuation highs are W3 progress,
    # not permission to rewrite the already-established W3-(2) targets.
    rows = [
        (104, 101, 103),
        (109, 103, 108),
        (115, 107, 113),
        (121, 112, 119),
        (126, 118, 124),
        (130, 122, 128),
        (129, 121, 124),
        (126, 118, 120),
        (125, 119, 123),
        (131, 122, 131),  # acceptance above old W3-(1)
        (138, 130, 136),
        (145, 135, 143),
        (151, 141, 149),
        (150, 138, 142),
        (147, 132, 136),
        (146, 129, 133),
        (145, 131, 140),
        (144, 134, 141),
    ]
    frame = _h4(rows)
    parent = {
        "w2_low": 100.0,
        "w2_ts": frame["timestamp"].iloc[0] - pd.Timedelta(hours=4),
        "high": 120.0,
    }
    nested = SeniorWaveDetector()._detect_nested(frame, parent)
    assert nested is not None
    assert nested["high"] == 130.0
    assert nested["low"] == 118.0


def test_bch_reference_targets_use_active_senior_w3_1_impulse():
    targets = _targets(296.10, 317.70 - 212.90)
    expected = [400.90, 465.6664, 570.4664, 740.0328]
    for got, want in zip(targets, expected, strict=True):
        assert abs(got - want) < 1e-9


def test_gram_reference_targets_use_nested_w3_1_not_global_w1():
    # User-validated parquet map: parent W2~1.286 -> W3-(1)~1.740 -> W3-(2)~1.460.
    targets = _targets(1.460, 1.740 - 1.286)
    expected = [1.914, 2.194572, 2.648572, 3.383144]
    for got, want in zip(targets, expected, strict=True):
        assert abs(got - want) < 1e-9


def test_bch_reference_rating_is_not_artificially_9x():
    retrace = (317.70 - 296.10) / (317.70 - 212.90)
    rating = _rating(
        retrace=retrace,
        growth_pct=(310.10 / 296.10 - 1.0) * 100.0,
        strict_distance_pct=(296.10 / 212.90 - 1.0) * 100.0,
        fib_status=">R.618 · 2 C4H",
        liquidity_rank=52,
        top_n=300,
        wave_type="W3-(2)",
        t1_upside_pct=(400.90 / 310.10 - 1.0) * 100.0,
    )
    assert rating == 8.8


def test_w3_1_extends_until_first_qualifying_pullback():
    rows = [
        (104, 101, 103),
        (110, 104, 109),
        (118, 109, 116),
        (125, 116, 123),
        (130, 123, 128),  # first pivot high
        (129, 126, 128),
        (131, 127, 130),  # pullback from 130 is too shallow
        (137, 130, 136),
        (140, 134, 138),  # W3-(1) extends
        (138, 132, 134),
        (136, 130, 132),  # 25% correction of 100->140
        (135, 131, 133),
        (134, 132, 133),
        (136, 133, 135),
        (137, 134, 136),
        (136, 133, 135),
    ]
    frame = _h4(rows)
    parent = {
        "w2_low": 100.0,
        "w2_ts": frame["timestamp"].iloc[0] - pd.Timedelta(hours=4),
        "high": 120.0,
    }
    nested = SeniorWaveDetector()._detect_nested(frame, parent)
    assert nested is not None
    assert nested["high"] == 140.0
    assert nested["low"] == 130.0


def test_tracking_does_not_reanchor_consumed_global_w2():
    from core_models import MarketSnapshot, WaveState

    previous = WaveState(
        symbol="TESTUSDT",
        exchange="binance_spot",
        wave_type="W2",
        status="RECOVERING",
        origin=100.0,
        impulse_high=200.0,
        working_low=130.0,
        strict_origin=100.0,
        impulse_start_ts="2025-12-01T00:00:00+00:00",
        impulse_high_ts="2025-12-10T00:00:00+00:00",
        working_low_ts="2025-12-31T16:00:00+00:00",
        retrace_depth=0.70,
        current_price=150.0,
        rating=8.0,
        last_complete4h_bucket="2025-12-31T20:00:00+00:00",
    )
    ts = pd.date_range("2026-01-01T00:00:00Z", periods=12, freq="1h")
    # First COMPLETE4H accepts above W1 high; a later bucket trades below old W2 low.
    hourly = pd.DataFrame(
        {
            "timestamp": ts,
            "open": [195, 202, 205, 208, 180, 160, 140, 130, 135, 140, 145, 150],
            "high": [205, 210, 212, 214, 185, 165, 145, 135, 140, 145, 150, 155],
            "low": [190, 198, 202, 205, 170, 150, 125, 120, 130, 135, 140, 145],
            "close": [202, 205, 208, 210, 175, 155, 130, 125, 135, 140, 145, 150],
            "volume": [1.0] * 12,
        }
    )
    snap = MarketSnapshot(
        symbol="TESTUSDT",
        exchange="binance_spot",
        quote_volume=1.0,
        live_price=150.0,
        live_low=145.0,
        hourly_closed=hourly,
        daily_closed=pd.DataFrame(),
    )
    out = SeniorWaveDetector().track(previous, snap, top_n=300)
    assert out.working_low == 130.0
    assert "LOCKED" in out.last_event
