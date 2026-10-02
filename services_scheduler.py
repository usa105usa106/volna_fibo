from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
import logging

from db_repository import Repository

log = logging.getLogger(__name__)


class DynamicScheduler:
    """Dispatch from the last successfully delivered report's persisted timestamp."""

    def __init__(self, repo: Repository, callback: Callable[[str], Awaitable[bool]]):
        self.repo = repo
        self.callback = callback
        self.wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._held = False

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="dynamic-scheduler")

    async def stop(self):
        task = self._task
        self._task = None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._held = False
        self.wake.clear()

    def changed(self):
        self.wake.set()

    def hold(self):
        self._held = True
        self.wake.set()

    def release(self):
        self._held = False
        self.wake.set()

    async def _wait(self, seconds: float):
        try:
            await asyncio.wait_for(self.wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _loop(self):
        while True:
            # Clear BEFORE asynchronous reads so a concurrent change cannot be lost.
            self.wake.clear()
            try:
                if self._held:
                    await self.wake.wait()
                    continue
                settings = await self.repo.get_settings()
                if self.wake.is_set() or self._held:
                    continue
                if settings.mode not in {"search", "track"}:
                    await self.wake.wait()
                    continue
                now = datetime.now(timezone.utc)
                last = settings.last_report_for_mode()
                if last is None:
                    # Failed/manual requests cannot create an automatic countdown.
                    # A delivered report (or a mode change) will wake this loop.
                    await self.wake.wait()
                    continue
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                due = last + timedelta(minutes=settings.interval_minutes)
                delay = max(0.0, (due - now).total_seconds())
                if delay > 0:
                    await self._wait(delay)
                    if self.wake.is_set():
                        continue
                if self._held:
                    continue
                completed = await self.callback(settings.mode)
                if not completed:
                    await self._wait(30.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduler iteration failed; retrying")
                await self._wait(30.0)
