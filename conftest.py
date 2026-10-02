from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd
import pytest
import pytest_asyncio

from config import Settings
from core_models import AppSettings, WaveState
from db_repository import Repository


def frame(start="2026-01-01", periods=8, freq="h"):
    timestamps = pd.date_range(start, periods=periods, freq=freq, tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.5,
            "volume": 1.0,
        }
    )


def state(symbol="OLDUSDT"):
    return WaveState(
        symbol,
        "binance_spot",
        "W2",
        "DEEP",
        5.0,
        20.0,
        9.0,
        5.0,
        rating=8.5,
        current_price=10.5,
        targets=[24.0],
        last_complete4h_bucket="2026-01-01T00:00:00+00:00",
    )


def message(chat_id=1, text=""):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id), text=text, answer=AsyncMock()
    )


@pytest.fixture
def cfg(tmp_path):
    return Settings(
        BOT_TOKEN="123456:FAKE_TOKEN",
        DATA_DIR=tmp_path,
        HTTP_CONCURRENCY=2,
        TELEGRAM_RETRY_ATTEMPTS=2,
        TELEGRAM_RETRY_BASE_DELAY_SECONDS=0,
        API_RETRY_BASE_DELAY_SECONDS=0,
    )


@pytest_asyncio.fixture
async def repo(tmp_path):
    repository = Repository(
        tmp_path / "senior_wave_bot.sqlite3", AppSettings(top_n=100)
    )
    await repository.init()
    return repository
