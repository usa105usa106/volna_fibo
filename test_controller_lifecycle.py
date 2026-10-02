import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import SendMessage
import pytest

from bot_handlers import BotController
from services_scanner import RunResult, ScannerService
from services_scheduler import DynamicScheduler
from conftest import message, state

pytestmark = pytest.mark.asyncio


def setup_controller(cfg, repo):
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


async def drain(controller):
    await asyncio.wait_for(asyncio.gather(*list(controller.background_tasks)), 5)


async def test_second_chat_cannot_start_search_while_first_report_pending(cfg, repo):
    await repo.replace_active_session("binance_spot", 100, [state()])
    controller, scanner = setup_controller(cfg, repo)
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(*args, **kwargs):
        entered.set()
        await release.wait()

    controller.bot.send_message = send
    task = asyncio.create_task(controller.run_and_report("search", 1))
    await asyncio.wait_for(entered.wait(), 2)
    assert [s.symbol for s in await repo.tracked_states()] == ["OLDUSDT"]
    try:
        await controller._action(message(2), "search")
        await controller._action(message(3), "track")
        assert scanner.search.await_count == 1
        assert not controller.background_tasks
    finally:
        release.set()
        await task
        await drain(controller)
    assert [s.symbol for s in await repo.tracked_states()] == ["NEWUSDT"]
    assert (await repo.get_settings()).last_run_search is not None


async def test_repeated_clicks_are_reserved_before_background_task_starts(cfg, repo):
    controller, scanner = setup_controller(cfg, repo)
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked():
        entered.set()
        await release.wait()
        return RunResult("search", [], 100, 0, [], "binance_spot", 100)

    scanner.search = AsyncMock(side_effect=blocked)
    try:
        await asyncio.gather(
            *(controller._action(message(i), "search") for i in range(10))
        )
        await asyncio.wait_for(entered.wait(), 2)
        assert scanner.search.await_count == 1
        assert len(controller.background_tasks) == 1
    finally:
        release.set()
        await drain(controller)


async def test_reset_cancels_active_top300_and_no_late_commit(cfg, repo):
    await repo.set_top_n(300)
    await repo.replace_active_session("binance_spot", 300, [state()])
    started = asyncio.Event()
    active = 0

    async def snapshot(*args):
        nonlocal active
        active += 1
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    data = SimpleNamespace(
        universe=AsyncMock(return_value=[(f"C{i}USDT", 1) for i in range(300)]),
        snapshot=snapshot,
        reset_runtime_state=AsyncMock(),
    )
    scanner = ScannerService(cfg, repo, data)
    controller = BotController(cfg, repo, scanner)
    controller.bot = SimpleNamespace(send_message=AsyncMock())
    scheduler = DynamicScheduler(repo, controller.scheduled_run)
    controller.set_scheduler(scheduler)
    scheduler.start()
    try:
        await controller._action(message(1), "search")
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(controller._reset(message(2)), 3)
        assert active == 0 and not scanner.busy and not controller.busy
        assert await repo.active_session() is None
        settings = await repo.get_settings()
        assert settings.top_n == 100 and settings.mode == "idle"
        assert settings.last_run_search is None
        assert controller.report_chats == {2}
        assert await repo.report_chats() == {2}
        assert controller.bot.send_message.await_count == 0
        assert scheduler._task is not None and not scheduler._task.done()
    finally:
        await controller.shutdown()


async def test_reset_is_not_blocked_by_initial_telegram_ack_retry(cfg, repo):
    controller, scanner = setup_controller(cfg, repo)
    entered = asyncio.Event()
    first = message(1)

    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    first.answer = blocked
    action = asyncio.create_task(controller._action(first, "search"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(controller._reset(message(2)), 2)
        await action
        assert scanner.search.await_count == 0
        assert (await repo.get_settings()).mode == "idle"
    finally:
        action.cancel()
        await asyncio.gather(action, return_exceptions=True)
        await controller.shutdown()


async def test_walk_arriving_during_reset_does_not_start_after_reset(cfg, repo):
    controller, scanner = setup_controller(cfg, repo)
    scanner.walk_forward = AsyncMock()
    entered, release = asyncio.Event(), asyncio.Event()

    async def reset_runtime():
        entered.set()
        await release.wait()

    scanner.reset_runtime_state = reset_runtime
    reset = asyncio.create_task(controller._reset(message(1)))
    await asyncio.wait_for(entered.wait(), 2)
    walk = asyncio.create_task(controller._walk(message(2)))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(reset, walk)
    await drain(controller)
    assert scanner.walk_forward.await_count == 0
    assert (await repo.get_settings()).mode == "idle"


async def test_failed_delivery_preserves_old_set_and_anchor(cfg, repo):
    await repo.replace_active_session(
        "binance_spot", 100, [state()], completed_at="2026-01-01T00:00:00+00:00"
    )
    before = asdict(await repo.get_settings())
    controller, scanner = setup_controller(cfg, repo)
    controller.bot.send_message = AsyncMock(
        side_effect=TelegramNetworkError(
            method=SendMessage(chat_id=1, text="x"), message="offline"
        )
    )
    assert await controller.run_and_report("search", 1) is False
    assert [s.symbol for s in await repo.tracked_states()] == ["OLDUSDT"]
    assert asdict(await repo.get_settings()) == before
    assert not controller.busy


async def test_successful_delivery_anchor_is_after_final_send(cfg, repo):
    controller, scanner = setup_controller(cfg, repo)
    completed = []

    async def send(*args, **kwargs):
        completed.append(datetime.now(timezone.utc))

    controller.bot.send_message = send
    assert await controller.run_and_report("search", 1)
    settings = await repo.get_settings()
    assert datetime.fromisoformat(settings.last_run_search) >= completed[-1]


async def test_broadcast_blocked_chat_does_not_starve_healthy_chat(cfg, repo):
    controller, scanner = setup_controller(cfg, repo)
    for cid in (1, 2):
        await repo.add_report_chat(cid)
    await controller.restore_report_chats()
    sent = []

    async def send(cid, *args, **kwargs):
        if cid == 1:
            raise TelegramForbiddenError(
                method=SendMessage(chat_id=cid, text="x"), message="blocked"
            )
        sent.append(cid)

    controller.bot.send_message = send
    assert await controller.scheduled_run("search")
    assert sent == [2]
    assert await repo.report_chats() == {2}
    assert controller.report_chats == {2}


async def test_temporary_broadcast_failure_preserves_all_recipient_commit_policy(
    cfg, repo
):
    controller, scanner = setup_controller(cfg, repo)
    controller.report_chats = {1, 2}
    await repo.replace_active_session("binance_spot", 100, [state()])
    sent = []

    async def send(cid, *args, **kwargs):
        if cid == 1:
            raise TelegramNetworkError(
                method=SendMessage(chat_id=cid, text="x"), message="offline"
            )
        sent.append(cid)

    controller.bot.send_message = send
    assert await controller.scheduled_run("search") is False
    assert sent == [2]
    assert [s.symbol for s in await repo.tracked_states()] == ["OLDUSDT"]
    assert (await repo.get_settings()).last_run_search is None


async def test_report_destinations_survive_controller_restart(cfg, repo):
    controller, _ = setup_controller(cfg, repo)
    await controller._guard(message(42))
    restarted, _ = setup_controller(cfg, repo)
    await restarted.restore_report_chats()
    assert restarted.report_chats == {42}


async def test_telegram_retry_after_is_not_shortened(cfg, repo, monkeypatch):
    controller, _ = setup_controller(cfg, repo)
    call = AsyncMock(
        side_effect=[
            TelegramRetryAfter(
                method=SendMessage(chat_id=1, text="x"),
                message="limited",
                retry_after=120,
            ),
            "ok",
        ]
    )
    sleep = AsyncMock()
    monkeypatch.setattr("bot_handlers.asyncio.sleep", sleep)
    assert await controller._tg_call(call) == "ok"
    assert sleep.await_args.args[0] >= 120


async def test_reset_interrupts_telegram_retry_sleep(cfg, repo):
    controller, scanner = setup_controller(cfg, repo)
    entered = asyncio.Event()

    async def send(*args, **kwargs):
        entered.set()
        raise TelegramRetryAfter(
            method=SendMessage(chat_id=1, text="x"), message="limited", retry_after=120
        )

    controller.bot.send_message = send
    await controller._action(message(1), "search")
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(controller._reset(message(2)), 2)
    assert await repo.active_session() is None and not controller.busy


async def test_cooldown_expired_chat_entries_are_pruned(cfg, repo):
    controller, _ = setup_controller(cfg, repo)
    controller.cooldowns = {i: 0 for i in range(1000)}
    assert controller._cooldown_remaining(1) == 0
    assert not controller.cooldowns
