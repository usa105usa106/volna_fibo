from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import os
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import psutil
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, Message

from bot_keyboards import clean_button, keyboard
from core_toggles import EXCHANGE_LABELS, INTERVAL_LABELS, next_exchange, next_interval_minutes, next_top_n
from config import Settings
from db_repository import Repository
from services_formatter import (
    _fmt,
    render_table_png,
    safe_report_filename,
    split_plain,
    technical_report_text,
    telegram_table_messages,
)
from services_scanner import RunResult, ScannerService, WalkForwardResult
from services_scheduler import DynamicScheduler
from services_runtime import cleanup_runtime_files


TICKER_MESSAGE_RE = re.compile(r"^[A-Za-z0-9_\-/]+(?:\s*,\s*[A-Za-z0-9_\-/]+)*$")
T = TypeVar("T")
log = logging.getLogger(__name__)


class BotController:
    def __init__(self, cfg: Settings, repo: Repository, scanner: ScannerService):
        self.cfg = cfg
        self.repo = repo
        self.scanner = scanner
        self.router = Router()
        self.started = time.monotonic()
        self.cooldowns: dict[int, float] = {}
        # No Telegram account whitelist in v0018. Any chat that talks to the bot can use it
        # and becomes a persisted destination for periodic reports.
        self.report_chats: set[int] = set()
        self.scheduler: DynamicScheduler | None = None
        self.bot: Bot | None = None
        self.background_tasks: set[asyncio.Task] = set()
        self.state_lock = asyncio.Lock()
        self.resetting = False
        self._running = False
        self._generation = 0
        self._closing = False
        self._register()

    def set_scheduler(self, scheduler: DynamicScheduler):
        self.scheduler = scheduler

    def _spawn(self, coro: Awaitable[Any], *, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self.background_tasks.add(task)
        def done(completed):
            self.background_tasks.discard(completed)
            if not completed.cancelled() and completed.exception() is not None:
                exc = completed.exception()
                log.error("Background task failed", exc_info=(type(exc), exc, exc.__traceback__))
        task.add_done_callback(done)
        return task

    async def _guard(self, message: Message) -> bool:
        if self.resetting or self._closing:
            return False
        generation = self._generation
        async with self.state_lock:
            if self.resetting or self._closing or generation != self._generation:
                return False
            if message.chat.id not in self.report_chats:
                await self.repo.add_report_chat(message.chat.id)
                self.report_chats.add(message.chat.id)
        return True

    async def _kbd(self):
        return keyboard(await self.repo.get_settings())

    async def _tg_call(self, call: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
        attempts = max(1, self.cfg.telegram_retry_attempts)
        for attempt in range(attempts):
            try:
                return await call(*args, **kwargs)
            except TelegramRetryAfter as exc:
                if attempt >= attempts - 1:
                    raise
                delay = max(float(exc.retry_after), self.cfg.telegram_retry_base_delay_seconds)
                await asyncio.sleep(delay)
            except (TelegramNetworkError, TelegramServerError):
                if attempt >= attempts - 1:
                    raise
                delay = min(12.0, self.cfg.telegram_retry_base_delay_seconds * (2**attempt))
                await asyncio.sleep(delay)
        raise RuntimeError("Telegram retry loop exited unexpectedly")

    async def _answer(self, message: Message, text: str, **kwargs):
        chunks = split_plain(text)
        result = None
        for i, chunk in enumerate(chunks):
            options = dict(kwargs)
            if i < len(chunks) - 1:
                options.pop("reply_markup", None)
            result = await self._tg_call(message.answer, chunk, **options)
        return result

    async def _send_message(self, chat_id: int, text: str, **kwargs):
        if not self.bot:
            raise RuntimeError("bot is not attached")
        return await self._tg_call(self.bot.send_message, chat_id, text, **kwargs)

    def _cooldown_remaining(self, chat_id: int) -> int:
        now = time.monotonic()
        self.cooldowns = {cid: ts for cid, ts in self.cooldowns.items() if now - ts < self.cfg.action_cooldown_seconds}
        last = self.cooldowns.get(chat_id)
        if last is None:
            return 0
        left = self.cfg.action_cooldown_seconds - (time.monotonic() - last)
        return max(0, int(left + 0.999))

    def _register(self):
        r = self.router

        @r.message(CommandStart())
        async def start(message: Message):
            if not await self._guard(message):
                return
            s = await self.repo.get_settings()
            await self._answer(
                message,
                f"🚀 Senior Wave Scanner v{self.cfg.bot_version}\n"
                "Senior only: global W2 / W3-(2).\n"
                "Можно написать тикер: DOGE или DOGE,POL,SOL.\n"
                "/walk — отдельная walk-forward проверка детектора.",
                reply_markup=keyboard(s),
            )

        @r.message(Command("walk"))
        async def walk(message: Message):
            if not await self._guard(message):
                return
            await self._walk(message)

        @r.message(F.text)
        async def on_text(message: Message):
            if not await self._guard(message):
                return
            generation = self._generation
            original = (message.text or "").strip()
            text = clean_button(original)
            if self.resetting and text != "Сброс":
                await self._answer(message, "⏳ Сброс уже выполняется.")
                return
            if text in {"Поиск W2/W3-(2)", "Сопровождение"}:
                await self._action(message, "search" if text.startswith("Поиск") else "track")
                return
            if text in {"Top-100", "Top-200", "Top-300"}:
                async with self.state_lock:
                    if self.resetting or generation != self._generation:
                        return
                    s = await self.repo.get_settings()
                    new_top = next_top_n(s.top_n)
                    await self.repo.set_top_n(new_top)
                    if self.scheduler:
                        self.scheduler.changed()
                await self._answer(message, f"Universe: Top-{new_top}", reply_markup=await self._kbd())
                return
            if text in {"30 мин", "1 час", "4 часа", "12 часов"}:
                async with self.state_lock:
                    if self.resetting or generation != self._generation:
                        return
                    s = await self.repo.get_settings()
                    new_interval = next_interval_minutes(s.interval_minutes)
                    await self.repo.set_interval(new_interval)
                    if self.scheduler:
                        self.scheduler.changed()
                await self._answer(message, f"Интервал: {INTERVAL_LABELS[new_interval]}", reply_markup=await self._kbd())
                return
            if text in {"Binance Spot", "MEXC Futures"}:
                async with self.state_lock:
                    if self.resetting or generation != self._generation:
                        return
                    s = await self.repo.get_settings()
                    new_exchange = next_exchange(s.exchange)
                    await self.repo.set_exchange(new_exchange)
                    if self.scheduler:
                        self.scheduler.changed()
                    session = await self.repo.active_session()
                    note = ""
                    if session and s.mode == "track" and session[1] != new_exchange:
                        note = f"\nТекущее сопровождение остаётся на {session[1]} до нового Поиска."
                await self._answer(message, f"Биржа: {EXCHANGE_LABELS[new_exchange]}{note}", reply_markup=await self._kbd())
                return
            if text == "Сброс":
                await self._reset(message)
                return
            if text == "Пинг":
                await self._ping(message)
                return
            if TICKER_MESSAGE_RE.fullmatch(original):
                await self._manual_tickers(message, [x.strip() for x in original.split(",") if x.strip()])
                return
            await self._answer(
                message,
                "Используй кнопки ниже или напиши тикер: DOGE либо DOGE,POL,SOL.",
                reply_markup=await self._kbd(),
            )

    @property
    def busy(self) -> bool:
        return self._running or self.scanner.busy

    def _finish_run(self):
        self._running = False
        if self.scheduler:
            self.scheduler.release()

    async def restore_report_chats(self):
        self.report_chats = await self.repo.report_chats()

    async def shutdown(self):
        self._closing = True
        self.resetting = True
        self._generation += 1
        async with self.state_lock:
            if self.scheduler:
                await self.scheduler.stop()
            tasks = [task for task in self.background_tasks if not task.done()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _reset(self, message: Message):
        if self.resetting:
            await self._answer(message, "⏳ Сброс уже выполняется.")
            return
        self.resetting = True
        self._generation += 1
        scheduler_stopped = False
        try:
            async with self.state_lock:
                if self.scheduler:
                    await self.scheduler.stop()
                    scheduler_stopped = True
                tasks = [task for task in self.background_tasks if not task.done()]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                defaults = await self.repo.reset_to_defaults(message.chat.id)
                await self.scanner.reset_runtime_state()
                cleanup_runtime_files(self.cfg.data_dir, self.cfg.db_path)
                self.cooldowns.clear()
                self.report_chats.clear()
                self.report_chats.add(message.chat.id)
                self._running = False
            # Network retries never hold the admission/settings mutex.
            await self._answer(
                message,
                "♻️ Сброс выполнен\n\nАвтоанализ остановлен.\n"
                "Сопровождение, временные файлы и оперативное состояние очищены.\n"
                f"Настройки: Top-{defaults.top_n} · {INTERVAL_LABELS[defaults.interval_minutes]} · "
                f"{EXCHANGE_LABELS[defaults.exchange]}.",
                reply_markup=await self._kbd(),
            )
        except Exception:
            log.exception("Reset failed")
            try:
                await self._answer(message, "❌ Ошибка сброса. Подробности в журнале сервера.", reply_markup=await self._kbd())
            except Exception:
                log.exception("Unable to deliver Reset error")
        finally:
            if scheduler_stopped and self.scheduler and not self._closing:
                self.scheduler.start()
            self.resetting = self._closing

    async def _action(self, message: Message, mode: str):
        await self._start_request(message, mode)

    async def _manual_tickers(self, message: Message, symbols: list[str]):
        await self._start_request(message, "manual", symbols)

    async def _walk(self, message: Message):
        await self._start_request(message, "walk")

    async def _start_request(self, message: Message, mode: str, symbols: list[str] | None = None):
        generation = self._generation
        resetting_at_entry = self.resetting or self._closing
        rejection = None
        async with self.state_lock:
            if resetting_at_entry or self.resetting or generation != self._generation:
                rejection = "⏳ Запрос отменён сбросом."
            elif self.busy:
                rejection = "⏳ Анализ или выдача результата уже идёт. Повторный запуск не принят."
            elif mode in {"search", "track"} and (left := self._cooldown_remaining(message.chat.id)):
                rejection = f"⏳ Защита от повторного нажатия: ещё {left} сек."
            elif mode == "track" and await self.repo.active_session() is None:
                rejection = "Сначала нужен «Поиск W2/W3-(2)». Сопровождать пока нечего."
            elif mode == "manual" and len(symbols or []) > 25:
                rejection = "За один ручной запрос максимум 25 тикеров."
            else:
                # active_session() above yields: a scheduled run or Reset may have
                # acquired ownership while SQLite was answering the Track check.
                if self.resetting or generation != self._generation or self.busy:
                    rejection = "⏳ Другой анализ или сброс уже выполняется."
                else:
                    self._running = True
                    if mode in {"search", "track"}:
                        self.cooldowns[message.chat.id] = time.monotonic()
                    if self.scheduler:
                        self.scheduler.hold()
                    task = self._spawn(self._requested_run(message, mode, symbols), name=f"user-{mode}-{message.chat.id}")
                    # A coroutine's finally never executes if it is cancelled before
                    # its first step. Completion owns admission release in every case.
                    task.add_done_callback(lambda completed: self._finish_run())
        if rejection:
            await self._answer(message, rejection, reply_markup=await self._kbd())

    async def _requested_run(self, message: Message, mode: str, symbols: list[str] | None):
        if mode in {"search", "track"}:
            label = "🔍 Поиск с нуля запущен." if mode == "search" else "📈 Сопровождение найденного набора запущено."
            pending_settings = await self.repo.get_settings()
            pending_settings.mode = mode
            await self._answer(message, label, reply_markup=keyboard(pending_settings))
            await self.repo.set_mode(mode)
            await self.run_and_report(mode, message.chat.id, reserved=True)
        elif mode == "manual":
            await self._answer(message, f"🔎 Ручной senior-анализ: {', '.join(s.upper() for s in symbols or [])}", reply_markup=await self._kbd())
            await self.run_manual_and_report(symbols or [], message.chat.id)
        else:
            settings = await self.repo.get_settings()
            await self._answer(
                message,
                f"🧪 /walk запущен: {EXCHANGE_LABELS[settings.exchange]} · 10 majors: "
                "BTC, ETH, SOL, BNB, XRP, DOGE, ADA, LINK, LTC, BCH.\n"
                "Поиск/Сопровождение и их таймер не меняются.",
                reply_markup=await self._kbd(),
            )
            await self.run_walk_and_report(message.chat.id)

    async def run_manual_and_report(self, symbols: list[str], chat_id: int):
        try:
            result = await self.scanner.analyze_symbols(symbols)
            await self._report(result, chat_id)
        except Exception:
            log.exception("Manual analysis/report failed")
            await self._notify_failure(chat_id, "Ошибка ручного анализа/выдачи результата.")

    async def run_walk_and_report(self, chat_id: int):
        try:
            result = await self.scanner.walk_forward()
            report = self._format_walk_txt(result)
            stamp = datetime.now(ZoneInfo(self.cfg.bot_timezone)).strftime("%Y%m%d_%H%M%S")
            filename = safe_report_filename("walk_10majors", stamp)
            caption = (
                f"🧪 /walk готов за {self._format_duration(result.analysis_seconds)}. "
                f"10 majors: {result.assets_tested}/{result.assets_requested} · ошибок: {result.data_errors}. "
                f"Fresh unique: {result.signals} · T1 first: {result.t1_first} · invalid first: {result.invalid_first}. "
                "Файл — полный материал для полировки детектора."
            )
            if not self.bot:
                return
            await self._tg_call(
                self.bot.send_document,
                chat_id,
                BufferedInputFile(report.encode("utf-8"), filename=filename),
                caption=caption,
                reply_markup=await self._kbd(),
            )
        except Exception:
            log.exception("Walk analysis/report failed")
            await self._notify_failure(chat_id, "/walk завершился ошибкой.")

    async def _notify_failure(self, chat_id: int | None, text: str):
        if chat_id is not None:
            try:
                await self._send_message(chat_id, f"❌ {text}")
            except Exception:
                log.exception("Unable to deliver failure notification")

    async def run_and_report(self, mode: str, chat_id: int | None = None, *, reserved: bool = False):
        if not reserved:
            if self.resetting or self.busy:
                return False
            self._running = True
            if self.scheduler:
                self.scheduler.hold()
        try:
            result = await (self.scanner.search() if mode == "search" else self.scanner.track())
            delivered = await self._report(result, chat_id)
            if delivered:
                completed_at = datetime.now(timezone.utc).isoformat()
                if mode == "search":
                    await self.scanner.commit_search(result, completed_at)
                else:
                    await self.repo.mark_report_complete(mode, completed_at)
            return delivered
        except Exception:
            log.exception("%s analysis/report/commit failed", mode)
            await self._notify_failure(chat_id, "Ошибка анализа, выдачи или фиксации результата. Подробности в журнале сервера.")
            return False
        finally:
            if not reserved:
                self._finish_run()

    async def scheduled_run(self, mode: str) -> bool:
        if self.resetting or not self.report_chats or self.busy:
            return False
        # The same synchronous reservation covers scheduled calculation AND delivery.
        self._running = True
        try:
            result = await (self.scanner.search() if mode == "search" else self.scanner.track())
            delivered = await self._report(result, None)
            if delivered:
                completed_at = datetime.now(timezone.utc).isoformat()
                if mode == "search":
                    await self.scanner.commit_search(result, completed_at)
                else:
                    await self.repo.mark_report_complete(mode, completed_at)
            return delivered
        finally:
            # Do not wake our own scheduler on failure: it must apply retry backoff.
            self._running = False

    @staticmethod
    def _format_duration(seconds: float) -> str:
        total = max(0, int(round(seconds)))
        minutes, secs = divmod(total, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours} ч. {minutes} мин. {secs} сек."
        return f"{minutes} мин. {secs} сек."

    @staticmethod
    def _best_coins(result: RunResult, limit: int = 3) -> str:
        valid = [
            state for state in result.states
            if not state.is_control
            and state.rating is not None
            and state.status not in {"INVALID", "RECOUNT", "NO_SETUP", "DATA_INCOMPLETE"}
        ]
        valid.sort(key=lambda state: state.rating or 0.0, reverse=True)
        if not valid:
            return "нет валидных senior-сетапов"
        return ", ".join(f"{state.symbol.replace('_USDT', '').removesuffix('USDT')} ({state.rating:.1f})" for state in valid[:limit])

    def _short_summary(self, result: RunResult) -> str:
        duration = self._format_duration(result.analysis_seconds)
        errors = len(result.errors)
        best = self._best_coins(result)
        if result.mode == "search":
            scope = f"Top-{result.top_n}"
        elif result.mode == "track":
            scope = f"сопровождение {result.found_crypto} crypto"
        else:
            scope = f"тикеров {result.checked}"
        return (
            f"Анализ занял {duration} · {scope} / ошибок: {errors}.\n"
            f"Кратко: лучшие монеты — {best}."
        )

    def _report_titles(self, result: RunResult) -> tuple[str, str, str]:
        exchange_title = EXCHANGE_LABELS.get(result.exchange, result.exchange)
        if result.mode == "search":
            return "ПОИСК W2 / W3-(2)", f"{exchange_title} · Top-{result.top_n}", "TOP-10 CRYPTO"
        if result.mode == "track":
            return "СОПРОВОЖДЕНИЕ", f"{exchange_title} · набор последнего поиска", "CRYPTO ИЗ ПОСЛЕДНЕГО ПОИСКА"
        requested = ", ".join(result.requested_symbols or [])
        return "РУЧНОЙ SENIOR-АНАЛИЗ", f"{exchange_title} · {requested}", "ЗАПРОШЕННЫЕ CRYPTO"

    def _technical_txt(self, result: RunResult, title: str, subtitle: str) -> str:
        return technical_report_text(
            result.states,
            heading=[
                f"SENIOR WAVE BOT v{self.cfg.bot_version}",
                title,
                subtitle,
                f"Анализ: {self._format_duration(result.analysis_seconds)}",
                f"Проверено: {result.checked}; crypto в выдаче: {result.found_crypto}; ошибок: {len(result.errors)}",
            ],
            errors=result.errors,
            skipped_controls=result.skipped_controls,
        )

    async def _report(self, result: RunResult, chat_id: int | None) -> bool:
        if not self.bot:
            return False
        targets = [chat_id] if chat_id else sorted(self.report_chats)
        if not targets:
            return False
        if result.mode == "track" and "NO_ACTIVE_SEARCH_SESSION" in result.errors:
            for cid in targets:
                await self._send_message(cid, "Сопровождать нечего: сначала запусти Поиск.", reply_markup=await self._kbd())
            return False

        title, subtitle, crypto_label = self._report_titles(result)
        short_summary = self._short_summary(result)
        technical = self._technical_txt(result, title, subtitle)
        stamp = datetime.now(ZoneInfo(self.cfg.bot_timezone)).strftime("%Y%m%d_%H%M%S")
        txt_name = safe_report_filename(result.mode, stamp)

        image_bytes: bytes | None = None
        try:
            image_bytes = await asyncio.to_thread(
                render_table_png,
                result.states,
                title=title,
                subtitle=subtitle,
                crypto_label=crypto_label,
                version=self.cfg.bot_version,
            )
        except Exception:
            # Rendering must never destroy a valid Search. Text fallback remains available.
            log.exception("PNG table rendering failed; using Telegram text fallback")

        fallback_title = f"{title}\n{subtitle}"
        fallback_msgs = telegram_table_messages(result.states, fallback_title, crypto_label=crypto_label)
        complete_chats = 0
        transient_failure = False
        for cid in targets:
            try:
                if image_bytes is not None:
                    await self._tg_call(
                        self.bot.send_photo,
                        cid,
                        BufferedInputFile(image_bytes, filename=f"{result.mode}_{stamp}.png"),
                    )
                else:
                    for text in fallback_msgs:
                        await self._send_message(cid, text, parse_mode=ParseMode.HTML)

                await self._tg_call(
                    self.bot.send_document,
                    cid,
                    BufferedInputFile(technical.encode("utf-8"), filename=txt_name),
                    caption=short_summary,
                    reply_markup=await self._kbd(),
                )
                complete_chats += 1
            except TelegramForbiddenError:
                log.warning("Removing unreachable report chat %s", cid)
                await self.repo.remove_report_chat(cid)
                self.report_chats.discard(cid)
            except Exception:
                log.exception("Report delivery failed for chat %s", cid)
                transient_failure = True
        return complete_chats > 0 and not transient_failure

    def _format_walk_txt(self, r: WalkForwardResult) -> str:
        def pct(num: int, den: int) -> str:
            return "—" if den == 0 else f"{num / den * 100:.1f}%"

        def fnum(v: float | None) -> str:
            return "—" if v is None else f"{v:+.1f}%"

        def zone_text(zone: tuple[float, float] | None) -> str:
            if not zone:
                return "—"
            return f"{_fmt(zone[0])}–{_fmt(zone[1])}"

        def fibs_text(fibs: dict[str, float]) -> str:
            if not fibs:
                return "—"
            return ", ".join(f"{key}={_fmt(value)}" for key, value in sorted(fibs.items()))

        def targets_text(targets: list[float]) -> str:
            return "—" if not targets else " / ".join(_fmt(value) for value in targets)

        resolved = r.t1_first + r.invalid_first
        lines = [
            f"SENIOR WAVE BOT v{self.cfg.bot_version}",
            "WALK-FORWARD — 10 LIQUID MAJORS — DETAILED DETECTOR DIAGNOSTIC",
            f"Биржа: {EXCHANGE_LABELS.get(r.exchange, r.exchange)}",
            "Монеты: BTC, ETH, SOL, BNB, XRP, DOGE, ADA, LINK, LTC, BCH",
            f"Время анализа: {self._format_duration(r.analysis_seconds)}",
            f"Активов: {r.assets_tested}/{r.assets_requested}; ошибок данных: {r.data_errors}",
            f"Историческое окно: {r.history_days}д; шаг проверки: каждый COMPLETE4H; "
            f"примерно checkpoint/актив: {r.complete4h_checkpoints}; горизонт результата: {r.horizon_days}д",
            "",
            "ИТОГ — ТОЛЬКО УНИКАЛЬНЫЕ СВЕЖИЕ СТРУКТУРЫ",
            f"Fresh signals >= {self.cfg.min_rating:.1f}: {r.signals}",
            f"T1 раньше strict invalidation: {r.t1_first} ({pct(r.t1_first, r.signals)})",
            f"Strict invalidation раньше T1: {r.invalid_first} ({pct(r.invalid_first, r.signals)})",
            f"Resolved-only T1 first: {r.t1_first}/{resolved} ({pct(r.t1_first, resolved)})",
            f"Не разрешились за горизонт: {r.unresolved}",
            f"T1 и invalid в одной 1H свече: {r.ambiguous}",
            f"Исключены как уже прошедшие T1 / EXTENDED при первом qualifying observation: {r.already_extended}",
            f"Повторные наблюдения уже учтённых senior-структур (НЕ считаются новыми сигналами): {r.duplicate_observations}",
            f"Median MFE до resolution/конца горизонта: {fnum(r.median_mfe_pct)}; "
            f"Median MAE до resolution/конца горизонта: {fnum(r.median_mae_pct)}",
            f"Median 30d MFE: {fnum(r.median_mfe_30d_pct)}; Median 30d MAE: {fnum(r.median_mae_30d_pct)}",
            "",
            "ПО РЕЙТИНГУ — FRESH UNIQUE SIGNALS",
        ]
        for label, (signals, t1, invalid) in r.rating_buckets.items():
            bucket_resolved = t1 + invalid
            lines.append(
                f"{label}: signals={signals}; T1 first={t1} ({pct(t1, signals)}); "
                f"invalid first={invalid} ({pct(invalid, signals)}); "
                f"resolved-only T1={pct(t1, bucket_resolved)}"
            )

        lines.extend(["", "ПО ТИПУ ВОЛНЫ — FRESH UNIQUE SIGNALS"])
        for wave, (signals, t1, invalid, rest) in r.wave_buckets.items():
            wave_resolved = t1 + invalid
            lines.append(
                f"{wave}: signals={signals}; T1 first={t1} ({pct(t1, signals)}); "
                f"invalid first={invalid} ({pct(invalid, signals)}); unresolved/ambiguous={rest}; "
                f"resolved-only T1={pct(t1, wave_resolved)}"
            )

        lines.extend(["", "ПО МОНЕТАМ — ПОЛНЫЕ ДАННЫЕ ДЛЯ ПОЛИРОВКИ", "=" * 120])
        for asset in r.assets:
            lines.append(f"\n[{asset.symbol}]")
            if not asset.tested:
                lines.append(f"DATA ERROR: {asset.error or 'unknown'}")
                continue
            lines.extend([
                f"COMPLETE4H checkpoints evaluated: {asset.checkpoints_evaluated}",
                f"Qualifying observations >= {self.cfg.min_rating:.1f}: {asset.qualifying_observations}",
                f"Fresh unique signals: {len(asset.signals)}",
                f"Duplicate observations suppressed: {asset.duplicate_observations}",
                f"Already-T1/EXTENDED structures excluded: {len(asset.excluded)}",
            ])
            if not asset.signals:
                lines.append("Нет свежих уникальных qualifying W2/W3-(2) в диагностическом окне.")

            for idx, sig in enumerate(asset.signals, 1):
                retrace = "—" if sig.retrace_depth is None else f"{sig.retrace_depth * 100:.2f}%"
                growth = "—" if sig.growth_from_low_pct is None else f"{sig.growth_from_low_pct:+.2f}%"
                strict_dist = "—" if sig.strict_distance_pct is None else f"{sig.strict_distance_pct:.2f}%"
                hours = "—" if sig.hours_to_outcome is None else f"{sig.hours_to_outcome:.1f}ч"
                lines.extend([
                    "",
                    f"  SIGNAL {idx}",
                    f"    structure_id={sig.structure_id}",
                    f"    first_seen={sig.first_seen}; last_seen={sig.last_seen}; observations={sig.observations}",
                    f"    wave={sig.wave_type}; state_status={sig.state_status}; rating={sig.rating:.1f}; liquidity_rank={sig.liquidity_rank or '—'}",
                    f"    entry={_fmt(sig.entry)}; working_low={_fmt(sig.working_low)}; strict_origin={_fmt(sig.strict_origin)}",
                    f"    origin={_fmt(sig.origin)}; impulse_high={_fmt(sig.impulse_high)}; "
                    f"parent_w2_low={_fmt(sig.parent_w2_low)}; w3_1_high={_fmt(sig.w3_1_high)}",
                    f"    retrace={retrace}; growth_from_low={growth}; strict_distance={strict_dist}",
                    f"    fib_status={sig.fib_status}; fibs={fibs_text(sig.fibs)}",
                    (
                        "    target_projection="
                        f"{sig.target_source or '—'}; "
                        f"origin={_fmt(sig.target_origin)}; "
                        f"high={_fmt(sig.target_impulse_high)}; "
                        f"length={_fmt(sig.target_impulse_length)}"
                    ),
                    f"    base={zone_text(sig.base_zone)}; deep/on-sweep={zone_text(sig.deep_zone)}",
                    f"    targets={targets_text(sig.targets)}; T1={_fmt(sig.t1)}",
                    f"    last_complete4h_bucket={sig.last_complete4h_bucket or '—'}",
                    f"    outcome={sig.outcome}; outcome_at={sig.outcome_at or '—'}; time_to_outcome={hours}",
                    f"    MFE_to_resolution={fnum(sig.mfe_to_resolution_pct)}; MAE_to_resolution={fnum(sig.mae_to_resolution_pct)}",
                    f"    MFE_30d={fnum(sig.mfe_30d_pct)}; MAE_30d={fnum(sig.mae_30d_pct)}",
                ])

            if asset.excluded:
                lines.append("")
                lines.append("  ИСКЛЮЧЕННЫЕ КАК НЕСВЕЖИЕ / УЖЕ ПРОШЕДШИЕ T1:")
                for idx, ex in enumerate(asset.excluded, 1):
                    lines.append(
                        f"    {idx}. first_seen={ex.first_seen}; structure_id={ex.structure_id}; "
                        f"wave={ex.wave_type}; rating={ex.rating:.1f}; entry={_fmt(ex.entry)}; "
                        f"T1={_fmt(ex.t1)}; reason={ex.reason}"
                    )

        lines.extend([
            "",
            "МЕТОДИКА WALK",
            "1) Проверяются только фиксированные 10 liquid majors; текущий Top-100/200/300 на состав walk не влияет.",
            "2) История идёт последовательно по каждому завершённому UTC COMPLETE4H, а не по шести редким месячным snapshots.",
            "3) Одна и та же живущая senior-структура считается сигналом только один раз — при первом свежем qualifying observation.",
            "4) Если при первом qualifying observation цена уже >= T1 или state=EXTENDED, это не считается успешным fresh signal и попадает в excluded.",
            "5) Detector на checkpoint видит только свечи, закрытые к этому моменту. Будущие 1H свечи используются только для outcome/MFE/MAE.",
            "6) MFE/MAE_to_resolution считаются только до первого T1/strict invalidation (или до конца горизонта, если unresolved).",
            "7) MFE/MAE_30d отдельно показывают всё последующее движение за полный горизонт и не смешиваются с риском пути до исхода.",
            "8) /walk не меняет Search, Сопровождение, tracked-set или их таймер.",
        ])
        return "\n".join(lines).rstrip() + "\n"

    async def _ping(self, message: Message):
        t0 = time.perf_counter()
        if self.bot:
            await self._tg_call(self.bot.get_me)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        uptime = int(time.monotonic() - self.started)
        days, rem = divmod(uptime, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _ = divmod(rem, 60)
        mem_mb = psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
        await self._answer(
            message,
            f"🟢 Pong\n\n"
            f"Отклик: {elapsed_ms:.1f} ms\n"
            f"Работает: {days}д {hours}ч {minutes}м\n"
            f"Память: {mem_mb:.0f} MB\n"
            f"Версия: {self.cfg.bot_version}",
            reply_markup=await self._kbd(),
        )

    def attach(self, dp: Dispatcher, bot: Bot):
        self.bot = bot
        dp.include_router(self.router)
