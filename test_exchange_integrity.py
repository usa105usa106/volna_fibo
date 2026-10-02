import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pandas as pd
import pytest

from data_collector import MarketDataService
from data_exchanges import BinanceSpotClient, ExchangeAPIError, MexcFuturesClient
from data_http import request_with_retry
from data_integrity import DataIntegrityError, IntegrityPolicy, validate_candles
from conftest import frame

UTC = timezone.utc
END = datetime(2026, 1, 2, tzinfo=UTC)


def binance_rows():
    return [
        [
            int(ts.timestamp() * 1000),
            "10",
            "11",
            "9",
            "10.5",
            "1",
            int(ts.timestamp() * 1000) + 3599999,
            "1",
            1,
            "1",
            "1",
            "0",
        ]
        for ts in pd.date_range("2026-01-01", periods=4, freq="h", tz="UTC")
    ]


def mexc_payload(rows=None):
    data = frame(periods=4) if rows is None else rows
    return {
        "success": True,
        "code": 0,
        "data": {
            "time": [int(t.timestamp()) for t in data.timestamp],
            "open": data.open.tolist(),
            "high": data.high.tolist(),
            "low": data.low.tolist(),
            "close": data.close.tolist(),
            "vol": data.volume.tolist(),
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("exchange", ["binance", "mexc"])
@pytest.mark.parametrize("kind", ["duplicate", "gap", "nan_live", "negative_volume"])
async def test_exchange_adapter_rejects_corrupt_raw_candles(exchange, kind):
    if exchange == "binance":
        rows = binance_rows()
        if kind == "duplicate":
            rows.insert(1, rows[0].copy())
        if kind == "gap":
            rows.pop(1)
        if kind == "nan_live":
            rows[-1][4] = "nan"
        if kind == "negative_volume":
            rows[-1][5] = "-1"
        payload = rows
        cls = BinanceSpotClient
    else:
        rows = frame(periods=4)
        if kind == "duplicate":
            rows = pd.concat([rows, rows.iloc[[0]]])
        if kind == "gap":
            rows = rows.drop(index=1)
        if kind == "nan_live":
            rows["close"] = rows.close.astype(object)
            rows.loc[3, "close"] = "nan"
        if kind == "negative_volume":
            rows.loc[3, "volume"] = -1
        payload = mexc_payload(rows)
        cls = MexcFuturesClient
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as http:
        with pytest.raises(DataIntegrityError):
            await cls(http, retry_attempts=1).candles(
                "DOGEUSDT" if exchange == "binance" else "DOGE_USDT",
                "1h",
                END - timedelta(days=1),
                END,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cls,payload",
    [(BinanceSpotClient, binance_rows()), (MexcFuturesClient, mexc_payload())],
)
async def test_closed_candle_uses_request_cutoff_not_later_wallclock(cls, payload):
    end = datetime(2026, 1, 1, 3, 30, tzinfo=UTC)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as http:
        closed, price, low = await cls(http).candles(
            "DOGEUSDT", "1h", end - timedelta(hours=4), end
        )
    assert len(closed) == 3
    assert closed.timestamp.max() == pd.Timestamp("2026-01-01T02:00:00Z")
    assert (price, low) == (10.5, 9.0)


@pytest.mark.parametrize(
    "bad", ["future", "open", "nan_volume", "negative_volume", "subnanosecond"]
)
def test_integrity_rejects_future_open_and_invalid_volume(bad):
    candles = frame(periods=4)
    now = datetime(2026, 1, 1, 4, tzinfo=UTC)
    if bad == "future":
        candles["timestamp"] += pd.Timedelta(days=1)
    if bad == "open":
        now = datetime(2026, 1, 1, 3, 30, tzinfo=UTC)
    if bad == "nan_volume":
        candles.loc[1, "volume"] = float("nan")
    if bad == "negative_volume":
        candles.loc[1, "volume"] = -1
    if bad == "subnanosecond":
        candles["timestamp"] += pd.Timedelta(nanoseconds=1)
    with pytest.raises(DataIntegrityError):
        validate_candles(candles, IntegrityPolicy("1h"), now)


@pytest.mark.asyncio
async def test_mexc_http200_rate_limit_retries_then_succeeds():
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        payload = (
            {"success": False, "code": 510}
            if count < 3
            else {
                "success": True,
                "code": 0,
                "data": [{"symbol": "DOGE_USDT", "amount24": "12"}],
            }
        )
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await MexcFuturesClient(http, retry_base_delay=0).top_symbols(100)
    assert result == [("DOGE_USDT", 12.0)] and count == 3


@pytest.mark.asyncio
async def test_mexc_permanent_error_is_not_empty_success_or_retried():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"success": False, "code": 600})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ExchangeAPIError, match="600"):
            await MexcFuturesClient(http, retry_base_delay=0).top_symbols(100)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_mexc_pagination_has_no_holes_duplicates_or_oversized_pages(monkeypatch):
    calls = []

    def handler(request):
        start = int(request.url.params["start"])
        end = int(request.url.params["end"])
        calls.append((start, end))
        times = range(((start + 3599) // 3600) * 3600, end + 1, 3600)
        times = list(times)
        assert len(times) <= 2000
        payload = {
            "success": True,
            "code": 0,
            "data": {
                "time": times,
                **{
                    k: [v] * len(times)
                    for k, v in [
                        ("open", 10),
                        ("high", 11),
                        ("low", 9),
                        ("close", 10),
                        ("vol", 1),
                    ]
                },
            },
        }
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = MexcFuturesClient(http)
        monkeypatch.setattr(client, "_throttle_kline", AsyncMock())
        end = datetime(2026, 6, 1, tzinfo=UTC)
        result, _, _ = await client.candles(
            "DOGE_USDT", "1h", end - timedelta(days=120), end
        )
    assert len(result) == 120 * 24
    assert not result.timestamp.duplicated().any()
    assert all(right[0] == left[1] + 1 for left, right in zip(calls, calls[1:]))


@pytest.mark.asyncio
async def test_http_retry_after_seconds_and_date_are_not_capped(monkeypatch):
    sleeps = AsyncMock()
    monkeypatch.setattr("data_http.asyncio.sleep", sleeps)
    from email.utils import format_datetime

    for header in [
        "120",
        format_datetime(datetime.now(UTC) + timedelta(seconds=120), usegmt=True),
    ]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(429, headers={"Retry-After": header})
            )
        ) as http:
            await request_with_retry(
                http, "GET", "https://example.invalid", attempts=2, max_delay=8
            )
        assert sleeps.await_args.args[0] >= 118


@pytest.mark.asyncio
async def test_failed_snapshot_cancels_and_drains_sibling_request(cfg):
    service = MarketDataService(cfg)
    started, stopped = asyncio.Event(), asyncio.Event()

    async def candles(symbol, timeframe, *args):
        if timeframe == "1h":
            await started.wait()
            raise DataIntegrityError("broken hourly")
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    service.clients["binance_spot"] = SimpleNamespace(candles=candles)
    try:
        with pytest.raises(DataIntegrityError):
            await service.snapshot("binance_spot", "DOGEUSDT")
        assert stopped.is_set()
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("live", [float("nan"), float("inf"), 0, -1])
async def test_snapshot_rejects_bad_live_values(cfg, live):
    service = MarketDataService(cfg)

    async def candles(symbol, timeframe, start, end):
        end = pd.Timestamp(end)
        data = frame(
            start=(end.floor("h") - pd.Timedelta(hours=3))
            if timeframe == "1h"
            else (end.floor("D") - pd.Timedelta(days=3)),
            periods=2,
            freq="h" if timeframe == "1h" else "D",
        )
        return data, live, 9.0

    service.clients["binance_spot"] = SimpleNamespace(candles=candles)
    try:
        with pytest.raises(DataIntegrityError):
            await service.snapshot("binance_spot", "DOGEUSDT")
    finally:
        await service.close()
