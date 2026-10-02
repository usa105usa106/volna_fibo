from __future__ import annotations

import gc
import shutil
from pathlib import Path


_TEMP_DIR_NAMES = {"tmp", "temp", "cache", "parquet", "downloads", "reports"}
_TEMP_SUFFIXES = {".tmp", ".part", ".download", ".cache"}


def cleanup_runtime_files(data_dir: Path, db_path: Path) -> list[Path]:
    """Delete bot-owned temporary artifacts without touching persistent SQLite state.

    The current build does not persist candles, but this also removes known leftovers
    from older builds and interrupted downloads/reports.
    """
    removed: list[Path] = []
    data_dir.mkdir(parents=True, exist_ok=True)
    db_resolved = db_path.resolve()

    for child in list(data_dir.iterdir()):
        try:
            resolved = child.resolve()
        except FileNotFoundError:
            continue
        if resolved == db_resolved:
            continue

        if child.is_dir() and child.name.lower() in _TEMP_DIR_NAMES:
            shutil.rmtree(child, ignore_errors=True)
            removed.append(child)
            continue

        lower = child.name.lower()
        if child.is_file() and (
            child.suffix.lower() in _TEMP_SUFFIXES
            or lower.startswith("tmp_")
            or lower.startswith("temp_")
        ):
            child.unlink(missing_ok=True)
            removed.append(child)

    # SQLite owns its WAL, SHM and rollback journal. Even with no live connections,
    # these files may contain committed data or be required for crash recovery.

    gc.collect()
    return removed
