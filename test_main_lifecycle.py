import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import main as entrypoint
from bot_handlers import BotController

pytestmark = pytest.mark.asyncio


async def test_polling_shutdown_drains_work_before_closing_sessions(cfg, monkeypatch):
    events = []
    holder = {}
    working = asyncio.Event()

    async def close_data():
        events.append("data_closed")

    async def close_bot():
        events.append("bot_closed")

    data = SimpleNamespace(close=close_data)
    bot = SimpleNamespace(session=SimpleNamespace(close=close_bot))

    def controller_factory(*args):
        holder["controller"] = BotController(*args)
        return holder["controller"]

    async def worker():
        working.set()
        try:
            await asyncio.Event().wait()
        finally:
            events.append("work_cancelled")

    class Dispatcher:
        def include_router(self, router):
            pass

        def resolve_used_update_types(self):
            return ["message"]

        async def start_polling(self, bot, **kwargs):
            assert kwargs["close_bot_session"] is False
            holder["controller"]._spawn(worker(), name="shutdown-test")
            await working.wait()
            raise OSError("polling stopped")

    monkeypatch.setattr(entrypoint, "Settings", lambda: cfg)
    monkeypatch.setattr(entrypoint, "MarketDataService", lambda cfg: data)
    monkeypatch.setattr(entrypoint, "Bot", lambda token: bot)
    monkeypatch.setattr(entrypoint, "Dispatcher", Dispatcher)
    monkeypatch.setattr(entrypoint, "BotController", controller_factory)
    with pytest.raises(OSError, match="polling stopped"):
        await entrypoint.main()
    assert events == ["work_cancelled", "bot_closed", "data_closed"]
    assert holder["controller"].scheduler._task is None


async def test_startup_failure_closes_already_created_exchange_client(cfg, monkeypatch):
    close = AsyncMock()
    monkeypatch.setattr(entrypoint, "Settings", lambda: cfg)
    monkeypatch.setattr(
        entrypoint, "MarketDataService", lambda cfg: SimpleNamespace(close=close)
    )

    def bad_bot(token):
        raise ValueError("bad token")

    monkeypatch.setattr(entrypoint, "Bot", bad_bot)
    with pytest.raises(ValueError, match="bad token"):
        await entrypoint.main()
    close.assert_awaited_once()
