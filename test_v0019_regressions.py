"""Regression evidence: real archive bytes plus isolated lifecycle failures.

Synthetic routing tests below are labelled explicitly. They are NOT golden
Elliott labels for GRAM/BCH/USOIL and do not replace the real parquet replay.
"""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from core_models import MarketSnapshot, WaveState
from core_ranking import select_top_crypto
from core_senior import SeniorWaveDetector, complete4h, _rating, _recovery, _fib_prices, _reached_targets
from data_integrity import DataIntegrityError
from replay_archive import ArchiveReader
from services_formatter import _targets as display_targets


@pytest.fixture(scope="module")
def archive():
    return ArchiveReader(Path(__file__).parent / "fixtures/market_0925")


@pytest.mark.parametrize("asset,origin,high,low,wave", [
    ("DOGE", .07831, .10589, .09031, "W3-(2)"),
    ("BCH", 212.8, 366.4, 296.1, "W3-(2)"),
    ("LTC", 50.20, 75.0, 65.63, "W3-(2)"),
    ("GRAM", 1.286, 1.740, 1.460, "W2"),
    ("USOIL", 74.36, 108.88, 88.31, "W2"),
    ("XAU", 3948.4, 4701.48, 4116.59, "W2"),
])
def test_real_archive_public_detector_anchors_and_decimal_projection(archive, asset, origin, high, low, wave):
    snapshot = archive.snapshot(asset)
    state = SeniorWaveDetector().detect(snapshot, archive.rank(asset), 300)
    assert state is not None
    assert (state.origin, state.impulse_high, state.working_low, state.wave_type) == (origin, high, low, wave)
    assert state.target_origin == state.strict_origin == origin
    assert state.target_impulse_high == high
    h4 = complete4h(snapshot.hourly_closed).set_index("timestamp")
    assert h4.at[pd.Timestamp(state.impulse_high_ts), "high"] == high
    assert h4.at[pd.Timestamp(state.working_low_ts), "low"] == low
    if wave == "W3-(2)":
        assert h4.at[pd.Timestamp(state.parent_w2_ts), "low"] == origin
        assert pd.Timestamp(state.parent_w2_ts) < pd.Timestamp(state.impulse_high_ts) < pd.Timestamp(state.working_low_ts)
    length = Decimal(str(high)) - Decimal(str(origin))
    expected = [Decimal(str(low)) + m * length for m in map(Decimal, ("1", "1.618", "2.618", "4.236"))]
    assert state.targets == pytest.approx([float(x) for x in expected], rel=1e-12)


def test_real_gram_all_time_low_cannot_inherit_an_invented_parent(archive):
    snap = archive.snapshot("GRAM")
    assert snap.daily_closed.low.min() == 1.286
    state = SeniorWaveDetector().detect(snap, archive.rank("GRAM"), 300)
    assert state.wave_type == "W2"  # prior parent is NOT proved by these bytes


def test_real_old_daily_parent_is_not_lost_at_hourly_window_boundary(archive):
    snap = archive.snapshot("DOGE")
    det = SeniorWaveDetector()
    parents = det._global_candidates(snap.daily_closed, complete4h(snap.hourly_closed))
    assert any(p["high"] == .1008 and p["w2_low"] == .07831 and p["high_context_only"] for p in parents)


def test_real_global_endpoint_cannot_be_a_lower_high_inside_same_impulse(archive):
    snap = archive.snapshot("BCH")
    parents = SeniorWaveDetector()._global_candidates(snap.daily_closed, complete4h(snap.hourly_closed))
    assert parents
    for p in parents:
        span = snap.daily_closed[(snap.daily_closed.timestamp >= pd.Timestamp(p["origin_ts"]).floor("D"))
                                 & (snap.daily_closed.timestamp <= p["high_ts"])]
        assert float(span.high.max()) <= p["high"]


def test_real_ltc_broken_origin_before_high_is_not_repaired_by_rebound(archive):
    snap = archive.snapshot("LTC")
    origin_ts = pd.Timestamp("2026-09-22T08:00:00Z")
    high_ts = pd.Timestamp("2026-09-26T08:00:00Z")
    between = snap.hourly_closed[(snap.hourly_closed.timestamp > origin_ts) & (snap.hourly_closed.timestamp < high_ts)]
    assert between.low.min() == 58.8 < 59.78
    state = SeniorWaveDetector()._build_state(snapshot=snap, wave_type="W3-(2)", origin=59.78,
        impulse_high=75., working_low=65.63, strict_origin=59.78, impulse_start_ts=origin_ts,
        impulse_high_ts=high_ts, working_low_ts=pd.Timestamp("2026-09-30T16:00:00Z"),
        retrace=(75-65.63)/(75-59.78), parent_w2_low=59.78, parent_w2_ts=origin_ts,
        w3_1_high=75., w3_1_high_ts=high_ts, h4=complete4h(snap.hourly_closed), liquidity_rank=22, top_n=300, is_control=False)
    assert state is None


@pytest.mark.parametrize("problem", ["duplicate", "gap", "bad_close_time"])
def test_archive_adapter_rejects_bad_candles(archive, problem):
    reader = deepcopy(archive)
    raw = reader.frames["crypto", "1h"]
    idx = raw[raw.asset == "DOGE"].index[100]
    if problem == "duplicate":
        raw = pd.concat([raw, raw.loc[[idx]].assign(low=.00001)])
    elif problem == "gap":
        raw = raw.drop(index=idx)
    else:
        raw.loc[idx, "close_time_ms"] += 1
    reader.frames["crypto", "1h"] = raw
    with pytest.raises((DataIntegrityError, ValueError)):
        reader.snapshot("DOGE")


def test_historical_snapshot_has_no_future_candles_or_live_ticker(archive):
    cutoff = pd.Timestamp("2026-09-26T08:00:00Z")
    snap = archive.snapshot("DOGE", cutoff=cutoff)
    assert (snap.hourly_closed.timestamp + pd.Timedelta(hours=1)).max() <= cutoff
    assert (snap.daily_closed.timestamp + pd.Timedelta(days=1)).max() <= cutoff
    assert snap.live_price == snap.hourly_closed.close.iloc[-1]
    state = SeniorWaveDetector().detect(snap, archive.rank("DOGE"), 300)
    if state:
        assert pd.Timestamp(state.working_low_ts) < cutoff


def test_synthetic_ancestry_fallback_passes_child_origin_to_every_subsystem(archive):
    snap = archive.snapshot("GRAM")
    det = SeniorWaveDetector()
    child = det._global_candidates(snap.daily_closed, complete4h(snap.hourly_closed))[-1]

    class IsolatedFallback(SeniorWaveDetector):
        def _global_candidates(self, daily, h4):
            return [child]
        def _detect_nested(self, h4, g, daily=None):
            return None
        def _lineage_nested(self, h4, parent, globals_):
            return None
        def _origin_is_prior_w2_h4(self, h4, candidate):
            return True  # synthetic routing, not evidence of actual GRAM ancestry

    state = IsolatedFallback().detect(snap, 58, 300)
    assert state is not None
    assert state.wave_type == "W3-(2)"
    assert state.origin == state.strict_origin == state.parent_w2_low == state.target_origin == 1.286
    assert state.working_low == 1.460


@pytest.mark.parametrize("wrong", ["price", "time"])
def test_nearby_price_or_different_date_is_not_parent_lineage(archive, wrong):
    snap = archive.snapshot("GRAM")
    h4 = complete4h(snap.hourly_closed)
    det = SeniorWaveDetector()
    child = det._global_candidates(snap.daily_closed, h4)[-1]
    parent = {"origin": .8, "high": 1.9, "w2_low": 1.30 if wrong == "price" else 1.286,
              "w2_ts": pd.Timestamp("2026-09-15T16:00:00Z") if wrong == "time" else pd.Timestamp("2026-09-16T16:00:00Z")}
    assert det._lineage_nested(h4, parent, [parent, child]) is None


def lifecycle_snapshot(periods=12, breakout_low=200.):
    h1 = pd.DataFrame({"timestamp": pd.date_range("2026-01-01", periods=12, freq="h", tz="UTC"),
        "open": [140.]*4+[205.]*4+[180.]*4, "high": [145.]*4+[215.]*4+[185.]*4,
        "low": [120.]*4+[breakout_low]*4+[170.]*4, "close": [140.]*4+[210.]*4+[180.]*4, "volume": 1.})
    h1 = h1.iloc[:periods].copy()
    return MarketSnapshot("TEST", "binance_spot", 1., float(h1.close.iloc[-1]), float(h1.low.iloc[-1]), h1, pd.DataFrame())


def prior(wave="W2"):
    return WaveState("TEST", "binance_spot", wave, "RECOVERING", 100., 200., 130., 100.,
        impulse_start_ts="2025-12-01T00:00:00Z", impulse_high_ts="2025-12-10T00:00:00Z",
        working_low_ts="2025-12-31T16:00:00Z", last_complete4h_bucket="2025-12-31T20:00:00Z",
        parent_w2_low=100. if wave == "W3-(2)" else None,
        w3_1_high=200. if wave == "W3-(2)" else None)


@pytest.mark.parametrize("wave", ["W2", "W3-(2)"])
@pytest.mark.parametrize("breakout_low,expected", [(200.,120.), (110.,110.)])
def test_track_reanchor_before_acceptance_is_independent_of_poll_batch(wave, breakout_low, expected):
    det = SeniorWaveDetector()
    batch = det.track(prior(wave), lifecycle_snapshot(breakout_low=breakout_low), 300)
    sequential = prior(wave)
    for periods in (4,8,12):
        sequential = det.track(sequential, lifecycle_snapshot(periods, breakout_low), 300)
    assert batch.working_low == sequential.working_low == expected
    assert batch.targets == sequential.targets
    assert batch.impulse_high == sequential.impulse_high == 200.


def test_legacy_saved_count_requires_same_symbol_recount():
    payload = prior().to_dict()
    payload.pop("detector_version")
    old = WaveState.from_dict(payload)
    result = SeniorWaveDetector().track(old, lifecycle_snapshot(), 300)
    assert result.status == "RECOUNT"
    assert result.symbol == "TEST"
    assert result.targets == []
    assert "LEGACY" in result.last_event


def test_three_of_three_recovery_never_scores_lower_than_two():
    args = dict(retrace=.7, growth_pct=2., strict_distance_pct=10., liquidity_rank=10,
                top_n=300, wave_type="W3-(2)", t1_upside_pct=30.)
    for level in ("236","382","500","618","705","786","886","950"):
        scores = [_rating(**args,fib_status=f"> .{level} · {n}/3 C4H") for n in (1,2,3)]
        assert scores == sorted(scores), (level, scores)


def test_new_low_cannot_inherit_old_three_bar_confirmation():
    h4 = complete4h(lifecycle_snapshot().hourly_closed)
    text, holds = _recovery(h4, _fib_prices(100.,200.), h4.timestamp.iloc[-1])
    assert holds == 1
    assert "1/3 C4H" in text


def test_conflicting_hourly_duplicates_cannot_be_aggregated_into_a_senior_bar():
    h1 = lifecycle_snapshot(4).hourly_closed
    with pytest.raises(DataIntegrityError, match="duplicate"):
        complete4h(pd.concat([h1, h1.iloc[[0]].assign(low=99.)]))


def test_hit_targets_are_persisted_and_not_recycled_after_retreat():
    snap = lifecycle_snapshot()
    targets = [150.,200.,250.,300.]
    hit = _reached_targets(snap,"2025-12-31T20:00:00Z",targets)
    assert hit == [1,2]
    state = prior()
    state.targets, state.targets_hit = targets, hit
    restored = WaveState.from_dict(state.to_dict())
    assert display_targets(restored) == "T3: 250 / T4: 300"
    restored.status = "INVALID"
    assert display_targets(restored) == "after recount"


def test_equal_displayed_rating_uses_freshness_then_confirmation_then_liquidity():
    states = []
    for symbol,growth,holds,rank in [("OLD",9.,3,1),("WEAK",2.,1,1),("THIN",2.,3,200),("BEST",2.,3,1)]:
        s = prior();s.symbol=symbol;s.rating=8.8;s.growth_from_low_pct=growth;s.fib_status=f"> .500 · {holds}/3 C4H";s.liquidity_rank=rank
        states.append(s)
    assert [s.symbol for s in select_top_crypto(states)] == ["BEST","THIN","WEAK","OLD"]
