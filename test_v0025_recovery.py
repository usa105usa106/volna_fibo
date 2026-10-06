"""Recovery semantics regressions: actual closed-C4H streaks, not old depth labels."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from core_models import MarketSnapshot, WaveState
from core_ranking import ranking_key
from core_recovery import parse_recovery_status, recovery_prices, recovery_status
from core_senior import SeniorWaveDetector, _rating, _status_from_state, complete4h
from data_integrity import DataIntegrityError
from replay_archive import ArchiveReader
from services_formatter import (
    _row_values_flags,
    technical_report_text,
    telegram_table_messages,
)
from services_scanner import ScannerService


def bars(closes):
    return pd.DataFrame(
        {
            "timestamp": pd.date_range(
                "2026-01-01", periods=len(closes), freq="4h", tz="UTC"
            ),
            "open": closes,
            "high": [x + 1 for x in closes],
            "low": [min(100.0, x) for x in closes],
            "close": closes,
            "volume": 4.0,
            "n": 4,
        }
    )


def snapshot(closes, live_price=None, incomplete=()):
    rows = []
    for row in bars(closes).to_dict("records"):
        row.pop("n")
        for hour in range(4):
            rows.append(
                {**row, "timestamp": row["timestamp"] + pd.Timedelta(hours=hour)}
            )
    for price in incomplete:
        rows.append(
            {
                "timestamp": rows[-1]["timestamp"] + pd.Timedelta(hours=1),
                "open": price,
                "close": price,
                "high": price + 1,
                "low": price - 1,
                "volume": 1.0,
            }
        )
    return MarketSnapshot(
        "TESTUSDT",
        "binance_spot",
        1.0,
        live_price or closes[-1],
        None,
        pd.DataFrame(rows),
        pd.DataFrame(),
    )


def build(snap):
    return SeniorWaveDetector()._build_state(
        snapshot=snap,
        wave_type="W2",
        origin=50.0,
        impulse_high=200.0,
        working_low=100.0,
        strict_origin=50.0,
        impulse_start_ts="2025-12-01T00:00Z",
        impulse_high_ts="2025-12-20T00:00Z",
        working_low_ts="2026-01-01T00:00Z",
        retrace=2 / 3,
        parent_w2_low=None,
        parent_w2_ts=None,
        w3_1_high=None,
        w3_1_high_ts=None,
        h4=complete4h(snap.hourly_closed),
        liquidity_rank=1,
        top_n=300,
        is_control=False,
    )


@pytest.mark.parametrize(
    "low,high,close,r236,status",
    [
        (296.49, 366.88, 315.84, 313.10204, ">R.236 · 1 C4H"),
        (4116.59, 4701.48, 4143.57, 4254.62404, "<R.236 · 0 C4H"),
        (88.31, 108.88, 91.26, 93.16452, "<R.236 · 0 C4H"),
        (2600.15, 2807.34, 2708.0, 2649.04684, ">R.500 · 1 C4H"),
    ],
)
def test_user_examples(low, high, close, r236, status):
    grid = recovery_prices(low, high)
    assert grid["R236"] == pytest.approx(r236, rel=1e-12)
    assert recovery_status(bars([close]), grid, "2026-01-01T00:00Z")[0] == status


@pytest.mark.parametrize(
    "closes,status,count",
    [
        ([110], "<R.236 · 0 C4H", 0),
        ([125], ">R.236 · 1 C4H", 1),
        ([110] + [125] * 7, ">R.236 · 7 C4H", 7),
        ([125, 140, 141], ">R.382 · 2 C4H", 2),
        ([110, 151, 160, 152], ">R.500 · 3 C4H", 3),
        ([151, 149, 151], ">R.500 · 1 C4H", 1),
        # Losing R500 downgrades to R382 and recounts at R382, including prior R500 closes.
        ([125, 140, 151, 160, 149], ">R.382 · 4 C4H", 4),
        ([125, 110], "<R.236 · 0 C4H", 0),
        ([200] * 12, ">R.886 · 12 C4H", 12),
        ([123.6], "=R.236 · 0 C4H", 0),
        ([151, 150], ">R.382 · 2 C4H", 2),
    ],
)
def test_consecutive_unlimited_count_and_downgrade(closes, status, count):
    assert recovery_status(
        bars(closes), recovery_prices(100, 200), "2026-01-01T00:00Z"
    ) == (status, count)


def test_new_low_resets_grid_and_entire_streak():
    candles = bars([160.0] * 8)
    assert (
        recovery_status(candles, recovery_prices(100, 200), candles.timestamp.iloc[0])[
            1
        ]
        == 8
    )
    assert recovery_status(
        candles, recovery_prices(90, 200), candles.timestamp.iloc[-1]
    ) == (">R.618 · 1 C4H", 1)


def test_hole_does_not_create_consecutive_acceptance():
    candles = bars([160.0] * 6).drop(index=3)
    assert (
        recovery_status(candles, recovery_prices(100, 200), candles.timestamp.iloc[0])[
            1
        ]
        == 2
    )


def test_truncated_history_does_not_invent_an_exact_counter():
    candles = bars([160.0] * 4)
    with pytest.raises(DataIntegrityError, match="history does not cover"):
        recovery_status(candles, recovery_prices(100, 200), "2025-12-01T00:00Z")
    candles.loc[0, "close"] = 110.0
    assert (
        recovery_status(candles, recovery_prices(100, 200), "2025-12-01T00:00Z")[1] == 3
    )


def test_live_price_and_incomplete_four_hours_cannot_change_official_status():
    s = build(snapshot([110] + [125] * 7))
    live = build(
        snapshot([110] + [125] * 7, live_price=190.0, incomplete=[190.0, 191.0, 192.0])
    )
    assert s.fib_status == live.fib_status == ">R.236 · 7 C4H"
    assert s.last_complete4h_close == live.last_complete4h_close == 125.0
    assert s.targets == live.targets
    assert live.fibs == recovery_prices(100, 200)
    assert live.retrace_depth == pytest.approx(2 / 3)


def test_track_recomputes_legacy_fibs_and_counters_without_changing_anchors(
    monkeypatch,
):
    snap = snapshot([110] + [140] * 7)
    previous = build(snap)
    expected = deepcopy(previous)
    previous.fibs = {"0.236": 164.6, "0.382": 142.7}
    previous.fib_status = "> .382 · 999/3 C4H"
    detector = SeniorWaveDetector()
    monkeypatch.setattr(detector, "detect", lambda *a: None)
    result = detector.track(WaveState.from_dict(previous.to_dict()), snap, 300)
    assert result.fib_status == ">R.382 · 7 C4H"
    assert result.fibs == recovery_prices(100, 200)
    for field in (
        "origin",
        "impulse_high",
        "working_low",
        "strict_origin",
        "targets",
        "retrace_depth",
    ):
        assert getattr(result, field) == getattr(expected, field)


@pytest.mark.asyncio
@pytest.mark.parametrize("outage", [False, True])
async def test_track_downloads_to_frozen_low_or_preserves_saved_state(
    cfg, repo, monkeypatch, outage
):
    full = snapshot([110] + [140] * 7)
    prior = build(full)
    prior.fib_status = "> .382 · 3/3 C4H"
    await repo.replace_active_session("binance_spot", 300, [prior])
    short = deepcopy(full)
    short.hourly_closed = short.hourly_closed.tail(8)
    loader = SimpleNamespace(
        snapshot=AsyncMock(return_value=short),
        walk_history=AsyncMock(return_value=full),
    )
    if outage:
        loader.walk_history.side_effect = DataIntegrityError(
            "earlier history unavailable"
        )
    service = ScannerService(cfg, repo, loader)
    monkeypatch.setattr(service.detector, "detect", lambda *a: None)
    result = await service.track()
    loader.walk_history.assert_awaited_once()
    assert loader.walk_history.call_args.args == ("binance_spot", "TESTUSDT", 0.0)
    stored = (await repo.tracked_states())[0]
    if outage:
        assert result.states[0].status == "DATA_INCOMPLETE"
        assert stored.to_dict() == prior.to_dict()
    else:
        assert not result.errors
        assert result.states[0].fib_status == stored.fib_status == ">R.382 · 7 C4H"


def test_new_complete_low_reanchors_track_and_resets_counter(monkeypatch):
    initial = snapshot([110] + [140] * 7)
    prior = build(initial)
    later = snapshot([110] + [140] * 8)
    later.hourly_closed.loc[later.hourly_closed.index[-4:], "low"] = 90.0
    detector = SeniorWaveDetector()
    monkeypatch.setattr(detector, "detect", lambda *a: None)
    result = detector.track(prior, later, 300)
    assert result.working_low == 90.0
    assert result.fib_status == ">R.382 · 1 C4H"
    assert result.fibs == recovery_prices(90, 200)
    assert result.targets == pytest.approx([240.0, 332.7, 482.7, 725.4])


@pytest.mark.parametrize(
    "count,status",
    [
        (0, "DEEP"),
        (1, "RECOVERING"),
        (2, "RECOVERING"),
        (3, "CONFIRMED"),
        (7, "CONFIRMED"),
    ],
)
def test_state_uses_actual_recovery_persistence(count, status):
    fib = f">R.236 · {count} C4H" if count else "<R.236 · 0 C4H"
    assert _status_from_state(2.0, fib) == status


def test_rating_is_monotone_in_recovery_and_consecutive_acceptance():
    args = {
        "retrace": 0.7,
        "growth_pct": 2.0,
        "strict_distance_pct": 10.0,
        "liquidity_rank": 10,
        "top_n": 300,
        "wave_type": "W3-(2)",
        "t1_upside_pct": 30.0,
    }
    levels = ("236", "382", "500", "618", "705", "786", "886")
    for count in (1, 2, 3, 7):
        scores = [
            _rating(**args, fib_status=f">R.{level} · {count} C4H") for level in levels
        ]
        assert scores == sorted(scores)
    for level in levels:
        scores = [
            _rating(**args, fib_status=f">R.{level} · {n} C4H") for n in (1, 2, 3, 7)
        ]
        assert scores == sorted(scores)
    assert parse_recovery_status("> .236 · 3/3 C4H") == (None, 0)


def test_rendering_ranking_and_diagnostics_use_same_uncapped_count():
    state = build(snapshot([110] + [160] * 7))
    values, flags = _row_values_flags(state, 1)
    assert values[6] == ">R.500 · 7 C4H" and flags[6]
    assert "&gt;R.500 · 7 C4H" in "\n".join(telegram_table_messages([state], "manual"))
    assert ">R.500 · 7 C4H" in technical_report_text(
        [state], heading=["Search"], errors=[]
    )
    shorter = deepcopy(state)
    shorter.fib_status = ">R.500 · 3 C4H"
    assert ranking_key(state) < ranking_key(shorter)


@pytest.mark.parametrize("scale", [1.0, 1e-10, 1e6])
def test_tiny_decimal_recovery_grid_is_scale_invariant(scale):
    grid = recovery_prices(100 * scale, 200 * scale)
    candles = bars([125 * scale] * 7)
    assert recovery_status(candles, grid, candles.timestamp.iloc[0]) == (
        ">R.236 · 7 C4H",
        7,
    )


@pytest.mark.parametrize("symbol", ["BCH", "XAU", "USOIL", "DOGE", "GRAM", "ZEC"])
def test_real_archive_recovery_matches_independent_close_streak(symbol):
    root = Path(__file__).parent / "fixtures"
    reader = ArchiveReader(
        root / "market_2057",
        root / "ticker_history/binance_TON_1d.parquet",
        root / "hourly_history/binance_BCH_ZEC_1h_prefix.parquet",
    )
    snap = reader.snapshot(symbol)
    state = SeniorWaveDetector().detect(snap, 20, 300)
    h4 = complete4h(snap.hourly_closed)
    assert state is not None and state.fibs == recovery_prices(
        state.working_low, state.impulse_high
    )
    ratio, actual = parse_recovery_status(state.fib_status)
    if ratio is None:
        assert float(h4.close.iloc[-1]) <= state.fibs["R236"]
        assert actual == 0
        return
    level = state.working_low + ratio * (state.impulse_high - state.working_low)
    expected = 0
    rows = h4[h4.timestamp >= pd.Timestamp(state.working_low_ts)]
    last_ts = None
    for row in reversed(list(rows.itertuples())):
        if row.close <= level or (
            last_ts is not None and last_ts - row.timestamp != pd.Timedelta(hours=4)
        ):
            break
        expected += 1
        last_ts = row.timestamp
    assert actual == expected
