"""Phase guard: real ZEC/BCH candles, chronology, no symbol/price exceptions.

These are detector contract tests, not evidence that Elliott degree is unique or
that any projected price will be reached. Live exchange access is not required.
"""
import json
from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from core_models import AppSettings, WaveState
from core_ranking import select_top_crypto
from core_senior import SeniorWaveDetector, complete4h
from data_integrity import DataIntegrityError
from db_repository import Repository
from replay_archive import ArchiveReader
from services_formatter import _row_values_flags, _targets, render_table_png

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def archive():
    return ArchiveReader(FIXTURES / "market_2057",
                         FIXTURES / "ticker_history/binance_TON_1d.parquet",
                         FIXTURES / "hourly_history/binance_BCH_ZEC_1h_prefix.parquet")


def test_real_zec_completed_subdivision_blocks_false_fresh_w3_2(archive):
    snap = archive.snapshot("ZEC")
    state = SeniorWaveDetector().detect(snap, archive.rank("ZEC"), 300)
    assert (state.wave_type, state.status) == ("W4", "PHASE_UNCERTAIN")
    assert (state.origin, state.impulse_high, state.working_low) == (368.03, 1698., 1271.09)
    assert state.strict_origin == 544.28
    assert state.targets == [] and state.target_origin is None
    assert state.target_impulse_high is None and state.target_impulse_length is None
    assert state.fibs == {} and state.base_zone is None and state.deep_zone is None
    assert state.rating == 0 and state.w3_1_high is None
    proof = state.structure_evidence["mature_impulse"]
    assert proof["certainty"] == "AMBIGUOUS_DEGREE"
    assert [p["price"] for p in proof["subwaves"]] == [368.03, 589.18, 451.75, 888., 751.53, 1698.]
    h4 = complete4h(snap.hourly_closed).set_index("timestamp")
    for point in proof["subwaves"]:
        assert h4.at[pd.Timestamp(point["timestamp"]), "high" if point["point"] % 2 else "low"] == point["price"]


@pytest.mark.parametrize("scale,shift", [(1e-8, -731), (1., 0), (10000., 366)])
def test_zec_phase_is_independent_of_symbol_price_scale_and_date(archive, scale, shift):
    snap = deepcopy(archive.snapshot("ZEC"))
    snap.symbol = "UNSEEN_ASSET"
    for frame in (snap.hourly_closed, snap.daily_closed):
        frame[["open", "high", "low", "close"]] *= scale
        frame["timestamp"] += pd.Timedelta(days=shift)
    snap.live_price *= scale
    state = SeniorWaveDetector().detect(snap, 20, 300)
    assert state.wave_type == "W4" and not state.targets
    assert state.working_low == pytest.approx(1271.09 * scale, rel=1e-12, abs=0)


@pytest.mark.parametrize("symbol,expected_wave,expected_targets", [
    ("BCH", "W3-(2)", [462.8, 565.8206, 732.5206, 1002.2412]),
    ("DOGE", "W3-(2)", [.11789, .13493444, .16251444, .20713888]),
    ("GRAM", "W3-(2)", [1.914, 2.194572, 2.648572, 3.383144]),
    ("XAU", "W2", [4869.67, 5335.07344, 6088.15344, 7306.63688]),
    ("USOIL", "W2", [122.83, 144.16336, 178.68336, 234.53672]),
])
def test_existing_senior_targets_are_not_removed_just_because_price_rose(archive, symbol, expected_wave, expected_targets):
    state = SeniorWaveDetector().detect(archive.snapshot(symbol), archive.rank(symbol), 300)
    assert state.wave_type == expected_wave and state.status != "PHASE_UNCERTAIN"
    assert state.targets == pytest.approx(expected_targets)


def test_short_zec_history_does_not_invent_subdivision_or_publish_shorter_targets(archive):
    short = ArchiveReader(FIXTURES / "market_2057")
    state = SeniorWaveDetector().detect(short.snapshot("ZEC"), 5, 300)
    assert state.status == "DATA_INCOMPLETE" and state.targets == []
    restored = SeniorWaveDetector().track(state, archive.snapshot("ZEC"), 300)
    assert restored.wave_type == "W4" and restored.targets == []


def test_stored_v0021_zec_is_recounted_on_first_track(archive):
    snap = archive.snapshot("ZEC")
    state = SeniorWaveDetector().detect(snap, 5, 300)
    state.wave_type = "W3-(2)"; state.status = "CONFIRMED"
    state.detector_version = "0021"; state.structure_evidence = {}
    state.strict_origin = state.origin
    state.targets = [2601.06, 3422.98146, 4752.95146, 6904.84292]
    state.rating = 9.2
    state = WaveState.from_dict(json.loads(json.dumps(state.to_dict())))
    result = SeniorWaveDetector().track(state, snap, 300)
    assert result.wave_type == "W4" and result.targets == []
    assert result.detector_version == "0022"
    assert "SAME-SYMBOL RECOUNT" in result.last_event


def test_track_retains_mature_phase_when_rolling_window_loses_all_parent_anchors(archive):
    snap = archive.snapshot("ZEC")
    state = SeniorWaveDetector().detect(snap, 5, 300)
    saved = WaveState.from_dict(json.loads(json.dumps(state.to_dict())))
    snap.hourly_closed = snap.hourly_closed.tail(300)
    snap.daily_closed = snap.daily_closed.tail(45)
    result = SeniorWaveDetector().track(saved, snap, 300)
    assert result.wave_type == "W4" and result.status == "PHASE_UNCERTAIN"
    assert result.structure_evidence["mature_impulse"] == state.structure_evidence["mature_impulse"]
    assert not result.targets and result.rating == 0


def test_track_w4_overlap_invalidates_immediately_even_without_new_c4h(archive):
    snap = archive.snapshot("ZEC")
    state = SeniorWaveDetector().detect(snap, 5, 300)
    snap.live_low = 544.27
    result = SeniorWaveDetector().track(state, snap, 300)
    assert result.status == "RECOUNT" and not result.targets
    snap.live_low = 1300.
    result = SeniorWaveDetector().track(result, snap, 300)
    assert result.status == "RECOUNT"  # a rebound cannot revive the invalid phase


def append_hours(snap, values):
    snap = deepcopy(snap)
    timestamps = pd.date_range(snap.hourly_closed.timestamp.iloc[-1] + pd.Timedelta(hours=1), periods=len(values), freq="h")
    fresh = pd.DataFrame([{"timestamp": ts, "open": close, "high": close + 2,
                          "low": close - 2, "close": close, "volume": 100.}
                         for ts, close in zip(timestamps, values)])
    snap.hourly_closed = pd.concat([snap.hourly_closed, fresh], ignore_index=True)
    snap.live_price = values[-1]
    return snap


def test_phase_track_uses_closed_h1_only_for_overlap_not_for_reanchoring(archive):
    snap = archive.snapshot("ZEC")
    state = SeniorWaveDetector().detect(snap, 5, 300)
    # Archive ends at 16:00; 17/18/19 complete the 16:00 UTC bucket.
    partial = append_hours(snap, [1262.])
    result = SeniorWaveDetector().track(state, partial, 300)
    assert result.working_low == 1271.09
    complete = append_hours(partial, [1280., 1290.])
    result = SeniorWaveDetector().track(result, complete, 300)
    assert result.working_low == 1260. and not result.targets
    assert result.working_low_ts.startswith("2026-10-03T16:")


def test_new_record_high_does_not_recycle_w4_into_w2(archive):
    snap = archive.snapshot("ZEC")
    state = SeniorWaveDetector().detect(snap, 5, 300)
    later = append_hours(snap, [1701., 1702., 1703.])
    result = SeniorWaveDetector().track(state, later, 300)
    assert result.wave_type == "W4" and result.status == "PHASE_UNCERTAIN"
    assert result.structure_evidence["mature_impulse"]["phase"] == "AFTER_W4_CANDIDATE"
    assert _row_values_flags(result, 1)[0][4] == "после W4?"
    assert result.targets == []


def test_mature_phase_track_rejects_backward_history(archive):
    snap = archive.snapshot("ZEC")
    state = SeniorWaveDetector().detect(snap, 5, 300)
    snap.hourly_closed = snap.hourly_closed.iloc[:-10]
    with pytest.raises(DataIntegrityError, match="backwards"):
        SeniorWaveDetector().track(state, snap, 300)


def test_old_nested_track_advances_to_mature_phase_in_the_same_cycle(archive):
    old = archive.snapshot("ZEC", cutoff="2026-08-01T00:00Z")
    detector = SeniorWaveDetector()
    state = detector._build_state(snapshot=old, wave_type="W3-(2)", origin=368.03,
        impulse_high=589.18, working_low=451.75, strict_origin=368.03,
        impulse_start_ts="2026-06-28T20:00Z", impulse_high_ts="2026-07-15T12:00Z",
        working_low_ts="2026-07-29T04:00Z", retrace=.6214,
        parent_w2_low=368.03, parent_w2_ts="2026-06-28T20:00Z",
        w3_1_high=589.18, w3_1_high_ts="2026-07-15T12:00Z",
        h4=complete4h(old.hourly_closed), liquidity_rank=5, top_n=300, is_control=False)
    assert state and state.targets
    result = detector.track(state, archive.snapshot("ZEC"), 300)
    assert result.wave_type == "W4" and result.targets == []
    assert "SAME CYCLE ADVANCED" in result.last_event


def test_phase_diagnostic_cannot_enter_search_top_even_with_stale_score_and_targets(archive):
    state = SeniorWaveDetector().detect(archive.snapshot("ZEC"), 5, 300)
    state.rating = 10.; state.targets = [99999.]
    assert select_top_crypto([state]) == []
    assert _targets(state) == "после проверки стадии"
    values, flags = _row_values_flags(state, 1)
    assert values[2] == "—" and values[4] == "W4? · кандидат"
    assert not any(flags)
    assert render_table_png([state], title="PHASE", subtitle="closed data", crypto_label="CHECK", version="0022").startswith(b"\x89PNG")


@pytest.mark.parametrize("cutoff", ["2026-08-01T00:00Z", "2026-08-27T00:00Z", "2026-09-20T00:00Z"])
def test_historical_replay_never_uses_later_high_or_subdivision(archive, cutoff):
    snap = archive.snapshot("ZEC", cutoff=cutoff)
    state = SeniorWaveDetector().detect(snap, 5, 300)
    assert (snap.hourly_closed.timestamp + pd.Timedelta(hours=1)).max() <= pd.Timestamp(cutoff)
    if state is not None:
        assert state.impulse_high != 1698.
        for point in state.structure_evidence.get("mature_impulse", {}).get("subwaves", []):
            assert pd.Timestamp(point["timestamp"]) + pd.Timedelta(hours=4) <= pd.Timestamp(cutoff)


def phase_geometry(prices):
    """Synthetic control: six widely separated swings, not real exchange quotes."""
    knots = [0, 12*6, 24*6, 36*6, 48*6, 60*6, 68*6, 72*6]
    levels = list(prices) + [prices[-1] * .78, prices[-1] * .83]
    close = np.interp(np.arange(knots[-1] + 1), knots, levels)
    h4 = pd.DataFrame({"timestamp": pd.date_range("2024-01-01", periods=len(close), freq="4h", tz="UTC"),
                       "open": close, "high": close, "low": close, "close": close, "volume": 100., "n": 4})
    daily = h4.set_index("timestamp").resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).reset_index()
    parent = {"origin": 50., "high": 130., "w2_low": prices[0], "w2_ts": h4.timestamp.iloc[0]}
    state = WaveState("SYNTHETIC", "binance_spot", "W3-(2)", "RECOVERING",
                      prices[0], prices[-1], levels[-2], prices[0],
                      impulse_start_ts=h4.timestamp.iloc[0].isoformat(),
                      impulse_high_ts=h4.timestamp.iloc[knots[5]].isoformat(),
                      working_low_ts=h4.timestamp.iloc[knots[6]].isoformat())
    return h4, daily, state, parent


def test_synthetic_five_senior_legs_can_trigger_guard_without_any_known_symbol():
    evidence = SeniorWaveDetector()._mature_impulse_evidence(*phase_geometry([100., 160., 125., 250., 190., 400.]))
    assert evidence and evidence["phase"] == "W4_CANDIDATE"


@pytest.mark.parametrize("prices", [
    [100., 160., 125., 250., 150., 400.],  # internal (4) overlaps (1)
    [100., 160., 145., 200., 185., 400.],  # (3) shortest, despite valid corrections
    [100., 160., 95., 250., 190., 400.],   # internal (2) breaks its origin
    [100., 160., 125., 250., 190., 249.],  # no higher fifth high
    [100., 106., 102., 108., 106.5, 112.], # micro fluctuations
])
def test_invalid_or_micro_subdivision_is_not_a_w4_proof(prices):
    assert SeniorWaveDetector()._mature_impulse_evidence(*phase_geometry(prices)) is None


def test_local_four_hour_turns_without_daily_context_do_not_promote_degree():
    h4, daily, state, parent = phase_geometry([100., 160., 125., 250., 190., 400.])
    assert SeniorWaveDetector()._mature_impulse_evidence(h4, daily.iloc[:0], state, parent) is None


@pytest.mark.parametrize("wick_source", ["live", "closed_h1"])
def test_new_search_with_overlap_does_not_publish_a_fresh_w4_or_w3_2(archive, wick_source):
    snap = archive.snapshot("ZEC")
    if wick_source == "live":
        snap.live_low = 500.
    else:
        # This is a new hourly wick, not a complete 4H structural anchor.
        snap.hourly_closed.loc[snap.hourly_closed.index[-1], "low"] = 500.
    state = SeniorWaveDetector().detect(snap, 5, 300)
    # Without a saved phase, Search may retain a strictly intact HISTORICAL
    # correction. The existing methodology permits that, labelled "after W3-(2)".
    # It must not call the overlapping current correction a fresh long setup.
    assert state.status == "EXTENDED"
    assert state.structure_evidence["projection_accepted_at"]
    assert pd.Timestamp(state.working_low_ts) < pd.Timestamp("2026-09-26T20:00Z")
    assert state.strict_origin < 500.
    assert _row_values_flags(state, 1)[0][4].startswith("после ")


@pytest.mark.asyncio
async def test_sqlite_roundtrip_preserves_mature_phase_evidence(tmp_path, archive):
    state = SeniorWaveDetector().detect(archive.snapshot("ZEC"), 5, 300)
    repo = Repository(tmp_path / "phase.sqlite3", AppSettings())
    await repo.init()
    session_id = await repo.replace_active_session("binance_spot", 300, [state])
    loaded = (await repo.tracked_states(session_id))[0]
    assert loaded.to_dict() == state.to_dict()
    assert loaded.wave_type == "W4" and loaded.targets == []


def test_independent_init_case_from_full_universe_triggers_the_same_guard(archive):
    snapshot = archive.snapshot("INIT")
    snapshot.symbol = "ANOTHER_UNSEEN_ASSET"
    state = SeniorWaveDetector().detect(snapshot, 100, 300)
    assert state.wave_type == "W4" and state.status == "PHASE_UNCERTAIN"
    assert not state.targets
    points = state.structure_evidence["mature_impulse"]["subwaves"]
    assert [p["price"] for p in points] == [.05234, .06993, .06336, .1019, .08464, .11659]
    h4 = complete4h(snapshot.hourly_closed).set_index("timestamp")
    for point in points:
        assert h4.at[pd.Timestamp(point["timestamp"]), "high" if point["point"] % 2 else "low"] == point["price"]
