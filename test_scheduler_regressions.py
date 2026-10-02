import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core_models import AppSettings
from services_scheduler import DynamicScheduler

pytestmark = pytest.mark.asyncio


async def test_changed_during_db_read_is_not_lost():
    entered, release, called = asyncio.Event(), asyncio.Event(), asyncio.Event()
    reads = 0

    async def settings():
        nonlocal reads
        reads += 1
        if reads == 1:
            entered.set()
            await release.wait()
            return AppSettings(mode="idle")
        return AppSettings(mode="search", last_run_search="2000-01-01T00:00:00+00:00")

    async def callback(mode):
        called.set()
        return False

    scheduler = DynamicScheduler(SimpleNamespace(get_settings=settings), callback)
    scheduler.start()
    try:
        await entered.wait()
        scheduler.changed()
        release.set()
        await asyncio.wait_for(called.wait(), 2)
    finally:
        await scheduler.stop()
    assert reads >= 2


async def test_scheduler_survives_sqlite_read_error_and_backs_off():
    recovered = asyncio.Event()
    reads = 0
    delays = []

    async def settings():
        nonlocal reads
        reads += 1
        if reads == 1:
            raise OSError("temporary DB read failure")
        recovered.set()
        return AppSettings(mode="idle")

    scheduler = DynamicScheduler(SimpleNamespace(get_settings=settings), AsyncMock())

    async def wait(seconds):
        delays.append(seconds)
        await asyncio.sleep(0)

    scheduler._wait = wait
    scheduler.start()
    try:
        await asyncio.wait_for(recovered.wait(), 2)
    finally:
        await scheduler.stop()
    assert delays == [30.0]


async def test_countdown_uses_successful_delivery_anchor_and_live_interval_changes():
    now = datetime.now(timezone.utc)
    settings = AppSettings(
        mode="search", interval_minutes=60, last_run_search=now.isoformat()
    )
    scheduler = DynamicScheduler(
        SimpleNamespace(get_settings=AsyncMock(return_value=settings)), AsyncMock()
    )
    delays = []
    observed = asyncio.Event()

    async def wait(seconds):
        delays.append(seconds)
        observed.set()
        await scheduler.wake.wait()

    scheduler._wait = wait
    scheduler.start()
    try:
        await asyncio.wait_for(observed.wait(), 2)
        assert 3590 < delays[-1] <= 3600
        observed.clear()
        settings.interval_minutes = 30
        scheduler.changed()
        await asyncio.wait_for(observed.wait(), 2)
        assert 1790 < delays[-1] <= 1800
        scheduler.callback.assert_not_awaited()
    finally:
        await scheduler.stop()


async def test_failed_callback_keeps_anchor_and_waits_before_retry():
    settings = AppSettings(
        mode="track",
        last_run_track=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
    )
    old = settings.last_run_track
    observed = asyncio.Event()
    delays = []
    scheduler = DynamicScheduler(
        SimpleNamespace(get_settings=AsyncMock(return_value=settings)),
        AsyncMock(return_value=False),
    )

    async def wait(seconds):
        delays.append(seconds)
        observed.set()
        await asyncio.Event().wait()

    scheduler._wait = wait
    scheduler.start()
    try:
        await asyncio.wait_for(observed.wait(), 2)
        assert delays == [30.0] and settings.last_run_track == old
        assert scheduler.callback.await_count == 1
    finally:
        await scheduler.stop()
