from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta, timezone

import httpx

from config import Settings
from core_models import MarketSnapshot
from core_majors import is_major
from core_major_rules import HISTORY_START, FULL_DAILY_START, cross_asset_context
from core_senior import complete4h
from core_symbols import CONTROL_BASES
from data_exchanges import BinanceFuturesClient, BinanceSpotClient, ControlUnavailable, MexcFuturesClient, MexcSpotHistoryClient
from data_integrity import IntegrityPolicy, validate_candles
from data_integrity import DataIntegrityError
from data_lineage import load_daily_context, transition_for
from services_tasks import gather_owned


class MarketDataService:
    """Fresh exchange-only market-data loader.

    No parquet/database candle cache exists. Current candles and documented ticker
    predecessor context are downloaded fresh. For documented TON/GRAM ancestry,
    separately validated spot context is allowed when futures history is missing.
    Tracking persists senior-wave state and provenance, never OHLC history.
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
        self.binance_futures = BinanceFuturesClient(self.http, **client_kwargs)
        self.spot_history_clients = (
            MexcSpotHistoryClient(self.http, **client_kwargs), self.clients["binance_spot"],
        )
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
        """Same exchange; Binance commodities use USD-M Futures, crypto Spot."""
        return await self._control_client(exchange).control_symbols()

    def _control_client(self, exchange: str):
        return self.binance_futures if exchange == "binance_spot" else self.clients[exchange]

    async def _candles(self, exchange: str, symbol: str, timeframe: str, start: datetime, end: datetime):
        client = self.clients[exchange]
        if symbol in CONTROL_BASES:
            return await self._control_client(exchange).control_candles(symbol, timeframe, start, end)
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
        is_control = symbol in CONTROL_BASES
        source_exchange = exchange
        if is_major(symbol):
            h1_start = min(h1_start, HISTORY_START.to_pydatetime())
            d1_start = FULL_DAILY_START.to_pydatetime()

        # Resolve a commodity once per snapshot from the selected exchange itself.
        # There is deliberately no cross-exchange or Yahoo fallback.
        if is_control:
            client = self._control_client(exchange)
            mapping = await client.control_symbols()
            remote = mapping.get(symbol)
            if remote is None:
                raise ControlUnavailable(f"{symbol} unavailable on {exchange}")
            source_exchange = client.name
            h1_task = client.candles(remote, "1h", h1_start, now)
            d1_task = client.candles(remote, "1d", d1_start, now)
        else:
            h1_task = self._candles(exchange, symbol, "1h", h1_start, now)
            d1_task = self._candles(exchange, symbol, "1d", d1_start, now)
        h1_result, d1_result = await gather_owned(h1_task, d1_task)
        h1, live_price, live_low = h1_result
        d1, d_live_price, d_live_low = d1_result
        live_candle = h1.attrs.get("live_candle")

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

        context, history = None, {}
        if transition_for(exchange, symbol) is not None:
            context, history = await load_daily_context(self.clients[exchange], symbol, d1, d1_start, now,
                                                        spot_clients=self.spot_history_clients)

        cross = {}
        if is_major(symbol):
            # Optional supporting evidence; a peer outage cannot invalidate a USD count.
            from core_symbols import display_symbol, normalize_symbol
            base = display_symbol(symbol)
            peer = normalize_symbol(exchange, "ETH" if base == "BTC" else "BTC")
            try:
                peer_h1, _, _ = await self._candles(exchange, peer, "1h", now-timedelta(days=8), now)
                peer_h1 = validate_candles(peer_h1, IntegrityPolicy("1h", False, check_freshness), now)
                own4, peer4 = complete4h(h1.tail(8*24)), complete4h(peer_h1)
                cross = cross_asset_context(own4 if base=="ETH" else peer4,
                                            peer4 if base=="ETH" else own4, exchange)
            except Exception as exc:  # noqa: BLE001 -- optional peer outage must never invalidate the USD count
                cross = {"status": "UNAVAILABLE", "score": 0.0, "market": exchange,
                         "objective": [.07, .08], "reason": type(exc).__name__}

        return MarketSnapshot(
            symbol=symbol,
            exchange=source_exchange,
            quote_volume=quote_volume,
            live_price=live_price,
            live_low=live_low,
            hourly_closed=h1,
            daily_closed=d1,
            daily_context=context,
            history_evidence=history,
            live_candle=live_candle,
            observed_at=now.isoformat(),
            cross_asset=cross,
        )


__all__ = ["MarketDataService", "ControlUnavailable"]
