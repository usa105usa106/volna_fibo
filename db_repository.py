from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from core_models import AppSettings, WaveState


class Repository:
    def __init__(self, path: Path, defaults: AppSettings):
        self.path = path
        self.defaults = defaults

    @staticmethod
    def _settings_rows(settings: AppSettings) -> list[tuple[str, str]]:
        return [(key, str(value) if value is not None else "") for key, value in asdict(settings).items()]

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS search_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    top_n INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS tracked_setups (
                    session_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    is_control INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (session_id, symbol),
                    FOREIGN KEY(session_id) REFERENCES search_sessions(id)
                );
                CREATE TABLE IF NOT EXISTS report_chats (chat_id INTEGER PRIMARY KEY);
                """
            )
            await db.executemany("INSERT OR IGNORE INTO kv(key,value) VALUES(?,?)", self._settings_rows(self.defaults))
            await db.commit()

    async def _set(self, key: str, value: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            await db.commit()

    async def get_settings(self) -> AppSettings:
        # One SELECT gives a coherent SQLite snapshot, including the report anchor.
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT key,value FROM kv") as cur:
                values = dict(await cur.fetchall())
        return AppSettings(
            top_n=int(values.get("top_n", self.defaults.top_n)),
            interval_minutes=int(values.get("interval_minutes", self.defaults.interval_minutes)),
            exchange=values.get("exchange", self.defaults.exchange),
            mode=values.get("mode", self.defaults.mode),
            last_run_search=values.get("last_run_search") or None,
            last_run_track=values.get("last_run_track") or None,
        )

    async def save_settings(self, settings: AppSettings) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.executemany(
                "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                self._settings_rows(settings),
            )
            await db.commit()

    async def set_top_n(self, n: int):
        await self._set("top_n", str(n))

    async def set_interval(self, minutes: int):
        await self._set("interval_minutes", str(minutes))

    async def set_exchange(self, exchange: str):
        await self._set("exchange", exchange)

    async def set_mode(self, mode: str):
        await self._set("mode", mode)

    async def mark_report_complete(self, mode: str, completed_at: str | None = None):
        if mode not in {"search", "track"}:
            raise ValueError("unsupported report mode")
        await self._set(f"last_run_{mode}", completed_at or datetime.now(timezone.utc).isoformat())

    async def report_chats(self) -> set[int]:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT chat_id FROM report_chats") as cur:
                return {int(row[0]) for row in await cur.fetchall()}

    async def add_report_chat(self, chat_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT OR IGNORE INTO report_chats(chat_id) VALUES(?)", (chat_id,))
            await db.commit()

    async def remove_report_chat(self, chat_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM report_chats WHERE chat_id=?", (chat_id,))
            await db.commit()

    async def reset_to_defaults(self, chat_id: int | None = None) -> AppSettings:
        defaults = AppSettings(
            top_n=self.defaults.top_n,
            interval_minutes=self.defaults.interval_minutes,
            exchange=self.defaults.exchange,
            mode="idle",
        )
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute("DELETE FROM tracked_setups")
                await db.execute("DELETE FROM search_sessions")
                await db.execute("DELETE FROM kv")
                await db.execute("DELETE FROM report_chats")
                # Keep monotonically increasing session IDs across Reset.
                await db.executemany("INSERT INTO kv(key,value) VALUES(?,?)", self._settings_rows(defaults))
                if chat_id is not None:
                    await db.execute("INSERT INTO report_chats(chat_id) VALUES(?)", (chat_id,))
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return defaults

    async def clear_active_session(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE search_sessions SET active=0 WHERE active=1")
            await db.commit()

    async def replace_active_session(
        self, exchange: str, top_n: int, states: list[WaveState], *, completed_at: str | None = None,
    ) -> int:
        """Commit the delivered Search and its countdown in the same transaction."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute("UPDATE search_sessions SET active=0 WHERE active=1")
                cur = await db.execute(
                    "INSERT INTO search_sessions(created_at,exchange,top_n,active) VALUES(?,?,?,1)",
                    (datetime.now(timezone.utc).isoformat(), exchange, top_n),
                )
                session_id = int(cur.lastrowid)
                await cur.close()
                for state in states:
                    await db.execute(
                        "INSERT INTO tracked_setups(session_id,symbol,state_json,is_control) VALUES(?,?,?,?)",
                        (session_id, state.symbol, json.dumps(state.to_dict(), ensure_ascii=False, allow_nan=False), int(state.is_control)),
                    )
                if completed_at is not None:
                    await db.execute(
                        "INSERT INTO kv(key,value) VALUES('last_run_search',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (completed_at,),
                    )
                # There is no history UI or consumer: retain only the current session.
                await db.execute("DELETE FROM tracked_setups WHERE session_id<>?", (session_id,))
                await db.execute("DELETE FROM search_sessions WHERE id<>?", (session_id,))
                await db.commit()
                return session_id
            except BaseException:
                await db.rollback()
                raise

    async def active_session(self) -> tuple[int, str, int] | None:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT id,exchange,top_n FROM search_sessions WHERE active=1 ORDER BY id DESC LIMIT 1") as cur:
                row = await cur.fetchone()
        return (int(row[0]), str(row[1]), int(row[2])) if row else None

    async def tracked_states(self, session_id: int | None = None) -> list[WaveState]:
        async with aiosqlite.connect(self.path) as db:
            if session_id is None:
                query = "SELECT state_json FROM tracked_setups WHERE session_id=(SELECT id FROM search_sessions WHERE active=1 ORDER BY id DESC LIMIT 1) ORDER BY is_control,symbol"
                params = ()
            else:
                query = "SELECT state_json FROM tracked_setups WHERE session_id=? ORDER BY is_control,symbol"
                params = (session_id,)
            async with db.execute(query, params) as cur:
                rows = await cur.fetchall()
        return [WaveState.from_dict(json.loads(row[0])) for row in rows]

    async def update_states(self, session_id: int, states: list[WaveState]) -> None:
        async with aiosqlite.connect(self.path) as db:
            for state in states:
                await db.execute(
                    "UPDATE tracked_setups SET state_json=?, is_control=? WHERE session_id=? AND symbol=?",
                    (json.dumps(state.to_dict(), ensure_ascii=False, allow_nan=False), int(state.is_control), session_id, state.symbol),
                )
            await db.commit()
