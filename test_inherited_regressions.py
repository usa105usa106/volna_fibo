import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import threading

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.types import Chat, Message, Update, User
import aiosqlite
import httpx
import pandas as pd
import pytest

from bot_handlers import BotController
from core_models import AppSettings, MarketSnapshot
from core_senior import SeniorWaveDetector
from data_exchanges import BinanceSpotClient, MexcFuturesClient
from data_integrity import DataIntegrityError, IntegrityPolicy, validate_candles
from services_scanner import RunResult, ScannerService
from services_scheduler import DynamicScheduler
from services_tasks import gather_owned, run_cpu
from conftest import frame, message, state


def controller_for(cfg, repo):
    data = SimpleNamespace(reset_runtime_state=AsyncMock())
    scanner = ScannerService(cfg, repo, data)
    scanner.search = AsyncMock(
        return_value=RunResult(
            "search", [state("NEWUSDT")], 100, 1, [], "binance_spot", 100
        )
    )
    controller = BotController(cfg, repo, scanner)
    controller.bot = SimpleNamespace(send_message=AsyncMock())
    return controller, scanner


@pytest.mark.asyncio
async def test_cancel_before_first_task_step_releases_admission(cfg, repo):
    controller, scanner = controller_for(cfg, repo)
    scheduler = DynamicScheduler(repo, controller.scheduled_run)
    controller.set_scheduler(scheduler)
    await controller._action(message(), "search")
    tasks = list(controller.background_tasks)
    assert len(tasks) == 1 and controller.busy
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0)
    assert not controller.busy
    assert not scheduler._held
    scanner.search.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["Поиск W2/W3-(2)", "doge,pol,sol", "/walk"])
async def test_router_request_queued_during_reset_is_discarded(cfg, repo, text):
    controller, scanner = controller_for(cfg, repo)
    scanner.analyze_symbols = AsyncMock()
    scanner.walk_forward = AsyncMock()
    controller._format_walk = lambda result: "walk diagnostic"
    entered, release = asyncio.Event(), asyncio.Event()

    async def reset_runtime():
        entered.set()
        await release.wait()

    scanner.reset_runtime_state = reset_runtime
    controller._answer = AsyncMock()
    controller._kbd = AsyncMock(return_value=None)
    reset = asyncio.create_task(controller._reset(message(1)))
    await asyncio.wait_for(entered.wait(), 2)
    handler = (
        controller.router.message.handlers[1].callback
        if text == "/walk"
        else controller.router.message.handlers[-1].callback
    )
    request = asyncio.create_task(handler(message(2, text)))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(reset, request)
    await asyncio.gather(*list(controller.background_tasks))
    scanner.search.assert_not_awaited()
    scanner.analyze_symbols.assert_not_awaited()
    scanner.walk_forward.assert_not_awaited()
    assert (await repo.get_settings()).mode == "idle"
    assert controller.report_chats == {1}
    assert await repo.report_chats() == {1}


@pytest.mark.asyncio
async def test_scheduler_does_not_invent_anchor_without_success(monkeypatch):
    clock = datetime(2026, 1, 1, tzinfo=timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock

    monkeypatch.setattr("services_scheduler.datetime", Clock)
    called = asyncio.Event()

    async def callback(mode):
        called.set()
        return False

    repo = SimpleNamespace(
        get_settings=AsyncMock(
            return_value=AppSettings(mode="search", interval_minutes=30)
        )
    )
    scheduler = DynamicScheduler(repo, callback)

    async def fast_wait(seconds):
        nonlocal clock
        clock += timedelta(seconds=seconds)
        await asyncio.sleep(0.001)

    scheduler._wait = fast_wait
    scheduler.start()
    try:
        await asyncio.sleep(0.02)
        assert not called.is_set()
        scheduler.changed()
        await asyncio.sleep(0.02)
        assert not called.is_set()
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_no_active_session_notification_is_not_successful_analysis(cfg, repo):
    controller, _ = controller_for(cfg, repo)
    result = RunResult("track", [], 0, 0, ["NO_ACTIVE_SEARCH_SESSION"], "", 0)
    assert not await controller._report(result, 1)
    assert controller.bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_track_refuses_to_move_confirmed_checkpoint_backwards(cfg, repo):
    previous = state()
    previous.last_complete4h_bucket = "2026-01-01T08:00:00+00:00"
    await repo.replace_active_session("binance_spot", 100, [previous])
    snap = MarketSnapshot(
        previous.symbol,
        previous.exchange,
        1,
        10.5,
        9,
        frame(periods=8),
        frame(periods=40, freq="D"),
    )
    data = SimpleNamespace(snapshot=AsyncMock(return_value=snap))
    result = await ScannerService(cfg, repo, data).track()
    assert result.states[0].status == "DATA_INCOMPLETE"
    assert (await repo.tracked_states())[0].to_dict() == previous.to_dict()


@pytest.mark.parametrize(
    "missing", ["strict_origin", "origin", "impulse_high", "working_low"]
)
def test_recount_drops_obsolete_targets_and_entry_zones(missing):
    previous = state()
    previous.base_zone, previous.deep_zone = (8.0, 9.0), (6.0, 7.0)
    setattr(previous, missing, None)
    snap = MarketSnapshot(
        previous.symbol, previous.exchange, 1, 10.5, 9, frame(periods=8), pd.DataFrame()
    )
    result = SeniorWaveDetector().track(previous, snap, 100)
    assert result.status == "RECOUNT"
    assert result.targets == []
    assert result.base_zone is None and result.deep_zone is None
    assert result.rating == 0


def chronology_snapshot(valid_correction=False):
    rows = []
    for day in range(50):
        if day < 10:
            low, high = 150 - day * 4, 160 - day * 4
        elif day == 10:
            low, high = 100, 115
        elif day < 20:
            low = 115 + (day - 11) * 7
            high = low + 10
        elif day == 20:
            low, high = 120, 190
        else:
            low = 188 - (day - 21) * 0.2
            high = low + 5
        if valid_correction and day >= 21:
            low, high = 125, 135
        for hour in range(24):
            lo, hi = (185, 200) if day == 20 and hour >= 20 else (low, high)
            ts = pd.Timestamp("2026-01-01", tz="UTC") + timedelta(days=day, hours=hour)
            rows.append([ts, (lo + hi) / 2, hi, lo, (lo + hi) / 2, 1.0])
    h1 = pd.DataFrame(
        rows, columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    daily = (
        h1.set_index("timestamp")
        .resample("1d")
        .agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        )
        .reset_index()
    )
    return MarketSnapshot(
        "TESTUSDT",
        "binance_spot",
        1,
        float(h1.close.iloc[-1]),
        float(h1.low.iloc[-1]),
        h1,
        daily,
    )


def test_w2_low_before_actual_w1_high_is_not_a_valid_correction():
    assert SeniorWaveDetector().detect(chronology_snapshot(), 1, 100) is None


def test_real_correction_after_daily_high_remains_eligible():
    result = SeniorWaveDetector().detect(chronology_snapshot(True), 1, 100)
    assert result is not None and result.wave_type == "W2"
    assert pd.Timestamp(result.impulse_high_ts) == pd.Timestamp("2026-01-21T20:00:00Z")
    assert pd.Timestamp(result.working_low_ts) > pd.Timestamp(result.impulse_high_ts)
    assert result.working_low == 125


@pytest.mark.asyncio
async def test_mexc_ranking_never_substitutes_contract_count_for_turnover():
    data = [
        {"symbol": "DOGE_USDT", "amount24": 0, "volume24": "99999999"},
        {"symbol": "POL_USDT", "volume24": "99999999"},
        {"symbol": "SOL_USDT", "amount24": "12", "volume24": "1"},
    ]
    payload = {"success": True, "code": 0, "data": data}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as http:
        rows = await MexcFuturesClient(http).top_symbols(100)
    assert rows == [("SOL_USDT", 12.0)]


@pytest.mark.asyncio
@pytest.mark.parametrize("exchange", ["binance", "mexc"])
async def test_infinite_turnover_cannot_enter_crypto_universe(exchange):
    def handler(request):
        if exchange == "mexc":
            payload = {
                "success": True,
                "code": 0,
                "data": [
                    {"symbol": "DOGE_USDT", "amount24": "1e309"},
                    {"symbol": "SOL_USDT", "amount24": "12"},
                ],
            }
        elif request.url.path.endswith("exchangeInfo"):
            payload = {
                "symbols": [
                    {
                        "symbol": s + "USDT",
                        "baseAsset": s,
                        "quoteAsset": "USDT",
                        "status": "TRADING",
                    }
                    for s in ("DOGE", "SOL")
                ]
            }
        else:
            payload = [
                {"symbol": "DOGEUSDT", "quoteVolume": "1e309"},
                {"symbol": "SOLUSDT", "quoteVolume": "12"},
            ]
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = (
            BinanceSpotClient(http)
            if exchange == "binance"
            else MexcFuturesClient(http)
        )
        rows = await client.top_symbols(100)
    assert rows == [("SOLUSDT" if exchange == "binance" else "SOL_USDT", 12.0)]


def test_control_daily_gaps_must_remain_on_one_candle_grid():
    rows = frame(periods=2, freq="25h")
    with pytest.raises(DataIntegrityError):
        validate_candles(
            rows,
            IntegrityPolicy("1d", True, False),
            datetime(2026, 1, 5, tzinfo=timezone.utc),
        )


@pytest.mark.asyncio
async def test_repeated_cancellation_does_not_orphan_cpu_thread():
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def work():
        entered.set()
        try:
            release.wait(3)
        finally:
            finished.set()

    task = asyncio.create_task(run_cpu(work))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done() and not finished.is_set()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert finished.is_set() and task.cancelled()


@pytest.mark.asyncio
async def test_repeated_cancellation_still_waits_for_child_cleanup():
    entered, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))

    async def work():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            cleaned.set()

    task = asyncio.create_task(gather_owned(work()))
    try:
        await entered.wait()
        task.cancel()
        await cleaning.wait()
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done() and not cleaned.is_set()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert cleaned.is_set() and task.cancelled()


def test_persisted_recount_from_v0008_cannot_keep_old_trade_levels():
    previous = state()
    previous.status = "RECOUNT"
    previous.base_zone, previous.deep_zone = (8, 9), (6, 7)
    previous.fibs = {"0.500": 12.5}
    snap = MarketSnapshot(
        previous.symbol, previous.exchange, 1, 10.5, 9, frame(), pd.DataFrame()
    )
    result = SeniorWaveDetector().track(previous, snap, 100)
    assert result.targets == [] and result.fibs == {}
    assert result.base_zone is None and result.deep_zone is None
    assert result.rating == 0


def test_control_daily_weekend_gap_and_timezone_offset_remain_allowed():
    rows = frame("2026-01-01T16:00:00", periods=2, freq="3D")
    checked = validate_candles(
        rows,
        IntegrityPolicy("1d", True, False),
        datetime(2026, 1, 5, 16, tzinfo=timezone.utc),
    )
    assert len(checked) == 2
    assert checked.timestamp.dt.hour.tolist() == [16, 16]


@pytest.mark.asyncio
async def test_owned_gather_preserves_first_error_after_repeated_cancellation():
    started, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))

    async def fail():
        await started.wait()
        raise DataIntegrityError("original failure")

    async def sibling():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            cleaned.set()

    task = asyncio.create_task(gather_owned(fail(), sibling()))
    try:
        await cleaning.wait()
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(DataIntegrityError, match="original failure"):
        await task
    assert cleaned.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exchange,expected",
    [
        ("binance_spot", ["DOGEUSDT", "POLUSDT", "SOLUSDT"]),
        ("mexc_futures", ["DOGE_USDT", "POL_USDT", "SOL_USDT"]),
    ],
)
async def test_lowercase_manual_input_through_dispatcher_is_isolated(
    cfg, repo, exchange, expected
):
    sent = []

    class Session(BaseSession):
        async def close(self):
            pass

        async def make_request(self, bot, method, timeout=None):
            sent.append(method)
            return Message(
                message_id=len(sent),
                date=datetime.now(timezone.utc),
                chat=Chat(id=1, type="private"),
                text="ok",
            )

        async def stream_content(self, *args, **kwargs):
            for chunk in ():
                yield chunk

    async def snapshot(exchange, symbol, quote_volume):
        return MarketSnapshot(
            symbol,
            exchange,
            quote_volume,
            10.5,
            9,
            frame(),
            frame(periods=40, freq="D"),
        )

    data = SimpleNamespace(
        universe=AsyncMock(return_value=[(f"C{i}USDT", 1) for i in range(100)]),
        snapshot=AsyncMock(side_effect=snapshot),
    )
    scanner = ScannerService(cfg, repo, data)
    controller = BotController(cfg, repo, scanner)
    bot = Bot("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi", session=Session())
    dp = Dispatcher()
    controller.attach(dp, bot)
    await repo.set_exchange(exchange)
    await repo.set_mode("track")
    await repo.mark_report_complete("track", "2026-01-01T00:00:00+00:00")
    await repo.replace_active_session("binance_spot", 100, [state()])
    before = (
        await repo.get_settings(),
        await repo.active_session(),
        await repo.tracked_states(),
    )
    incoming = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=1, type="private"),
        from_user=User(id=1, is_bot=False, first_name="test"),
        text="doge,pol,sol",
    )
    try:
        await dp.feed_update(bot, Update(update_id=1, message=incoming))
        await asyncio.gather(*list(controller.background_tasks))
        assert [call.args[1] for call in data.snapshot.await_args_list] == expected
        assert (
            await repo.get_settings(),
            await repo.active_session(),
            await repo.tracked_states(),
        ) == before
        assert any("DOGE, POL, SOL" in getattr(method, "text", "") for method in sent)
    finally:
        await controller.shutdown()
        await bot.session.close()


@pytest.mark.asyncio
async def test_cancelled_sqlite_search_transaction_restores_previous_set(
    repo, monkeypatch
):
    old = await repo.replace_active_session("binance_spot", 100, [state()])
    before = await repo.get_settings(), await repo.tracked_states()
    entered = asyncio.Event()
    original = aiosqlite.Connection.execute

    async def block_insert(connection, sql, parameters=None):
        if sql.startswith("INSERT INTO tracked_setups"):
            entered.set()
            await asyncio.Event().wait()
        return await original(connection, sql, parameters or ())

    monkeypatch.setattr(aiosqlite.Connection, "execute", block_insert)
    task = asyncio.create_task(
        repo.replace_active_session(
            "mexc_futures",
            300,
            [state("NEW")],
            completed_at="2026-02-01T00:00:00+00:00",
        )
    )
    await entered.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    monkeypatch.setattr(aiosqlite.Connection, "execute", original)
    assert task.cancelled()
    assert (await repo.active_session())[0] == old
    assert (await repo.get_settings(), await repo.tracked_states()) == before


@pytest.mark.asyncio
async def test_reset_cancels_scheduled_top300_and_drains_all_workers(cfg, repo):
    entered = asyncio.Event()
    active = 0

    async def snapshot(*args):
        nonlocal active
        active += 1
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    async def reset_runtime():
        assert active == 0

    data = SimpleNamespace(
        universe=AsyncMock(return_value=[(f"C{i}USDT", 1) for i in range(300)]),
        snapshot=snapshot,
        reset_runtime_state=reset_runtime,
    )
    scanner = ScannerService(cfg, repo, data)
    controller = BotController(cfg, repo, scanner)
    controller.bot = SimpleNamespace(send_message=AsyncMock())
    controller.report_chats = {1}
    await repo.set_top_n(300)
    await repo.set_mode("search")
    await repo.replace_active_session(
        "binance_spot", 300, [state()], completed_at="2026-01-01T00:00:00+00:00"
    )
    scheduler = DynamicScheduler(repo, controller.scheduled_run)
    controller.set_scheduler(scheduler)
    scheduler.start()
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(controller._reset(message(2)), 3)
        assert active == 0 and not scanner.busy and not controller.busy
        assert await repo.active_session() is None
        settings = await repo.get_settings()
        assert settings.mode == "idle" and settings.top_n == 100
        assert settings.last_run_search is None
        assert controller.report_chats == {2}
        assert controller.bot.send_message.await_count == 0
        assert scheduler._task is not None and not scheduler._task.done()
    finally:
        await controller.shutdown()
