import asyncio
from dataclasses import asdict
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from services_runtime import cleanup_runtime_files
from conftest import state


@pytest.mark.asyncio
async def test_concurrent_setters_preserve_each_other_and_anchor(repo, monkeypatch):
    original_get = repo.get_settings
    barrier = asyncio.Event()
    reads = 0

    async def synchronized_get():
        nonlocal reads
        result = await original_get()
        reads += 1
        if reads == 3:
            barrier.set()
        await barrier.wait()
        return result

    monkeypatch.setattr(repo, "get_settings", synchronized_get)
    await asyncio.wait_for(
        asyncio.gather(
            repo.set_top_n(300),
            repo.set_interval(240),
            repo.mark_report_complete("search"),
        ),
        5,
    )
    monkeypatch.setattr(repo, "get_settings", original_get)
    result = await repo.get_settings()
    assert (result.top_n, result.interval_minutes) == (300, 240)
    assert result.last_run_search is not None


@pytest.mark.asyncio
async def test_save_settings_rolls_back_all_fields_on_failure(repo):
    before = await repo.get_settings()
    with sqlite3.connect(repo.path) as db:
        db.execute(
            "CREATE TRIGGER fail_interval BEFORE UPDATE ON kv WHEN NEW.key='interval_minutes' BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    changed = await repo.get_settings()
    changed.top_n, changed.interval_minutes = 300, 240
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        await repo.save_settings(changed)
    assert asdict(await repo.get_settings()) == asdict(before)


@pytest.mark.asyncio
async def test_search_transaction_rollback_preserves_old_set_and_timer(repo):
    old_id = await repo.replace_active_session("binance_spot", 100, [state()])
    await repo.mark_report_complete("search", "2026-01-01T00:00:00+00:00")
    with pytest.raises(sqlite3.IntegrityError):
        await repo.replace_active_session(
            "mexc_futures",
            300,
            [state("NEW"), state("NEW")],
            completed_at="2026-02-01T00:00:00+00:00",
        )
    assert (await repo.active_session())[0] == old_id
    assert [s.symbol for s in await repo.tracked_states()] == ["OLDUSDT"]
    assert (await repo.get_settings()).last_run_search == "2026-01-01T00:00:00+00:00"


@pytest.mark.asyncio
async def test_anchor_failure_rolls_back_new_session(repo):
    old_id = await repo.replace_active_session("binance_spot", 100, [state()])
    with sqlite3.connect(repo.path) as db:
        db.execute(
            "CREATE TRIGGER fail_anchor BEFORE UPDATE ON kv WHEN NEW.key='last_run_search' BEGIN SELECT RAISE(ABORT,'anchor'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="anchor"):
        await repo.replace_active_session(
            "binance_spot",
            100,
            [state("NEW")],
            completed_at="2026-02-01T00:00:00+00:00",
        )
    assert (await repo.active_session())[0] == old_id
    assert [s.symbol for s in await repo.tracked_states()] == ["OLDUSDT"]


@pytest.mark.asyncio
async def test_repeated_searches_bound_storage_and_reset_is_atomic(repo):
    for i in range(5):
        await repo.replace_active_session(
            "binance_spot",
            100,
            [state(str(i))],
            completed_at=f"2026-01-0{i + 1}T00:00:00+00:00",
        )
    with sqlite3.connect(repo.path) as db:
        assert db.execute("SELECT count(*) FROM search_sessions").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM tracked_setups").fetchone()[0] == 1
    await repo.add_report_chat(1)
    await repo.reset_to_defaults(2)
    assert await repo.active_session() is None
    assert await repo.tracked_states() == []
    assert await repo.report_chats() == {2}
    settings = await repo.get_settings()
    assert settings.top_n == 100 and settings.mode == "idle"
    assert settings.last_run_search is None and settings.last_run_track is None


def test_cleanup_preserves_committed_wal_after_process_crash(tmp_path):
    path = tmp_path / "state.sqlite3"
    subprocess.run(
        [
            sys.executable,
            "-c",
            """import sqlite3,os,sys
c=sqlite3.connect(sys.argv[1]);c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA wal_autocheckpoint=0')
c.execute('CREATE TABLE evidence(value TEXT)');c.execute("INSERT INTO evidence VALUES ('committed')");c.commit();os._exit(0)
""",
            str(path),
        ],
        check=True,
    )
    assert Path(str(path) + "-wal").exists()
    cleanup_runtime_files(tmp_path, path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM evidence").fetchone() == ("committed",)
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_cleanup_preserves_hot_rollback_journal(tmp_path):
    path = tmp_path / "state.sqlite3"
    subprocess.run(
        [
            sys.executable,
            "-c",
            """import sqlite3,os,sys
c=sqlite3.connect(sys.argv[1]);c.execute('PRAGMA cache_size=5');c.execute('CREATE TABLE evidence(value TEXT)')
c.executemany('INSERT INTO evidence VALUES(?)',[('old'*1000,)]*200);c.commit()
c.execute("UPDATE evidence SET value='new'");os._exit(0)
""",
            str(path),
        ],
        check=True,
    )
    assert Path(str(path) + "-journal").exists()
    cleanup_runtime_files(tmp_path, path)
    with sqlite3.connect(path) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM evidence WHERE value LIKE 'old%'"
            ).fetchone()[0]
            == 200
        )
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
