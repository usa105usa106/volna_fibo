# Historical v0018 synthetic geometry tests. These do NOT verify the named assets against parquet.
# Real archive regressions are in test_v0019_regressions.py.
from __future__ import annotations

import pandas as pd

from core_models import MarketSnapshot, WaveState
from core_senior import SeniorWaveDetector, _project_targets


def _assert_close_list(got: list[float], want: list[float], tol: float = 1e-10) -> None:
    assert len(got) == len(want)
    for a, b in zip(got, want, strict=True):
        assert abs(a - b) <= tol


def test_bch_parquet_targets_use_only_parent_w2_to_w3_1_even_if_generic_anchors_are_wrong():
    projection = _project_targets(
        "W3-(2)",
        working_low=296.10,
        # Deliberately wrong global anchors: production must ignore these for W3-(2).
        origin=180.0,
        impulse_high=500.0,
        parent_w2_low=212.90,
        w3_1_high=317.70,
    )
    assert projection is not None
    targets, origin, high, length, source = projection
    assert origin == 212.90
    assert high == 317.70
    assert abs(length - 104.80) < 1e-12
    assert source.startswith("W3-(1)")
    _assert_close_list(targets, [400.90, 465.6664, 570.4664, 740.0328])


def test_gram_parquet_targets_are_exact():
    projection = _project_targets(
        "W3-(2)",
        working_low=1.460,
        origin=0.80,
        impulse_high=1.90,
        parent_w2_low=1.286,
        w3_1_high=1.740,
    )
    assert projection is not None
    targets, *_ = projection
    _assert_close_list(targets, [1.914, 2.194572, 2.648572, 3.383144], tol=1e-12)


def test_usoil_parquet_targets_are_exact():
    projection = _project_targets(
        "W3-(2)",
        working_low=88.31,
        origin=50.0,
        impulse_high=100.0,
        parent_w2_low=76.0,
        w3_1_high=98.71,
    )
    assert projection is not None
    targets, *_ = projection
    _assert_close_list(targets, [111.02, 125.05478, 147.76478, 184.50956], tol=1e-10)


def test_w2_targets_use_w1_not_nested_fields():
    projection = _project_targets(
        "W2",
        working_low=50.0,
        origin=20.0,
        impulse_high=80.0,
        parent_w2_low=999.0,
        w3_1_high=2000.0,
    )
    assert projection is not None
    targets, origin, high, length, source = projection
    assert (origin, high, length) == (20.0, 80.0, 60.0)
    assert source.startswith("W1")
    _assert_close_list(targets, [110.0, 147.08, 207.08, 304.16])


def test_w3_2_never_falls_back_to_global_anchors_when_nested_anchors_missing():
    assert _project_targets(
        "W3-(2)",
        working_low=296.10,
        origin=180.0,
        impulse_high=500.0,
        parent_w2_low=None,
        w3_1_high=None,
    ) is None


def test_tracking_malformed_w3_2_forces_recount_instead_of_publishing_wrong_targets():
    prev = WaveState(
        symbol="BCH_USDT",
        exchange="mexc_futures",
        wave_type="W3-(2)",
        status="CONFIRMED",
        origin=180.0,
        impulse_high=500.0,
        working_low=296.1,
        strict_origin=212.9,
        # Intentionally missing parent_w2_low / w3_1_high.
        current_price=310.0,
        last_complete4h_bucket="2026-10-01T00:00:00+00:00",
    )
    ts = pd.date_range("2026-10-01T04:00:00Z", periods=8, freq="1h")
    hourly = pd.DataFrame(
        {
            "timestamp": ts,
            "open": [305.0] * 8,
            "high": [312.0] * 8,
            "low": [300.0] * 8,
            "close": [310.0] * 8,
            "volume": [1.0] * 8,
        }
    )
    snap = MarketSnapshot("BCH_USDT", "mexc_futures", 1.0, 310.0, 309.0, hourly, pd.DataFrame())
    out = SeniorWaveDetector().track(prev, snap, 300)
    assert out.status == "RECOUNT"
    assert out.targets == []
    assert out.target_origin is None
    assert "TARGET ANCHORS INVALID" in out.last_event
