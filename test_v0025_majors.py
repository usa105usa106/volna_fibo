"""Real Binance parquet + explicitly synthetic future candles/venue basis tests."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from conftest import state as generic_state
from core_major_rules import (
    KNOWN_AT,
    PROTOCOL,
    cross_asset_context,
    impulse_errors,
    projection,
    recovery,
)
from core_major_state import btc_hard_rules, choose_btc
from core_majors import MajorWaveEngine
from core_models import MarketSnapshot
from data_exchanges import _candle_result
from services_formatter import _row_values_flags, split_plain, technical_report_text
from services_major_formatter import major_count_text
from services_scanner import ScannerService

ROOT = Path(__file__).parent / "fixtures" / "majors_v0025"


def snapshot(base):
    h = pd.read_parquet(ROOT / f"{base}_1h.parquet")
    d = pd.read_parquet(ROOT / f"{base}_1d.parquet")
    return MarketSnapshot(
        base + "USDT",
        "binance_spot",
        1.0,
        float(h.close.iloc[-1]),
        float(h.low.iloc[-1]),
        h,
        d,
        observed_at=(h.timestamp.max() + pd.Timedelta(hours=1)).isoformat(),
    )


@pytest.fixture(scope="module")
def initial():
    e = MajorWaveEngine()
    return {base: e.evaluate(snapshot(base), None, 1, 300) for base in ("BTC", "ETH")}


def book(initial, base="BTC"):
    return deepcopy(initial[base].structure_evidence["major_count"])


def live(s, low, high, close):
    s = deepcopy(s)
    stamp = s.hourly_closed.timestamp.iloc[-1] + pd.Timedelta(hours=1)
    s.live_candle = {
        "timestamp": stamp.isoformat(),
        "open": close,
        "high": high,
        "low": low,
        "close": close,
        "volume": 10.0,
    }
    s.observed_at = (stamp + pd.Timedelta(minutes=20)).isoformat()
    s.live_price, s.live_low = close, low
    return s


def next_bucket(s, low, high, close):
    s = deepcopy(s)
    start = s.hourly_closed.timestamp.iloc[-1] + pd.Timedelta(hours=1)
    end = start.ceil("4h") + pd.Timedelta(hours=4)
    times = pd.date_range(start, end - pd.Timedelta(hours=1), freq="h")
    rows = pd.DataFrame(
        [
            {
                "timestamp": t,
                "open": close,
                "high": high,
                "low": low,
                "close": close,
                "volume": 100.0,
            }
            for t in times
        ]
    )
    s.hourly_closed = pd.concat([s.hourly_closed, rows], ignore_index=True)
    s.observed_at = end.isoformat()
    s.live_price = close
    s.live_low = low
    s.live_candle = None
    return s


def test_real_btc_all_anchors_box_and_alts(initial):
    b = book(initial)
    assert b["primary"] == "V1" and b["current"] == "W3-(5) ACTIVE / DEVELOPING"
    assert b["box"] == {"lower": 82300.0, "upper": 94990.7, "status": "ACTIVE"}
    assert b["alt"] == ["V2", "V3"]
    assert b["variants"]["V2"]["targets"] == pytest.approx(
        [107683.67, 123208.24406, 148328.91406, 188974.15812]
    )
    assert b["variants"]["V3"]["targets"] == pytest.approx(
        [112158.48, 130448.48664, 160043.96664, 207929.45328]
    )


@pytest.mark.parametrize(
    "low,high,retired",
    [
        (82300, 85700, False),
        (82299, 85700, True),
        (85000, 94990.70, False),
        (85000, 94990.71, True),
    ],
)
def test_exact_btc_boundaries_and_wicks(initial, low, high, retired):
    b = book(initial)
    btc_hard_rules(b, low, high, "2026-10-05T00:00Z")
    assert ("V1" in b["retired"]) == retired
    if retired:
        assert b["state"] == "BTC_POSTBOX_DUAL"


def test_no_resurrection_and_origin_hard_switches(initial):
    b = book(initial)
    btc_hard_rules(b, 82299, 85700, "2026-10-05T00:00Z")
    choose_btc(b, {"V2": {"total": 7}, "V3": {"total": 7}}, "2026-10-05T00:00Z")
    assert b["primary"] == "V3" and b["unresolved"] and b["alt"] == ["V2"]
    btc_hard_rules(b, 85000, 87000, "2026-10-05T01:00Z")
    assert "V1" in b["retired"] and b["primary"] != "V1"
    btc_hard_rules(b, 62274, 85000, "2026-10-05T02:00Z")
    assert b["state"] == "BTC_V3_PRIMARY" and b["alt"] == []
    btc_hard_rules(b, 57800.18, 60000, "2026-10-05T03:00Z")
    assert b["state"] == "BTC_FULL_RECOUNT"


def test_primary_hysteresis_duplicate_poll_and_gap(initial):
    b = book(initial)
    btc_hard_rules(b, 82299, 85000, "2026-10-05T00:00Z")
    choose_btc(b, {"V2": {"total": 6}, "V3": {"total": 8}}, "2026-10-05T00:00Z")
    assert b["primary"] == "V3"
    scores = {"V2": {"total": 9}, "V3": {"total": 7}}
    choose_btc(b, scores, "2026-10-05T04:00Z")
    assert b["primary"] == "V3"
    choose_btc(b, scores, "2026-10-05T04:00Z")
    assert b["primary"] == "V3"
    choose_btc(b, scores, "2026-10-05T12:00Z")
    assert b["primary"] == "V3"
    choose_btc(b, scores, "2026-10-05T16:00Z")
    assert b["primary"] == "V2"
    choose_btc(b, {"V2": {"total": 7}, "V3": {"total": 8}}, "2026-10-05T20:00Z")
    assert b["primary"] == "V2" and b["unresolved"]


def test_real_eth_chronology_and_derived_internal_one(initial):
    s = initial["ETH"]
    b = book(initial, "ETH")
    a = b["anchors"]
    assert s.wave_type == "W3-(2)" and s.working_low == 2600.15
    assert a["origin"]["price"] == 1512 and a["w1"]["price"] == 1846
    assert a["w1"]["timestamp"] > a["origin"]["timestamp"]
    assert a["i1"]["price"] == 1981.24 and a["i4"]["price"] == 2356.41
    assert impulse_errors(b["subdivision"]) == []
    assert s.target_source == "W3-(3) TARGETS"
    assert s.targets == pytest.approx([3657.29, 4310.60252, 5367.74252, 7078.19504])


def test_eth_2708_recovery_and_reanchor(initial):
    e = MajorWaveEngine()
    s = next_bucket(snapshot("ETH"), 2650, 2740, 2708)
    a = e.evaluate(s, initial["ETH"], 1, 300)
    assert a.status == "RECOVERING" and a.fib_status == ">R.500 · 2 C4H"
    s2 = next_bucket(s, 2500, 2650, 2600)
    a2 = e.evaluate(s2, a, 1, 300)
    assert a2.working_low == 2500 and a2.fibs == recovery(2500, 2807.34)
    assert a2.targets == projection(2500, 1750.2, 2807.34)
    assert a2.targets != a.targets


def test_eth_wick_only_no_confirmation_no_reanchor(initial):
    s = live(snapshot("ETH"), 2500, 2900, 2708)
    a = MajorWaveEngine().evaluate(s, initial["ETH"], 1, 300)
    assert a.working_low == 2600.15 and a.status == "RECOVERING"
    assert a.targets == initial["ETH"].targets


def test_eth_c4h_close_acceptance(initial):
    s = next_bucket(snapshot("ETH"), 2650, 2840, 2810)
    a = MajorWaveEngine().evaluate(s, initial["ETH"], 1, 300)
    assert a.status == "CONFIRMED" and a.wave_type == "W3-(3)"
    assert a.structure_evidence["major_count"]["accepted_at"]


def test_eth_valid_intrabar_origin_break_never_revives(initial):
    e = MajorWaveEngine()
    snap = live(snapshot("ETH"), 1749, 2710, 2708)
    a = e.evaluate(snap, initial["ETH"], 1, 300)
    assert a.status == "RECOUNT" and a.targets == [] and a.fibs == {}
    assert a.last_event == "OLD COUNT INVALID — FULL SENIOR RECOUNT REQUIRED."
    bounced = live(snapshot("ETH"), 2605, 2750, 2708)
    b = e.evaluate(bounced, a, 1, 300)
    assert b.status == "RECOUNT"


def test_btc_api_current_high_survives_into_invalidation(initial):
    s = snapshot("BTC")
    current = live(s, 85000, 95000, 85700)
    raw = pd.concat(
        [s.hourly_closed, pd.DataFrame([current.live_candle])], ignore_index=True
    )
    closed, _price, _low = _candle_result(
        raw, "1h", pd.Timestamp(current.observed_at).to_pydatetime(), is_control=False
    )
    assert closed.attrs["live_candle"]["high"] == 95000
    current.hourly_closed = closed
    current.live_candle = closed.attrs["live_candle"]
    a = MajorWaveEngine().evaluate(current, initial["BTC"], 1, 300)
    assert "V1" in a.structure_evidence["major_count"]["retired"]


@pytest.mark.parametrize("fault", ["duplicate", "gap", "nan", "ohlc", "live_ohlc"])
def test_integrity_failure_does_not_mutate_count(initial, fault):
    before = initial["ETH"].to_dict()
    s = live(snapshot("ETH"), 1700, 2800, 2700)
    if fault == "duplicate":
        s.hourly_closed = pd.concat(
            [s.hourly_closed, s.hourly_closed.tail(1)], ignore_index=True
        )
    if fault == "gap":
        s.hourly_closed = s.hourly_closed.drop(index=50)
    if fault == "nan":
        s.hourly_closed.loc[50, "low"] = float("nan")
    if fault == "ohlc":
        s.hourly_closed.loc[50, "low"] = 1e9
    if fault == "live_ohlc":
        s.live_candle["high"] = 1600
    a = MajorWaveEngine().evaluate(s, initial["ETH"], 1, 300)
    assert a.status == "DATA_INCOMPLETE"
    assert initial["ETH"].to_dict() == before
    assert not a.structure_evidence["major_count"]["retired"]


@pytest.mark.parametrize("base", ["BTC", "ETH"])
def test_synthetic_mexc_basis_prices_are_measured_not_copied(base, initial):
    s = snapshot(base)
    scale = 1.007
    for f in (s.hourly_closed, s.daily_closed):
        for c in ("open", "high", "low", "close"):
            f[c] *= scale
    s.exchange = "mexc_futures"
    s.symbol = base + "_USDT"
    s.live_price *= scale
    s.live_low *= scale
    a = MajorWaveEngine().evaluate(s, None, 1, 300)
    assert a.status != "DATA_INCOMPLETE"
    assert a.working_low == pytest.approx(initial[base].working_low * scale)
    assert all(
        p["market"] == "mexc_futures"
        for p in a.structure_evidence["major_count"]["anchors"].values()
    )
    assert a.targets == pytest.approx([x * scale for x in initial[base].targets])
    if base == "BTC":
        assert a.structure_evidence["major_count"]["box"]["upper"] == pytest.approx(
            94990.7 * scale
        )


def test_cross_exchange_previous_count_rejected(initial):
    s = snapshot("ETH")
    s.exchange = "mexc_futures"
    s.symbol = "ETH_USDT"
    a = MajorWaveEngine().evaluate(s, initial["ETH"], 1, 300)
    assert a.status == "DATA_INCOMPLETE" and "mismatch" in a.last_event


def test_missing_reference_history_not_faked():
    s = snapshot("BTC")
    s.hourly_closed = s.hourly_closed.tail(999)
    a = MajorWaveEngine().evaluate(s, None, 1, 300)
    assert a.status == "DATA_INCOMPLETE" and not a.targets


def test_stale_snapshot_cannot_roll_back_state(initial):
    s = snapshot("ETH")
    s.observed_at = (pd.Timestamp(s.observed_at) - pd.Timedelta(hours=1)).isoformat()
    s.hourly_closed = s.hourly_closed.iloc[:-1]
    a = MajorWaveEngine().evaluate(s, initial["ETH"], 1, 300)
    assert a.status == "DATA_INCOMPLETE"


def test_ethbtc_derived_ratio_synchronous_and_soft():
    times = pd.date_range("2026-09-01", periods=12, freq="4h", tz="UTC")
    btc = pd.DataFrame({"timestamp": times, "close": [85000] * 12})
    eth = pd.DataFrame(
        {"timestamp": times, "close": [2500 + i * 20 for i in range(12)]}
    )
    c = cross_asset_context(eth, btc, "mexc_futures")
    assert c["score"] == 0.5 and c["ratio"] == pytest.approx(2720 / 85000)
    assert "derived ratio" in c["source"]
    eth["close"] = 1000
    assert cross_asset_context(eth, btc, "mexc_futures")["score"] == 0


def test_complete_output_and_html_limits(initial):
    text = major_count_text(initial["BTC"])
    for key in (
        "BTC COUNT:",
        "BTC STATE:",
        "CURRENT WAVE:",
        "BOX:",
        "BOX STATUS:",
        "WORKING LOW:",
        "HARD INVALIDATION:",
        "PRIMARY:",
        "ALT:",
        "WHY PRIMARY:",
        "V2 SCORE:",
        "V3 SCORE:",
    ):
        assert key in text
    eth = major_count_text(initial["ETH"])
    for key in (
        "ETH BASE:",
        "W1:",
        "W2:",
        "W3-(1):",
        "W3-(2):",
        "CURRENT:",
        "RECOVERY FIB:",
        "W3-(3) TARGETS:",
        "ETH/BTC:",
    ):
        assert key in eth
    report = technical_report_text(list(initial.values()), heading=["test"])
    assert "V1 PRIMARY" in report and "W3-(3) TARGETS:" in report
    for part in split_plain(text + "\n" + eth):
        assert len(part.encode("utf-16-le")) // 2 <= 3500
    row, _ = _row_values_flags(initial["BTC"], 1)
    assert "V1" in row[4] and "94990.7" in row[-1]


@pytest.mark.asyncio
async def test_ledger_survives_reset_search_manual_and_restart(cfg, repo, initial):
    old = generic_state()
    await repo.replace_active_session("binance_spot", 100, [old])
    data = SimpleNamespace(
        universe=AsyncMock(return_value=[("BTCUSDT", 1)]),
        snapshot=AsyncMock(return_value=live(snapshot("BTC"), 82299, 86000, 85700)),
    )
    scanner = ScannerService(cfg, repo, data)
    first = await scanner.analyze_symbols(["BTC"])
    assert first.states[0].structure_evidence["major_count"]["retired"]["V1"]
    assert [s.symbol for s in await repo.tracked_states()] == [old.symbol]
    await repo.reset_to_defaults()
    scanner = ScannerService(cfg, repo, data)
    data.snapshot.return_value = live(snapshot("BTC"), 85000, 86000, 85700)
    second = await scanner.analyze_symbols(["BTC"])
    assert second.states[0].structure_evidence["major_count"]["primary"] != "V1"
    saved, revision = await repo.major_count("binance_spot", "BTCUSDT", PROTOCOL)
    assert revision == 2 and saved.structure_evidence["major_count"]["retired"]["V1"]


@pytest.mark.asyncio
async def test_ledger_cas_prevents_concurrent_and_resurrection(repo, initial):
    s = deepcopy(initial["BTC"])
    await repo.save_major_count(s, 0)
    with pytest.raises(RuntimeError, match="concurrently"):
        await repo.save_major_count(s, 0)
    b = s.structure_evidence["major_count"]
    btc_hard_rules(b, 82299, 85000, b["observed_at"])
    await repo.save_major_count(s, 1)
    with pytest.raises(ValueError, match="restored"):
        await repo.save_major_count(deepcopy(initial["BTC"]), 2)
    restored, revision = await repo.major_count("binance_spot", "BTCUSDT", PROTOCOL)
    assert revision == 2 and restored.structure_evidence["major_count"]["retired"]["V1"]


@pytest.mark.asyncio
async def test_bad_new_data_does_not_overwrite_ledger(cfg, repo, initial):
    await repo.save_major_count(initial["ETH"], 0)
    s = snapshot("ETH")
    s.hourly_closed = s.hourly_closed.iloc[::2]
    data = SimpleNamespace(
        universe=AsyncMock(return_value=[]), snapshot=AsyncMock(return_value=s)
    )
    scanner = ScannerService(cfg, repo, data)
    result = await scanner.analyze_symbols(["eth"])
    saved, revision = await repo.major_count("binance_spot", "ETHUSDT", PROTOCOL)
    assert result.states[0].status == "DATA_INCOMPLETE" and revision == 1
    assert saved.to_dict() == initial["ETH"].to_dict()


@pytest.mark.asyncio
async def test_scanner_other_assets_use_original_detector(cfg, repo):
    data = SimpleNamespace(
        universe=AsyncMock(return_value=[]),
        snapshot=AsyncMock(
            side_effect=lambda exchange, symbol, qv: SimpleNamespace(
                symbol=symbol, exchange=exchange, live_price=10
            )
        ),
    )
    scanner = ScannerService(cfg, repo, data)
    scanner.detector = SimpleNamespace(
        detect=lambda snap, *args: generic_state(snap.symbol)
    )
    result = await scanner.analyze_symbols(["doge", "pol", "sol", "bch", "gram", "zec"])
    assert len(result.states) == 6 and all(
        s.status == generic_state().status for s in result.states
    )


@pytest.mark.parametrize(
    "base,key,source",
    [
        ("ETH", "w1", "base1"),
        ("ETH", "i1", "w2"),
        ("BTC", "i1", "w1"),
        ("BTC", "low", "peak"),
    ],
)
def test_saved_anchor_chronology_rejected_before_projection(
    initial, base, key, source, monkeypatch
):
    s = deepcopy(initial[base])
    b = s.structure_evidence["major_count"]
    b["anchors"][key]["timestamp"] = b["anchors"][source]["timestamp"]

    def forbidden(*args):
        raise AssertionError(
            "targets must not be calculated before chronology validation"
        )

    monkeypatch.setattr("core_majors.projection", forbidden)
    result = MajorWaveEngine().evaluate(snapshot(base), s, 1, 300)
    assert (
        result.status == "DATA_INCOMPLETE"
        and result.targets == []
        and result.fibs == {}
    )
    assert "chronology" in result.last_event


def test_saved_cross_exchange_anchor_is_rejected(initial):
    s = deepcopy(initial["ETH"])
    s.structure_evidence["major_count"]["anchors"]["w2"]["market"] = "mexc_futures"
    result = MajorWaveEngine().evaluate(snapshot("ETH"), s, 1, 300)
    assert (
        result.status == "DATA_INCOMPLETE" and "exchange mismatch" in result.last_event
    )


def test_btc_reanchor_lower_complete_low_recalculates_both_variants(initial):
    snap = next_bucket(snapshot("BTC"), 80000, 85000, 84000)
    a = MajorWaveEngine().evaluate(snap, initial["BTC"], 1, 300)
    b = a.structure_evidence["major_count"]
    assert b["working_low"] == 80000 and "V1" in b["retired"]
    assert b["variants"]["V2"]["targets"] == projection(80000, 62275, 87395.67)
    assert b["variants"]["V3"]["targets"] == projection(80000, 57800.19, 87395.67)


def test_unverified_live_scalar_cannot_retire_a_count(initial):
    snap = snapshot("BTC")
    snap.live_price = 100000
    snap.live_low = 57000
    a = MajorWaveEngine().evaluate(snap, initial["BTC"], 1, 300)
    # A legacy snapshot with no valid OHLC envelope has no trustworthy wick evidence.
    assert a.status == "DATA_INCOMPLETE"
    assert not a.structure_evidence["major_count"]["retired"]


def test_exact_eth_origin_wick_is_valid(initial):
    a = MajorWaveEngine().evaluate(
        live(snapshot("ETH"), 1750.2, 2750, 2708), initial["ETH"], 1, 300
    )
    assert (
        a.status == "RECOVERING"
        and "ETH" not in a.structure_evidence["major_count"]["retired"]
    )


@pytest.mark.asyncio
async def test_collector_loads_full_daily_reference_hours_and_same_market_ratio(cfg):
    from core_major_rules import FULL_DAILY_START, HISTORY_START
    from data_collector import MarketDataService

    own, peer = snapshot("ETH"), snapshot("BTC")
    now = pd.Timestamp(own.observed_at)
    service = MarketDataService(cfg)
    calls = []

    async def candles(exchange, symbol, tf, start, end):
        calls.append((exchange, symbol, tf, pd.Timestamp(start)))
        s = own if symbol == "ETHUSDT" else peer
        f = deepcopy(s.hourly_closed if tf == "1h" else s.daily_closed)
        f.attrs["live_candle"] = {
            "timestamp": now.isoformat(),
            "open": 2708.0,
            "high": 2800.0,
            "low": 2605.0,
            "close": 2708.0,
            "volume": 10.0,
        }
        return f, float(f.close.iloc[-1]), float(f.low.iloc[-1])

    service._candles = candles
    try:
        result = await service._snapshot_window(
            "binance_spot",
            "ETHUSDT",
            0.0,
            now=now.to_pydatetime(),
            h1_start=(now - pd.Timedelta(days=5)).to_pydatetime(),
            d1_start=(now - pd.Timedelta(days=30)).to_pydatetime(),
            check_freshness=False,
        )
    finally:
        await service.close()
    assert calls[0][3] == HISTORY_START and calls[1][3] == FULL_DAILY_START
    assert calls[2][0:3] == ("binance_spot", "BTCUSDT", "1h")
    assert result.live_candle["high"] == 2800 and result.cross_asset["ratio"] > 0


@pytest.mark.asyncio
async def test_walk_after_known_at_uses_private_ledger_and_no_future(
    cfg, repo, initial, monkeypatch
):
    # Freeze diagnostic clock so its latest checkpoint is after policy publication.
    from datetime import datetime, timezone

    import services_scanner as module

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 8, 0, 0, tzinfo=timezone.utc)

    class TimestampClock:
        def __call__(self, *args, **kwargs):
            return pd.Timestamp(*args, **kwargs)

        def now(self, *args, **kwargs):
            return pd.Timestamp(Clock.now())

    class PandasClock:
        Timestamp = TimestampClock()

        def __getattr__(self, name):
            return getattr(pd, name)

    monkeypatch.setattr(module, "pd", PandasClock())
    cfg = cfg.model_copy(update={"walk_history_days": 1, "walk_horizon_days": 1})
    full = next_bucket(snapshot("BTC"), 85000, 86000, 85700)
    start = full.hourly_closed.timestamp.iloc[-1] + pd.Timedelta(hours=1)
    times = pd.date_range(start, "2026-10-07T23:00Z", freq="h")
    full.hourly_closed = pd.concat(
        [
            full.hourly_closed,
            pd.DataFrame(
                {
                    "timestamp": times,
                    "open": 85700.0,
                    "high": 86000.0,
                    "low": 85000.0,
                    "close": 85700.0,
                    "volume": 1.0,
                }
            ),
        ],
        ignore_index=True,
    )
    data = SimpleNamespace(
        universe=AsyncMock(return_value=[]), walk_history=AsyncMock(return_value=full)
    )
    await repo.save_major_count(initial["BTC"], 0)
    before = (await repo.major_count("binance_spot", "BTCUSDT", PROTOCOL))[0].to_dict()
    scanner = ScannerService(cfg, repo, data)
    scanner.detector = SimpleNamespace(detect=lambda *args: None)
    seen = []

    def evaluate(self, snap, previous, *args):
        cutoff = pd.Timestamp(snap.observed_at)
        assert cutoff >= KNOWN_AT
        assert (snap.hourly_closed.timestamp + pd.Timedelta(hours=1)).max() <= cutoff
        assert (snap.daily_closed.timestamp + pd.Timedelta(days=1)).max() <= cutoff
        assert snap.cross_asset == {}  # today's ratio cannot leak into past score
        seen.append((snap.symbol, previous is not None))
        return deepcopy(initial["BTC"])

    monkeypatch.setattr(MajorWaveEngine, "evaluate", evaluate)
    await scanner.walk_forward()
    saved, rev = await repo.major_count("binance_spot", "BTCUSDT", PROTOCOL)
    assert (
        seen
        and any(previous for _, previous in seen)
        and rev == 1
        and saved.to_dict() == before
    )


@pytest.mark.asyncio
async def test_cancel_during_major_commit_drains_observed_invalidation(
    cfg, repo, initial
):
    import asyncio

    entered, release = asyncio.Event(), asyncio.Event()
    scanner = ScannerService(cfg, repo, SimpleNamespace())
    original = repo.save_major_count

    async def paused_save(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    repo.save_major_count = paused_save
    task = asyncio.create_task(
        scanner._analyze(live(snapshot("BTC"), 82299, 86000, 85700), 1, 300)
    )
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    saved, revision = await repo.major_count("binance_spot", "BTCUSDT", PROTOCOL)
    assert revision == 1 and "V1" in saved.structure_evidence["major_count"]["retired"]


@pytest.mark.asyncio
async def test_search_keeps_major_context_separate_from_top10_and_old_tracked_set(
    cfg, repo, initial
):
    universe = [("BTCUSDT", 1), ("ETHUSDT", 1)] + [(f"C{i}USDT", 1) for i in range(98)]

    async def snap(exchange, symbol, qv):
        return (
            snapshot(symbol[:-4])
            if symbol in {"BTCUSDT", "ETHUSDT"}
            else SimpleNamespace(symbol=symbol, exchange=exchange)
        )

    data = SimpleNamespace(
        universe=AsyncMock(return_value=universe),
        snapshot=snap,
        available_controls=AsyncMock(return_value={}),
    )
    scanner = ScannerService(cfg, repo, data)

    def detect(s, *args):
        a = generic_state(s.symbol)
        a.rating = 10.0
        return a

    scanner.detector = SimpleNamespace(detect=detect)
    old = generic_state()
    await repo.replace_active_session("binance_spot", 100, [old])
    result = await scanner.search()
    assert len(result.states) == 10 and len(result.major_context) == 2
    assert [s.symbol for s in await repo.tracked_states()] == [old.symbol]
    assert "BTCUSDT" not in {s.symbol for s in result.states}
    assert {s.symbol for s in result.major_context} == {"BTCUSDT", "ETHUSDT"}


def test_btc_completed_low_adjusts_box_cap_without_moving_intrabar(initial):
    s = next_bucket(snapshot("BTC"), 82400, 86000, 85000)
    a = MajorWaveEngine().evaluate(s, initial["BTC"], 1, 300)
    b = a.structure_evidence["major_count"]
    assert b["primary"] == "V1" and b["box"]["upper"] == 94827.7
    current = live(s, 82350, 86000, 85000)
    newer = MajorWaveEngine().evaluate(current, a, 1, 300)
    assert (
        newer.working_low == 82400
        and newer.structure_evidence["major_count"]["box"]["upper"] == 94827.7
    )


def test_cross_asset_wrong_venue_is_discarded_not_usd_invalidation(initial):
    snap = snapshot("ETH")
    snap.cross_asset = {"market": "mexc_futures", "score": 1.0, "ratio": 0.05}
    a = MajorWaveEngine().evaluate(snap, initial["ETH"], 1, 300)
    assert a.status == "RECOVERING"
    assert a.structure_evidence["major_count"]["cross_asset"]["score"] == 0.0


def test_full_recount_skips_projection_arithmetic(initial, monkeypatch):
    import core_majors

    def forbidden(*args):
        raise AssertionError("invalid counts must not calculate targets")

    monkeypatch.setattr(core_majors, "projection", forbidden)
    a = MajorWaveEngine().evaluate(
        live(snapshot("ETH"), 1700, 2750, 2708), initial["ETH"], 1, 300
    )
    assert a.status == "RECOUNT" and a.targets == []
