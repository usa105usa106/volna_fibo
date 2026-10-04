from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from datetime import datetime, timedelta
import logging
import math
import time
from typing import Any

import httpx
import pandas as pd

from core_symbols import CONTROL_BASES, excluded_from_crypto_top
from data_http import request_with_retry
from data_integrity import DataIntegrityError, IntegrityPolicy, validate_candles
from data_lineage import restrict_current_history
from services_tasks import gather_owned


log = logging.getLogger(__name__)


class ControlUnavailable(LookupError):
    pass


class ExchangeAPIError(RuntimeError):
    pass


def _candle_result(raw: pd.DataFrame, timeframe: str, end: datetime, *, is_control: bool):
    # Validate the raw response before filtering: duplicates and malformed live
    # candles must not be hidden by normalization or the closed-candle selection.
    cutoff = min(pd.Timestamp(end), pd.Timestamp.now(tz="UTC"))
    raw = validate_candles(raw, IntegrityPolicy(timeframe, is_control, False, False), cutoff)
    step = pd.Timedelta(hours=1) if timeframe == "1h" else pd.Timedelta(days=1)
    closed = raw[(raw["timestamp"] + step) <= cutoff].copy()
    return closed, float(raw["close"].iloc[-1]), float(raw["low"].iloc[-1])


class ExchangeClient(ABC):
    name: str

    def __init__(self, http: httpx.AsyncClient, retry_attempts: int = 5, retry_base_delay: float = 0.5):
        self.http = http
        self.retry_attempts = retry_attempts
        self.retry_base_delay = retry_base_delay

    async def _get(self, url: str, **kwargs) -> httpx.Response:
        return await request_with_retry(
            self.http,
            "GET",
            url,
            attempts=self.retry_attempts,
            base_delay=self.retry_base_delay,
            **kwargs,
        )

    @abstractmethod
    async def top_symbols(self, n: int) -> list[tuple[str, float]]:
        raise NotImplementedError

    @abstractmethod
    async def candles(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> tuple[pd.DataFrame, float | None, float | None]:
        """Return closed candles, live price, current/incomplete candle low."""
        raise NotImplementedError

    @abstractmethod
    async def control_symbols(self) -> dict[str, str]:
        """Map canonical XAU/USOIL names to instruments on this exact selected exchange."""
        raise NotImplementedError

    async def control_candles(self, canonical: str, timeframe: str, start: datetime, end: datetime):
        mapping = await self.control_symbols()
        remote = mapping.get(canonical)
        if remote is None:
            raise ControlUnavailable(f"{canonical} unavailable on {self.name}")
        return await self.candles(remote, timeframe, start, end)


class BinanceSpotClient(ExchangeClient):
    name = "binance_spot"
    base_url = "https://api.binance.com"

    async def _exchange_info(self) -> dict[str, Any]:
        r = await self._get(f"{self.base_url}/api/v3/exchangeInfo")
        r.raise_for_status()
        return r.json()

    async def top_symbols(self, n: int) -> list[tuple[str, float]]:
        info_r, tick_r = await gather_owned(
            self._get(f"{self.base_url}/api/v3/exchangeInfo"),
            self._get(f"{self.base_url}/api/v3/ticker/24hr"),
        )
        info_r.raise_for_status()
        tick_r.raise_for_status()
        info = info_r.json()
        tickers = {x["symbol"]: x for x in tick_r.json()}
        rows: list[tuple[str, float]] = []
        for s in info.get("symbols", []):
            if s.get("status") != "TRADING" or s.get("quoteAsset") != "USDT":
                continue
            if not s.get("isSpotTradingAllowed", True):
                continue
            base = str(s.get("baseAsset", ""))
            if excluded_from_crypto_top(base):
                continue
            t = tickers.get(s["symbol"])
            if not t:
                continue
            try:
                qv = float(t.get("quoteVolume", 0.0))
            except (TypeError, ValueError):
                qv = 0.0
            if math.isfinite(qv) and qv > 0:
                rows.append((s["symbol"], qv))
        rows.sort(key=lambda x: x[1], reverse=True)
        return rows[:n]

    async def control_symbols(self) -> dict[str, str]:
        # Deliberately exact: XAUT or other tokenized-gold assets are NOT treated as XAU.
        info = await self._exchange_info()
        found: dict[str, str] = {}
        for s in info.get("symbols", []):
            if s.get("status") != "TRADING" or s.get("quoteAsset") != "USDT":
                continue
            if not s.get("isSpotTradingAllowed", True):
                continue
            base = str(s.get("baseAsset", ""))
            if base in CONTROL_BASES:
                found[base] = str(s.get("symbol"))
        return found

    async def candles(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> tuple[pd.DataFrame, float | None, float | None]:
        if timeframe not in {"1h", "1d"}:
            raise ValueError(f"unsupported timeframe: {timeframe}")
        interval = "1h" if timeframe == "1h" else "1d"
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        out: list[list[Any]] = []
        cursor = start_ms
        while cursor < end_ms:
            r = await self._get(
                f"{self.base_url}/api/v3/klines",
                params={"symbol": symbol, "interval": interval, "startTime": cursor, "endTime": end_ms, "limit": 1000},
            )
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            out.extend(batch)
            last_open = int(batch[-1][0])
            step = 3_600_000 if interval == "1h" else 86_400_000
            next_cursor = last_open + step
            if next_cursor <= cursor:
                raise RuntimeError(f"Binance candle cursor stalled for {symbol} {interval}")
            cursor = next_cursor
            if len(batch) < 1000:
                break
        if not out:
            return pd.DataFrame(), None, None
        raw = pd.DataFrame(out, columns=[
            "open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume",
            "trades", "taker_base", "taker_quote", "ignore",
        ])
        step = 3_600_000 if interval == "1h" else 86_400_000
        if (pd.to_numeric(raw["close_time"], errors="coerce") != pd.to_numeric(raw["open_time"], errors="coerce") + step - 1).any():
            raise DataIntegrityError("Binance: invalid candle close_time")
        raw["timestamp"] = pd.to_datetime(raw["open_time"], unit="ms", utc=True)
        raw = restrict_current_history(raw, self.name, symbol, timeframe)
        if raw.empty:
            return pd.DataFrame(), None, None
        return _candle_result(raw[["timestamp", "open", "high", "low", "close", "volume"]], timeframe, end, is_control=symbol in {"XAUUSDT", "USOILUSDT"})


class MexcFuturesClient(ExchangeClient):
    name = "mexc_futures"
    base_url = "https://api.mexc.com"

    def __init__(self, http: httpx.AsyncClient, retry_attempts: int = 5, retry_base_delay: float = 0.5):
        super().__init__(http, retry_attempts, retry_base_delay)
        self._kline_lock = asyncio.Lock()
        self._last_kline_request = 0.0

    @staticmethod
    def _retry_payload(response: httpx.Response) -> bool:
        if int(getattr(response, "status_code", 200)) != 200:
            return False
        try:
            payload = response.json()
        except ValueError:
            return False
        return isinstance(payload, dict) and str(payload.get("code")) in {"500", "501", "510", "604", "801"}

    @staticmethod
    def _data(response: httpx.Response):
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("success") is not True or payload.get("code") != 0:
            code = payload.get("code") if isinstance(payload, dict) else "malformed"
            raise ExchangeAPIError(f"MEXC response error: {code}")
        if "data" not in payload:
            raise ExchangeAPIError("MEXC response has no data")
        return payload["data"]

    async def _get(self, url: str, **kwargs) -> httpx.Response:
        return await super()._get(url, retry_response=self._retry_payload, **kwargs)

    async def _throttle_kline(self) -> None:
        # Official public kline limit is 20 requests / 2 seconds. Keep margin.
        async with self._kline_lock:
            wait = 0.12 - (time.monotonic() - self._last_kline_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_kline_request = time.monotonic()

    async def _kline_get(self, url: str, params: dict[str, Any]) -> httpx.Response:
        return await request_with_retry(
            self.http,
            "GET",
            url,
            params=params,
            attempts=self.retry_attempts,
            base_delay=self.retry_base_delay,
            before_attempt=self._throttle_kline,
            retry_response=self._retry_payload,
        )

    async def _ticker_data(self) -> list[dict[str, Any]]:
        r = await self._get(f"{self.base_url}/api/v1/contract/ticker")
        data = self._data(r)
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            raise ExchangeAPIError("MEXC ticker data must be a list")
        return data

    async def top_symbols(self, n: int) -> list[tuple[str, float]]:
        data = await self._ticker_data()
        rows: list[tuple[str, float]] = []
        for t in data:
            symbol = str(t.get("symbol", ""))
            if not symbol.endswith("_USDT"):
                continue
            base = symbol.removesuffix("_USDT")
            if excluded_from_crypto_top(base):
                continue
            try:
                # amount24 is turnover; volume24 is a contract count, not USDT.
                qv = float(t.get("amount24") or 0.0)
            except (TypeError, ValueError):
                qv = 0.0
            if math.isfinite(qv) and qv > 0:
                rows.append((symbol, qv))
        rows.sort(key=lambda x: x[1], reverse=True)
        return rows[:n]

    async def control_symbols(self) -> dict[str, str]:
        # MEXC currently exposes these as XAU_USDT and USOIL_USDT. Resolve from the
        # exchange's own ticker list each run; absence means "skip", never fallback.
        available = {str(t.get("symbol", "")) for t in await self._ticker_data()}
        found: dict[str, str] = {}
        if "XAU_USDT" in available:
            found["XAU"] = "XAU_USDT"
        if "USOIL_USDT" in available:
            found["USOIL"] = "USOIL_USDT"
        return found

    async def candles(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> tuple[pd.DataFrame, float | None, float | None]:
        if timeframe not in {"1h", "1d"}:
            raise ValueError(f"unsupported timeframe: {timeframe}")
        interval = "Min60" if timeframe == "1h" else "Day1"
        max_span = timedelta(days=60 if timeframe == "1h" else 1000)
        cursor = start
        frames: list[pd.DataFrame] = []
        while cursor < end:
            chunk_end = min(end, cursor + max_span - timedelta(seconds=1))
            r = await self._kline_get(
                f"{self.base_url}/api/v1/contract/kline/{symbol}",
                {"interval": interval, "start": int(cursor.timestamp()), "end": int(chunk_end.timestamp())},
            )
            data = self._data(r)
            if not isinstance(data, dict):
                raise ExchangeAPIError("MEXC kline data must be an object")
            if isinstance(data, dict) and data.get("time"):
                times = data.get("time", [])
                frame = pd.DataFrame({
                    "timestamp": pd.to_datetime(times, unit="s", utc=True),
                    "open": data.get("open", []),
                    "high": data.get("high", []),
                    "low": data.get("low", []),
                    "close": data.get("close", []),
                    "volume": data.get("vol", data.get("volume", [])),
                })
                frames.append(frame)
            cursor = chunk_end + timedelta(seconds=1)
        if not frames:
            return pd.DataFrame(), None, None
        raw = pd.concat(frames, ignore_index=True)
        raw = restrict_current_history(raw, self.name, symbol, timeframe)
        if raw.empty:
            return pd.DataFrame(), None, None
        return _candle_result(raw, timeframe, end, is_control=symbol in {"XAU_USDT", "USOIL_USDT"})
