from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent

import httpx
import pandas as pd
import pytest

from config import Settings
from core_models import MarketSnapshot
from core_senior import SeniorWaveDetector
from core_symbols import excluded_from_crypto_top
from data_exchanges import BinanceSpotClient, MexcFuturesClient
from conftest import frame, state


@pytest.mark.parametrize(
    "base,excluded",
    [
        ("JUP", False),
        ("JUPITER", False),
        ("BTCUP", True),
        ("ETHDOWN", True),
        ("USDC", True),
        ("AEUR", True),
        ("XUSD", True),
        ("XAU", True),
        ("USOIL", True),
        ("XAUT", True),
        ("UKOIL", True),
    ],
)
def test_crypto_top_filter_is_exact_not_suffix_based(base, excluded):
    assert excluded_from_crypto_top(base) is excluded


@pytest.mark.asyncio
async def test_binance_top_keeps_jup_and_excludes_new_stables_and_real_leveraged_tokens():
    info = {
        "symbols": [
            {
                "symbol": symbol,
                "status": "TRADING",
                "quoteAsset": "USDT",
                "baseAsset": base,
                "isSpotTradingAllowed": True,
            }
            for base, symbol in [
                ("JUP", "JUPUSDT"),
                ("SOL", "SOLUSDT"),
                ("AEUR", "AEURUSDT"),
                ("XUSD", "XUSDUSDT"),
                ("BTCUP", "BTCUPUSDT"),
            ]
        ]
    }
    tickers = [
        {"symbol": "JUPUSDT", "quoteVolume": "500"},
        {"symbol": "SOLUSDT", "quoteVolume": "400"},
        {"symbol": "AEURUSDT", "quoteVolume": "999"},
        {"symbol": "XUSDUSDT", "quoteVolume": "998"},
        {"symbol": "BTCUPUSDT", "quoteVolume": "997"},
    ]

    def handler(request: httpx.Request):
        payload = info if request.url.path.endswith("exchangeInfo") else tickers
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        rows = await BinanceSpotClient(http, retry_base_delay=0).top_symbols(10)
    assert rows == [("JUPUSDT", 500.0), ("SOLUSDT", 400.0)]


@pytest.mark.asyncio
async def test_mexc_top_keeps_jup_and_excludes_new_stables_and_real_leveraged_tokens():
    payload = {
        "success": True,
        "code": 0,
        "data": [
            {"symbol": "JUP_USDT", "amount24": "500"},
            {"symbol": "SOL_USDT", "amount24": "400"},
            {"symbol": "AEUR_USDT", "amount24": "999"},
            {"symbol": "XUSD_USDT", "amount24": "998"},
            {"symbol": "ETHDOWN_USDT", "amount24": "997"},
        ],
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as http:
        rows = await MexcFuturesClient(http, retry_base_delay=0).top_symbols(10)
    assert rows == [("JUP_USDT", 500.0), ("SOL_USDT", 400.0)]


def _dirty_state(status: str):
    previous = state("DOGEUSDT")
    previous.status = status
    previous.retrace_depth = 0.82
    previous.fibs = {"0.500": 0.1}
    previous.fib_status = "> .500 · stale"
    previous.targets = [1.0, 2.0]
    previous.base_zone = (0.8, 0.9)
    previous.deep_zone = (0.6, 0.7)
    previous.growth_from_low_pct = 3.5
    previous.strict_distance_pct = 5.0
    previous.rating = 9.9
    return previous


@pytest.mark.parametrize(
    "status,expected_fib_status",
    [("RECOUNT", "RECOUNT REQUIRED"), ("INVALID", "STRICT ORIGIN BROKEN")],
)
def test_terminal_state_drops_all_obsolete_derived_values(status, expected_fib_status):
    previous = _dirty_state(status)
    snap = MarketSnapshot(
        previous.symbol,
        previous.exchange,
        1.0,
        0.95,
        0.9,
        frame(periods=8),
        pd.DataFrame(),
    )
    result = SeniorWaveDetector().track(previous, snap, 100)
    assert result.retrace_depth is None
    assert result.fibs == {}
    assert result.targets == []
    assert result.base_zone is None
    assert result.deep_zone is None
    assert result.growth_from_low_pct is None
    assert result.strict_distance_pct is None
    assert result.rating == 0.0
    assert result.fib_status == expected_fib_status


def test_runtime_dependencies_are_exactly_pinned_to_audited_versions():
    root = ROOT
    expected = {
        "aiogram==3.31.0",
        "httpx==0.28.1",
        "pandas==2.3.3",
        "numpy==2.5.3",
        "aiosqlite==0.22.1",
        "pydantic-settings==2.15.0",
        "psutil==7.2.2",
        "Pillow==12.3.0",
    }
    lines = {line.strip() for line in (root / "requirements.txt").read_text().splitlines() if line.strip()}
    assert lines == expected
    assert all(">=" not in line and "<" not in line for line in lines)
    dockerfile = (root / "Dockerfile").read_text()
    assert "requirements.lock.txt" in dockerfile
    assert "pip install --no-cache-dir -r requirements.lock.txt" in dockerfile


def test_v0018_is_default_and_documented():
    cfg = Settings(BOT_TOKEN="test", _env_file=None)
    assert cfg.bot_version == "0019"
    root = ROOT
    for path in [root / "README.md", root / ".env.example", root / "Dockerfile"]:
        assert "0019" in path.read_text()
