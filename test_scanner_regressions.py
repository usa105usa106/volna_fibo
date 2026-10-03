import asyncio
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pandas as pd
import pytest

from bot_handlers import BotController
from core_models import MarketSnapshot
from data_integrity import DataIntegrityError
from services_scanner import ScannerService, SearchIncompleteError
from conftest import frame, message, state

pytestmark = pytest.mark.asyncio


def fake_data():
    return SimpleNamespace(
        universe=AsyncMock(return_value=[(f"C{i}USDT", 1) for i in range(100)]),
        snapshot=AsyncMock(
            side_effect=lambda exchange, symbol, qv: SimpleNamespace(
                symbol=symbol, live_price=10.5
            )
        ),
        available_controls=AsyncMock(return_value={}),
        reset_runtime_state=AsyncMock(),
    )


async def test_detector_failures_cannot_commit_empty_success(cfg, repo):
    await repo.replace_active_session("binance_spot", 100, [state()])
    scanner = ScannerService(cfg, repo, fake_data())
    scanner.detector = SimpleNamespace(
        detect=Mock(side_effect=ValueError("injected detector failure"))
    )
    with pytest.raises(SearchIncompleteError):
        await scanner.search()
    assert [s.symbol for s in await repo.tracked_states()] == ["OLDUSDT"]


async def test_search_without_xau_usoil_preserves_old_until_explicit_commit(cfg, repo):
    await repo.replace_active_session("binance_spot", 100, [state()])
    scanner = ScannerService(cfg, repo, fake_data())
    scanner.detector = SimpleNamespace(
        detect=lambda snapshot, *args: state(snapshot.symbol)
    )
    result = await scanner.search()
    assert result.skipped_controls == ["XAU", "USOIL"]
    assert len(result.states) == 10 and result.successful_crypto_snapshots == 100
    assert [s.symbol for s in await repo.tracked_states()] == ["OLDUSDT"]
    await scanner.commit_search(result)
    assert len(await repo.tracked_states()) == 10


async def test_search_data_outage_rejected_at_existing_fraction_threshold(cfg, repo):
    data = fake_data()

    def snapshot(exchange, symbol, qv):
        if int(symbol[1:-4]) >= 79:
            raise DataIntegrityError("gap")
        return SimpleNamespace(symbol=symbol, live_price=10.5)

    data.snapshot = AsyncMock(side_effect=snapshot)
    scanner = ScannerService(cfg, repo, data)
    scanner.detector = SimpleNamespace(detect=lambda *args: None)
    with pytest.raises(SearchIncompleteError, match="79/100"):
        await scanner.search()


async def test_tracking_bad_data_keeps_confirmed_state_bytes(cfg, repo):
    original = state()
    await repo.replace_active_session("binance_spot", 100, [original])
    data = fake_data()
    data.snapshot = AsyncMock(side_effect=DataIntegrityError("missing candle"))
    result = await ScannerService(cfg, repo, data).track()
    assert result.states[0].status == "DATA_INCOMPLETE"
    assert (await repo.tracked_states())[0].to_dict() == original.to_dict()


async def test_tracking_requires_coverage_since_previous_confirmed_bucket(cfg, repo):
    original = state()
    await repo.replace_active_session("binance_spot", 100, [original])
    data = fake_data()
    data.snapshot = AsyncMock(
        return_value=MarketSnapshot(
            "OLDUSDT",
            "binance_spot",
            1,
            10.5,
            9,
            frame("2026-02-01"),
            frame("2026-01-01", 40, "D"),
        )
    )
    scanner = ScannerService(cfg, repo, data)
    detector = Mock(return_value=state())
    scanner.detector = SimpleNamespace(track=detector)
    result = await scanner.track()
    assert result.states[0].status == "DATA_INCOMPLETE"
    detector.assert_not_called()
    assert (await repo.tracked_states())[0].to_dict() == original.to_dict()


@pytest.mark.parametrize(
    "exchange,expected",
    [
        ("binance_spot", ["DOGEUSDT", "POLUSDT", "SOLUSDT"]),
        ("mexc_futures", ["DOGE_USDT", "POL_USDT", "SOL_USDT"]),
    ],
)
async def test_manual_lowercase_doge_pol_sol_through_handler_is_state_read_only(
    cfg, repo, exchange, expected
):
    await repo.set_exchange(exchange)
    await repo.set_mode("track")
    await repo.mark_report_complete("track", "2026-01-01T00:00:00+00:00")
    await repo.replace_active_session("binance_spot", 100, [state()])
    before = asdict(await repo.get_settings())
    old = (await repo.tracked_states())[0].to_dict()
    data = fake_data()
    scanner = ScannerService(cfg, repo, data)
    scanner.detector = SimpleNamespace(detect=lambda *args: None)
    controller = BotController(cfg, repo, scanner)
    controller.bot = SimpleNamespace(send_message=AsyncMock())
    await controller.router.message.handlers[-1].callback(message(1, "doge,pol,sol"))
    await asyncio.wait_for(asyncio.gather(*list(controller.background_tasks)), 5)
    assert [call.args[1] for call in data.snapshot.await_args_list] == expected
    assert asdict(await repo.get_settings()) == before
    assert (await repo.tracked_states())[0].to_dict() == old
    assert controller.bot.send_message.await_count > 0


async def test_walk_executes_checkpoints_without_state_or_timer_mutation(cfg, repo):
    cfg = cfg.model_copy(
        update={"walk_history_days": 1, "walk_horizon_days": 1}
    )
    await repo.set_mode("search")
    await repo.mark_report_complete("search", "2026-01-01T00:00:00+00:00")
    await repo.replace_active_session("binance_spot", 100, [state()])
    before = asdict(await repo.get_settings())
    old = (await repo.tracked_states())[0].to_dict()
    now = pd.Timestamp.now(tz="UTC").floor("h")
    full = MarketSnapshot(
        "X",
        "binance_spot",
        1,
        10.5,
        9,
        frame(now - pd.Timedelta(days=4), 96),
        frame(now.floor("D") - pd.Timedelta(days=45), 45, "D"),
    )
    data = fake_data()
    data.walk_history = AsyncMock(return_value=full)
    scanner = ScannerService(cfg, repo, data)
    seen = []

    def detect(snapshot, *args):
        last = snapshot.hourly_closed.timestamp.max() + pd.Timedelta(hours=1)
        assert now - pd.Timedelta(days=2) <= last <= now - pd.Timedelta(days=1)
        assert last.hour % 4 == 0
        assert (snapshot.daily_closed.timestamp + pd.Timedelta(days=1)).max() <= last
        assert snapshot.live_price == snapshot.hourly_closed.close.iloc[-1]
        seen.append(last)
        return None

    scanner.detector = SimpleNamespace(detect=detect)
    result = await scanner.walk_forward()
    assert result.assets_tested == 10
    assert result.complete4h_checkpoints > 0
    assert len(seen) == result.complete4h_checkpoints * 10
    assert asdict(await repo.get_settings()) == before
    assert (await repo.tracked_states())[0].to_dict() == old


async def test_cpu_work_does_not_block_event_loop_and_is_drained_on_cancel(cfg, repo):
    import threading
    from services_tasks import run_cpu

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def work():
        entered.set()
        try:
            release.wait(3)
        finally:
            finished.set()

    task = asyncio.create_task(run_cpu(work))
    try:
        for _ in range(200):
            if entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert entered.is_set()
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done() and not finished.is_set()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert finished.is_set() and task.cancelled()


async def test_walk_counts_same_living_structure_once_and_tracks_duplicates(cfg, repo):
    cfg = cfg.model_copy(update={"walk_history_days": 1, "walk_horizon_days": 1})
    now = pd.Timestamp.now(tz="UTC").floor("h")
    full = MarketSnapshot(
        "X",
        "binance_spot",
        1,
        10.5,
        9,
        frame(now - pd.Timedelta(days=4), 96),
        frame(now.floor("D") - pd.Timedelta(days=45), 45, "D"),
    )
    data = fake_data()
    data.walk_history = AsyncMock(return_value=full)
    scanner = ScannerService(cfg, repo, data)

    stable = state()
    stable.rating = 8.5
    stable.current_price = 10.5
    stable.targets = [24.0]
    stable.strict_origin = 5.0
    stable.impulse_start_ts = "2026-01-01T00:00:00+00:00"
    stable.impulse_high_ts = "2026-01-15T00:00:00+00:00"
    scanner.detector = SimpleNamespace(detect=lambda *args: stable)

    result = await scanner.walk_forward()
    assert result.assets_tested == 10
    assert result.signals == 10  # one unique structure per major, never once per COMPLETE4H
    assert result.unresolved == 10
    assert result.duplicate_observations > 0
    assert all(len(asset.signals) == 1 for asset in result.assets)
    assert all(asset.duplicate_observations > 0 for asset in result.assets)


async def test_walk_excludes_setup_already_beyond_t1_from_fresh_success_stats(cfg, repo):
    cfg = cfg.model_copy(update={"walk_history_days": 1, "walk_horizon_days": 1})
    now = pd.Timestamp.now(tz="UTC").floor("h")
    full = MarketSnapshot(
        "X",
        "binance_spot",
        1,
        10.5,
        9,
        frame(now - pd.Timedelta(days=4), 96),
        frame(now.floor("D") - pd.Timedelta(days=45), 45, "D"),
    )
    data = fake_data()
    data.walk_history = AsyncMock(return_value=full)
    scanner = ScannerService(cfg, repo, data)

    extended = state()
    extended.rating = 8.5
    extended.current_price = 10.5
    extended.targets = [10.0]
    extended.strict_origin = 5.0
    extended.impulse_start_ts = "2026-01-01T00:00:00+00:00"
    extended.impulse_high_ts = "2026-01-15T00:00:00+00:00"
    scanner.detector = SimpleNamespace(detect=lambda *args: extended)

    result = await scanner.walk_forward()
    assert result.signals == 0
    assert result.t1_first == 0
    assert result.already_extended == 10
    assert all(len(asset.excluded) == 1 for asset in result.assets)
