from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack

from aiogram import Bot, Dispatcher

from bot_handlers import BotController
from config import Settings
from core_models import AppSettings
from data_collector import MarketDataService
from db_repository import Repository
from services_runtime import cleanup_runtime_files
from services_scanner import ScannerService
from services_scheduler import DynamicScheduler


async def main():
    cfg = Settings()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    cfg.data_dir.mkdir(parents=True, exist_ok=True)

    # v0013 never persists candles. Clean known temporary/legacy artifacts on boot.
    cleanup_runtime_files(cfg.data_dir, cfg.db_path)

    defaults = AppSettings(
        top_n=cfg.default_top_n,
        interval_minutes=cfg.default_interval_minutes,
        exchange=cfg.default_exchange,
        mode="idle",
    )
    repo = Repository(cfg.db_path, defaults)
    await repo.init()
    async with AsyncExitStack() as stack:
        data = MarketDataService(cfg)
        stack.push_async_callback(data.close)
        scanner = ScannerService(cfg, repo, data)
        bot = Bot(cfg.bot_token)
        stack.push_async_callback(bot.session.close)
        dp = Dispatcher()
        controller = BotController(cfg, repo, scanner)
        scheduler = DynamicScheduler(repo, controller.scheduled_run)
        controller.set_scheduler(scheduler)
        stack.push_async_callback(controller.shutdown)
        await controller.restore_report_chats()
        controller.attach(dp, bot)
        scheduler.start()
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), close_bot_session=False)


if __name__ == "__main__":
    asyncio.run(main())
