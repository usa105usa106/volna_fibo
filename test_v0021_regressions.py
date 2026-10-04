"""Real 20:57 parquet regressions and explicit hierarchy/data-integrity negatives.

The manual numbers are tested only with their identified exchange and OHLC
anchors, not as universal forecasts or as MEXC crypto fixtures.
"""
import asyncio
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import httpx
import pandas as pd
import pytest

from core_models import WaveState
from core_senior import SeniorWaveDetector, complete4h
from data_exchanges import BinanceSpotClient, MexcFuturesClient
from data_integrity import DataIntegrityError
from data_lineage import (
    TRANSITIONS,
    decode_binance_daily_zip,
    load_daily_context,
    merge_daily_context,
    restrict_current_history,
)
from replay_archive import ArchiveReader
from services_formatter import _row_values_flags, _targets

FIXTURES = Path(__file__).parent / "fixtures"
TON = FIXTURES / "ticker_history/binance_TON_1d.parquet"
HOURS = FIXTURES / "hourly_history/binance_BCH_1h_prefix.parquet"


@pytest.fixture(scope="module")
def archive():
    return ArchiveReader(FIXTURES / "market_2057", TON, HOURS)


def test_bch_senior_parent_beats_inner_corrections(archive):
    state = SeniorWaveDetector().detect(archive.snapshot("BCH"), archive.rank("BCH"), 300)
    assert state.wave_type == "W3-(2)" and state.status != "DATA_INCOMPLETE"
    assert (state.origin, state.impulse_high, state.working_low) == (199.7, 366.4, 296.1)
    assert state.strict_origin == state.target_origin == state.parent_w2_low == 199.7
    assert state.targets == pytest.approx([462.8, 565.8206, 732.5206, 1002.2412], rel=1e-12)
    parent = state.structure_evidence["senior_parent"]
    assert (parent["origin"], parent["high"], parent["w2_low"]) == (181., 255.1, 199.7)
    assert parent["w2_ts"].startswith("2026-08-14T12:")
    h4 = complete4h(archive.snapshot("BCH").hourly_closed).set_index("timestamp")
    assert h4.at[pd.Timestamp(parent["high_ts"]), "high"] == 255.1
    assert h4.at[pd.Timestamp(parent["w2_ts"]), "low"] == 199.7
    assert state.origin not in {212.8, 239.}


def test_user_rounded_anchors_use_exact_existing_fibonacci_multipliers():
    from core_senior import _project_targets
    result = _project_targets("W3-(2)", working_low=296., origin=199., impulse_high=365.,
                              parent_w2_low=199., w3_1_high=365.)
    expected = [Decimal(296) + k * Decimal(166) for k in map(Decimal, ("1", "1.618", "2.618", "4.236"))]
    assert result[0] == pytest.approx([float(x) for x in expected])
    assert result[0] == pytest.approx([462., 564.588, 730.588, 999.176])


def test_truncated_h1_cannot_silently_lower_bch_degree(archive):
    short = ArchiveReader(FIXTURES / "market_2057", TON)
    state = SeniorWaveDetector().detect(short.snapshot("BCH"), 30, 300)
    assert state.status == "DATA_INCOMPLETE" and state.targets == []
    assert state.structure_evidence["senior_history_incomplete"]
    assert state.structure_evidence["senior_parent"]["w2_low"] == 199.7
    restored = SeniorWaveDetector().track(state, archive.snapshot("BCH"), 300)
    assert restored.origin == 199.7 and restored.targets[0] == pytest.approx(462.8)


def test_ltc_short_history_is_not_misrepresented_as_a_proven_senior_count(archive):
    state = SeniorWaveDetector().detect(archive.snapshot("LTC"), 30, 300)
    assert state.status == "DATA_INCOMPLETE" and state.targets == []
    assert state.structure_evidence["senior_parent"]["w2_low"] == 43.39


@pytest.mark.parametrize("scale", [1e-6, 10000.])
def test_hierarchy_uses_neither_ticker_nor_absolute_price_constants(archive, scale):
    snap = deepcopy(archive.snapshot("BCH"))
    snap.symbol = "UNSEEN_SYMBOL"
    for frame in (snap.hourly_closed, snap.daily_closed):
        frame[["open", "high", "low", "close"]] *= scale
        frame["timestamp"] -= pd.Timedelta(days=365)
    snap.live_price *= scale
    state = SeniorWaveDetector().detect(snap, 20, 300)
    assert state.origin == pytest.approx(199.7 * scale)
    assert state.targets == pytest.approx([x * scale for x in (462.8, 565.8206, 732.5206, 1002.2412)])


@pytest.mark.parametrize("asset,origin,first", [("DOGE", .07831, .11789), ("XAU", 3948.4, 4869.67), ("USOIL", 74.36, 122.83)])
def test_unchanged_anchors_are_not_replaced_to_match_a_screenshot(archive, asset, origin, first):
    state = SeniorWaveDetector().detect(archive.snapshot(asset), archive.rank(asset), 300)
    assert state.origin == origin
    assert state.targets[0] == pytest.approx(first)
    assert "completed_parent" not in state.structure_evidence


def test_gram_real_predecessor_proves_parent_without_changing_target_impulse(archive):
    state = SeniorWaveDetector().detect(archive.snapshot("GRAM"), 30, 300)
    assert state.wave_type == "W3-(2)"
    assert (state.origin, state.impulse_high, state.working_low) == (1.286, 1.740, 1.460)
    assert state.targets == pytest.approx([1.914, 2.194572, 2.648572, 3.383144])
    parent = state.structure_evidence["daily_parent"]
    assert parent["origin"] == 1.124
    assert parent["high"] == 2.907
    assert parent["timeframe"] == "1D_CONTEXT_ONLY"
    assert parent["origin_day"].startswith("2026-02-06")
    assert parent["high_day"].startswith("2026-05-07")
    assert state.structure_evidence["history"]["exchange"] == "binance_spot"


def test_missing_ton_is_visible_and_track_recovers_when_history_returns(archive):
    short = ArchiveReader(FIXTURES / "market_2057")
    state = SeniorWaveDetector().detect(short.snapshot("GRAM"), 30, 300)
    assert state.structure_evidence["ancestry_incomplete"] is True
    assert "нет истории TON" in _row_values_flags(state, 1)[0][4]
    targets = state.targets.copy()
    restored = SeniorWaveDetector().track(WaveState.from_dict(state.to_dict()), archive.snapshot("GRAM"), 300)
    assert restored.wave_type == "W3-(2)" and restored.targets == targets
    assert "HISTORY RESTORED" in restored.last_event
    assert not restored.structure_evidence["ancestry_incomplete"]


@pytest.mark.parametrize("asset,legacy_origin,expected", [("BCH", 212.8, 199.7), ("BCH", 239., 199.7)])
def test_upgrade_recounts_existing_symbol_and_replaces_stored_v0020_targets(archive, asset, legacy_origin, expected):
    snap = archive.snapshot(asset)
    current = SeniorWaveDetector().detect(snap, 30, 300)
    old = WaveState.from_dict(current.to_dict())
    old.detector_version = "0020"
    old.origin = old.strict_origin = old.parent_w2_low = legacy_origin
    old.targets = [9999.]
    result = SeniorWaveDetector().track(old, snap, 300)
    assert result.symbol == asset and result.origin == expected
    assert result.targets == current.targets
    assert result.detector_version == "0022"


def test_invalid_legacy_count_never_revives_on_version_upgrade(archive):
    snap = archive.snapshot("BCH")
    state = SeniorWaveDetector().detect(snap, 30, 300)
    state.status = "INVALID"; state.detector_version = "0020"
    result = SeniorWaveDetector().track(state, snap, 300)
    assert result.status == "INVALID" and not result.targets
    assert _targets(result) == "after recount"


def test_new_parent_is_invalidated_by_incomplete_hourly_wick(archive):
    snap = deepcopy(archive.snapshot("BCH"))
    state = SeniorWaveDetector().detect(snap, 30, 300)
    assert state.strict_origin == 199.7
    snap.live_low = 199.69
    result = SeniorWaveDetector().track(state, snap, 300)
    assert result.status == "INVALID" and not result.targets


def test_offline_predecessor_cannot_be_imported_from_another_exchange(archive):
    broken = deepcopy(archive)
    broken.predecessor_daily["source_exchange"] = "mexc_futures"
    with pytest.raises(DataIntegrityError, match="source_exchange mismatch"):
        broken.snapshot("GRAM")


@pytest.mark.parametrize("bad", ["gap", "duplicate", "wrong_symbol", "truncated_seam"])
def test_predecessor_defects_are_not_repaired_with_synthetic_candles(archive, bad):
    prefix = pd.read_parquet(TON)
    if bad == "gap":
        prefix = prefix.drop(index=100)
    elif bad == "duplicate":
        prefix = pd.concat([prefix, prefix.iloc[[100]]])
    elif bad == "wrong_symbol":
        prefix["source_symbol"] = "GRM_USDT"
    else:
        prefix = prefix.iloc[:-1]
    with pytest.raises(DataIntegrityError):
        merge_daily_context(archive.snapshot("GRAM").daily_closed, prefix, TRANSITIONS["binance_spot"], archive.asof)


def test_official_binance_microsecond_zip_and_checksum_are_used():
    folder = FIXTURES / "ticker_history"
    name = "TONUSDT-1d-2026-06.zip"
    data = (folder / name).read_bytes()
    checksum = (folder / (name + ".CHECKSUM")).read_text()
    with pytest.raises(DataIntegrityError, match="close_time"):
        decode_binance_daily_zip(data, checksum, name)
    frame = decode_binance_daily_zip(data, checksum, name, terminal_time=TRANSITIONS["binance_spot"].predecessor_end)
    assert str(frame.timestamp.iloc[0]) == "2026-06-01 00:00:00+00:00"
    assert frame.low.min() == 1.443
    assert len(frame) == 29  # Jun 30 traded only 00:00–03:00; not a complete D1.
    with pytest.raises(DataIntegrityError, match="checksum"):
        decode_binance_daily_zip(data + b"changed", checksum, name)
    with pytest.raises(DataIntegrityError):
        decode_binance_daily_zip(data, "", name)


def test_reused_mexc_gram_ticker_does_not_import_unrelated_old_gram():
    data = pd.DataFrame({"timestamp": pd.to_datetime(["2026-06-04T00:00Z", "2026-06-15T14:00Z", "2026-06-15T15:00Z"]), "low": [.00001, 1.5, 1.51]})
    filtered = restrict_current_history(data, "mexc_futures", "GRAM_USDT", "1h")
    assert filtered.low.tolist() == [1.51]


@pytest.mark.asyncio
async def test_predecessor_outage_is_explicit_and_cancellation_propagates(archive):
    class FakeClient:
        name = "mexc_futures"
        async def candles(self, symbol, tf, start, end):
            assert symbol == "TON_USDT" and tf == "1d"
            raise httpx.ConnectError("temporary outage")
    snap = archive.snapshot("GRAM")
    context, info = await load_daily_context(FakeClient(), "GRAM_USDT", snap.daily_closed,
        pd.Timestamp("2025-10-03T00:00Z"), archive.asof)
    assert context is None and info["status"] == "unavailable"
    assert info["exchange"] == "mexc_futures"
    class CancelledClient(FakeClient):
        async def candles(self, *args):
            raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await load_daily_context(CancelledClient(), "GRAM_USDT", snap.daily_closed,
            pd.Timestamp("2025-10-03T00:00Z"), archive.asof)


@pytest.mark.asyncio
async def test_mexc_predecessor_uses_only_mexc_client_and_current_wins_overlap():
    now = pd.Timestamp("2026-06-18T00:00Z")
    def frame(start, n):
        return pd.DataFrame({"timestamp": pd.date_range(start, periods=n, freq="D", tz="UTC"),
                             "open": 1.5, "high": 1.6, "low": 1.4, "close": 1.51, "volume": 10.})
    current = frame("2026-06-15", 3)
    requests = []
    class FakeMexc:
        name = "mexc_futures"
        async def candles(self, symbol, tf, start, end):
            requests.append((symbol, tf, end))
            return frame("2026-06-01", 14), None, None
    context, info = await load_daily_context(FakeMexc(), "GRAM_USDT", current,
        pd.Timestamp("2026-06-01T00:00Z"), now)
    assert info["status"] == "restored" and len(context) == 17
    assert requests[0][0] == "TON_USDT"
    assert context.iloc[-1].source_symbol == "GRAM_USDT"
    assert not context.timestamp.duplicated().any()


@pytest.mark.parametrize("cls,symbol", [(BinanceSpotClient, "DOGEUSDT"), (MexcFuturesClient, "DOGE_USDT")])
@pytest.mark.asyncio
async def test_ordinary_symbols_trigger_no_predecessor_download(cls, symbol):
    def forbidden(request):
        pytest.fail("unrelated symbol requested ticker history")
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
        context, info = await load_daily_context(cls(http), symbol, pd.DataFrame(), None, None)
    assert context is None and info == {}


def test_predecessor_context_is_clipped_at_replay_cutoff(archive):
    cutoff = pd.Timestamp("2026-09-25T08:00:00Z")
    snap = archive.snapshot("GRAM", cutoff=cutoff)
    assert (snap.daily_context.timestamp + pd.Timedelta(days=1)).max() <= cutoff
    assert snap.live_price == snap.hourly_closed.close.iloc[-1]


@pytest.mark.parametrize("locked", ["acceptance", "target"])
def test_senior_parent_does_not_rewrite_an_already_accepted_projection(archive, locked):
    snap = archive.snapshot("BCH")
    detector = SeniorWaveDetector()
    state = detector.detect(snap, 30, 300)
    state.origin = 212.8
    if locked == "acceptance":
        state.structure_evidence["projection_accepted_at"] = state.working_low_ts
    else:
        state.targets_hit = [1]
    assert detector._prefer_senior_parent(snap, complete4h(snap.hourly_closed), snap.daily_closed, state, 30, 300) is state


def test_prefix_from_wrong_exchange_is_rejected(archive):
    bad = deepcopy(archive)
    bad.hourly_prefix["source_exchange"] = "mexc_futures"
    with pytest.raises(ValueError, match="exchange mismatch"):
        bad.snapshot("BCH")


def test_prefix_duplicate_and_conflicting_overlap_are_rejected(archive):
    bad = deepcopy(archive)
    bad.hourly_prefix = pd.concat([bad.hourly_prefix, bad.hourly_prefix.iloc[[10]]])
    with pytest.raises(DataIntegrityError, match="duplicate"):
        bad.snapshot("BCH")
    bad = deepcopy(archive)
    bad.hourly_prefix.loc[bad.hourly_prefix.index[-1], "volume"] += 1
    with pytest.raises(ValueError, match="conflicts"):
        bad.snapshot("BCH")


@pytest.mark.asyncio
async def test_binance_lineage_loader_checks_official_zip_before_attaching_context():
    folder = FIXTURES / "ticker_history"
    name = "TONUSDT-1d-2026-06.zip"
    calls = []
    def handler(request):
        calls.append(str(request.url))
        assert request.url.host == "data.binance.vision"
        assert "/TONUSDT/1d/" in request.url.path
        if request.url.path.endswith(".CHECKSUM"):
            return httpx.Response(200, text=(folder / (name + ".CHECKSUM")).read_text())
        return httpx.Response(200, content=(folder / name).read_bytes())
    now = pd.Timestamp("2026-07-04T00:00Z")
    current = pd.DataFrame({"timestamp": pd.date_range("2026-07-02", periods=2, freq="D", tz="UTC"),
                            "open": 1.5, "high": 1.6, "low": 1.4, "close": 1.51, "volume": 10.})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        context, info = await load_daily_context(BinanceSpotClient(http), "GRAMUSDT", current,
                                                pd.Timestamp("2026-06-01T00:00Z"), now)
    assert info["status"] == "restored" and len(calls) == 2
    assert len(context) == 31 and info["missing_calendar_days"] == 2
    assert info["prefix_through"].startswith("2026-06-29")
    assert len(info["archives"]) == 1
