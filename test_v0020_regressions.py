"""Explicitly synthetic lifecycle regressions, not a live MEXC ground truth.

The 42/120-day case reproduces the old-low/remaining-T4 symptom. Its price path
is generated below; no daily candle is passed off as real hourly history.
"""
from copy import deepcopy

import numpy as np
import pandas as pd
import pytest

from core_models import MarketSnapshot, WaveState
from core_senior import SeniorWaveDetector, complete4h
from services_formatter import _row_values_flags


def synthetic_history(points, *, spike=None):
    times, prices = zip(*points, strict=True)
    close = np.interp(np.arange(120 * 24) / 24, times, prices)
    opens = np.r_[close[0], close[:-1]]
    h1 = pd.DataFrame({
        "timestamp": pd.date_range("2026-06-05", periods=len(close), freq="h", tz="UTC"),
        "open": opens, "high": np.maximum(opens, close), "low": np.minimum(opens, close),
        "close": close, "volume": 1.,
    })
    if spike:
        h1.loc[spike[0] * 24, "high"] = spike[1]
    daily = h1.set_index("timestamp").resample("1D").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
    }).reset_index()
    return MarketSnapshot("SYNTHETIC", "mexc_futures", 1., float(close[-1]), None, h1, daily)


def test_adding_older_history_cannot_replace_current_correction_with_consumed_child():
    full = synthetic_history([
        (0, 80.), (10, 50.), (25, 100.), (40, 74.36), (47, 83.96),
        (53, 79.47), (80, 101.55), (102, 88.31), (119, 91.11),
    ], spike=(80, 108.88))
    short = deepcopy(full)
    short.hourly_closed = short.hourly_closed.iloc[-42 * 24:].copy()
    states = [SeniorWaveDetector().detect(s, None, 300) for s in (short, full)]
    for state in states:
        assert state is not None
        assert state.working_low == 88.31
        assert state.targets_hit == []
        assert state.targets == pytest.approx([122.83, 144.16336, 178.68336, 234.53672])
    # This asserts current-vs-historical selection, not a universal USOIL label.
    assert states[0].working_low_ts == states[1].working_low_ts


def test_consumed_nested_candidate_does_not_suppress_same_parents_current_w2():
    snap = synthetic_history([
        (0, 80.), (10, 50.), (25, 100.), (40, 75.), (53, 90.),
        (60, 85.), (119, 94.),
    ])
    state = SeniorWaveDetector().detect(snap, None, 300)
    assert state is not None
    assert state.wave_type == "W2"
    assert (state.origin, state.impulse_high, state.working_low) == (50., 100., 75.)


def lifecycle_snapshot(*, touched=False, low_on_breakout=False):
    lows = [130.] * 4 + [120. if low_on_breakout else 200.] * 4 + [145.] * 4
    h1 = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=12, freq="h", tz="UTC"),
        "open": [140.] * 4 + [205.] * 4 + [150.] * 4,
        "high": [145.] * 4 + [250. if touched else 215.] * 4 + [155.] * 4,
        "low": lows, "close": [140.] * 4 + [210.] * 4 + [150.] * 4, "volume": 1.,
    })
    return MarketSnapshot("SYNTHETIC", "binance_spot", 1., 150., None, h1, pd.DataFrame())


def build(snapshot, *, low_on_breakout=False):
    return SeniorWaveDetector()._build_state(
        snapshot=snapshot, wave_type="W2", origin=100., impulse_high=200.,
        working_low=120. if low_on_breakout else 130., strict_origin=100.,
        impulse_start_ts="2025-12-01T00:00:00Z", impulse_high_ts="2025-12-20T00:00:00Z",
        working_low_ts="2026-01-01T04:00:00Z" if low_on_breakout else "2026-01-01T00:00:00Z",
        retrace=.8 if low_on_breakout else .7, parent_w2_low=None, parent_w2_ts=None,
        w3_1_high=None, w3_1_high_ts=None, h4=complete4h(snapshot.hourly_closed),
        liquidity_rank=10, top_n=300, is_control=False,
    )


@pytest.mark.parametrize("low_on_breakout", [False, True])
def test_historical_count_is_labelled_after_and_acceptance_is_persisted(low_on_breakout):
    state = build(lifecycle_snapshot(low_on_breakout=low_on_breakout), low_on_breakout=low_on_breakout)
    assert state is not None
    state = WaveState.from_dict(state.to_dict())
    assert pd.Timestamp(state.structure_evidence["projection_accepted_at"]) == pd.Timestamp("2026-01-01T04:00:00Z")
    values, _ = _row_values_flags(state, 1)
    assert values[4] == "после W2"
    assert state.targets  # historical remaining targets are retained, not deleted


def test_recorded_acceptance_survives_rolling_history_and_prevents_late_reanchor():
    state = build(lifecycle_snapshot())
    state = WaveState.from_dict(state.to_dict())
    h1 = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01T08:00:00Z", periods=8, freq="h"),
        "open": 150., "high": 155., "low": [145.] * 4 + [120.] * 4,
        "close": 150., "volume": 1.,
    })
    # v0025 reloads back to the working low for exact recovery counts. Missing
    # history must not be replaced with a saved three-bar confirmation counter.
    from data_integrity import DataIntegrityError
    with pytest.raises(DataIntegrityError, match="history does not cover"):
        SeniorWaveDetector().track(deepcopy(state), MarketSnapshot("SYNTHETIC", "binance_spot", 1., 150., None, h1, pd.DataFrame()), 300)
    h1 = pd.concat([lifecycle_snapshot().hourly_closed.iloc[:8], h1], ignore_index=True)
    updated = SeniorWaveDetector().track(state, MarketSnapshot("SYNTHETIC", "binance_spot", 1., 150., None, h1, pd.DataFrame()), 300)
    assert updated.working_low == 130.
    assert "LOCKED" in updated.last_event


def test_search_cannot_reward_t1_again_after_touch_and_retreat():
    untouched = build(lifecycle_snapshot())
    touched = build(lifecycle_snapshot(touched=True))
    assert untouched.targets_hit == []
    assert touched.targets_hit == [1]
    assert untouched.current_price == touched.current_price == 150.
    assert untouched.fib_status == touched.fib_status
    assert touched.rating < untouched.rating


def test_track_cannot_reward_persisted_t1_hit_after_price_retreat():
    snap = lifecycle_snapshot()
    fresh = build(snap)
    hit = WaveState.from_dict(fresh.to_dict())
    hit.targets_hit = [1]  # Recorded touch before the current rolling candle window.
    detector = SeniorWaveDetector()
    before = detector.track(fresh, snap, 300)
    after = detector.track(hit, snap, 300)
    assert after.targets_hit == [1]
    assert after.rating < before.rating
