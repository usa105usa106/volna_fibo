from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta, timezone

import httpx

from config import Settings
from core_models import MarketSnapshot
from data_exchanges import BinanceSpotClient, ControlUnavailable, MexcFuturesClient
from data_integrity import IntegrityPolicy, validate_candles
from data_integrity import DataIntegrityError
from services_tasks import gather_owned


class MarketDataService:
    """Fresh exchange-only market-data loader.

    No parquet/database candle cache exists. Search and manual analysis always download
    their complete configured history from the selected exchange. Tracking persists only
    senior-wave state, never OHLC history.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._init_runtime_clients()

    def _init_runtime_clients(self) -> None:
        self.http = httpx.AsyncClient(timeout=self.settings.http_timeout_seconds, follow_redirects=True)
        client_kwargs = {
            "retry_attempts": self.settings.api_retry_attempts,
            "retry_base_delay": self.settings.api_retry_base_delay_seconds,
        }
        self.clients = {
            "binance_spot": BinanceSpotClient(self.http, **client_kwargs),
            "mexc_futures": MexcFuturesClient(self.http, **client_kwargs),
        }
        self.sem = asyncio.Semaphore(self.settings.http_concurrency)

    async def reset_runtime_state(self) -> None:
        """Drop HTTP connection pools/semaphores after Reset; no candle data are persisted."""
        old_http = self.http
        await old_http.aclose()
        self._init_runtime_clients()

    async def close(self):
        await self.http.aclose()

    async def universe(self, exchange: str, top_n: int) -> list[tuple[str, float]]:
        return await self.clients[exchange].top_symbols(top_n)

    async def available_controls(self, exchange: str) -> dict[str, str]:
        """Return XAU/USOIL instruments available on the exact selected exchange."""
        return await self.clients[exchange].control_symbols()

    async def _candles(self, exchange: str, symbol: str, timeframe: str, start: datetime, end: datetime):
        client = self.clients[exchange]
        if symbol in {"XAU", "USOIL"}:
            return await client.control_candles(symbol, timeframe, start, end)
        return await client.candles(symbol, timeframe, start, end)

    async def snapshot(self, exchange: str, symbol: str, quote_volume: float = 0.0) -> MarketSnapshot:
        """Download a full fresh 1H + 1D lookback for one instrument and validate it."""
        async with self.sem:
            now = datetime.now(timezone.utc)
            return await self._snapshot_window(
                exchange,
                symbol,
                quote_volume,
                now=now,
                h1_start=now - timedelta(days=self.settings.lookback_1h_days),
                d1_start=now - timedelta(days=self.settings.lookback_1d_days),
                check_freshness=True,
            )

    async def walk_history(
        self,
        exchange: str,
        symbol: str,
        quote_volume: float,
        *,
        h1_days: int,
        d1_days: int,
    ) -> MarketSnapshot:
        """One fresh in-memory history download for /walk. Never persists candles."""
        async with self.sem:
            now = datetime.now(timezone.utc)
            return await self._snapshot_window(
                exchange,
                symbol,
                quote_volume,
                now=now,
                h1_start=now - timedelta(days=h1_days),
                d1_start=now - timedelta(days=d1_days),
                check_freshness=True,
            )

    async def _snapshot_window(
        self,
        exchange: str,
        symbol: str,
        quote_volume: float,
        *,
        now: datetime,
        h1_start: datetime,
        d1_start: datetime,
        check_freshness: bool,
    ) -> MarketSnapshot:
        is_control = symbol in {"XAU", "USOIL"}

        # Resolve a commodity once per snapshot from the selected exchange itself.
        # There is deliberately no cross-exchange or Yahoo fallback.
        if is_control:
            client = self.clients[exchange]
            mapping = await client.control_symbols()
            remote = mapping.get(symbol)
            if remote is None:
                raise ControlUnavailable(f"{symbol} unavailable on {exchange}")
            h1_task = client.candles(remote, "1h", h1_start, now)
            d1_task = client.candles(remote, "1d", d1_start, now)
        else:
            h1_task = self._candles(exchange, symbol, "1h", h1_start, now)
            d1_task = self._candles(exchange, symbol, "1d", d1_start, now)
        h1_result, d1_result = await gather_owned(h1_task, d1_task)
        h1, live_price, live_low = h1_result
        d1, d_live_price, d_live_low = d1_result

        h1 = validate_candles(h1, IntegrityPolicy("1h", is_control, check_freshness), now)
        d1 = validate_candles(d1, IntegrityPolicy("1d", is_control, check_freshness), now)

        if live_price is None:
            live_price = d_live_price
        if live_low is None:
            live_low = d_live_low
        for label, value in (("live_price", live_price), ("live_low", live_low)):
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise DataIntegrityError(f"invalid {label}")
        if live_price is not None and live_low is not None and live_low > live_price:
            raise DataIntegrityError("live low exceeds live price")

        return MarketSnapshot(
            symbol=symbol,
            exchange=exchange,
            quote_volume=quote_volume,
            live_price=live_price,
            live_low=live_low,
            hourly_closed=h1,
            daily_closed=d1,
        )


__all__ = ["MarketDataService", "ControlUnavailable"]
