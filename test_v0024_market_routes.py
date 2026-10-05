"""Exchange routing regressions. HTTP fixtures are synthetic unless noted."""
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pandas as pd
import pytest

from bot_handlers import BotController
from conftest import state
from core_models import WaveState
from core_senior import SeniorWaveDetector, complete4h
from core_symbols import excluded_from_crypto_top, normalize_symbol
from data_collector import MarketDataService
from data_exchanges import (
    BinanceFuturesClient,
    BinanceSpotClient,
    ControlUnavailable,
    MexcFuturesClient,
    MexcSpotHistoryClient,
)
from data_integrity import DataIntegrityError
from replay_archive import ArchiveReader
from services_formatter import (
    _row_values_flags,
    technical_report_text,
    telegram_table_messages,
)
from services_scanner import RunResult, ScannerService

FIXTURES = Path(__file__).parent / "fixtures"
NOW = pd.Timestamp("2026-07-04T00:00:00Z")


def contract(base, **overrides):
    return {"symbol": f"{base}USDT", "baseAsset": base, "quoteAsset": "USDT",
            "marginAsset": "USDT", "status": "TRADING", "contractType": "PERPETUAL", **overrides}


def binance_rows(times, interval="1h", price=10.):
    step = pd.Timedelta(hours=1) if interval == "1h" else pd.Timedelta(days=1)
    return [[int(t.timestamp() * 1000), str(price), str(price + 1), str(price - 1),
             str(price), "100", int((t + step).timestamp() * 1000) - 1, "1000", 1, "50", "500", "0"]
            for t in times]


def raw_times(request, *, mexc=False):
    params = request.url.params
    if mexc:
        start = pd.Timestamp(int(params["start"]), unit="s", tz="UTC")
        end = pd.Timestamp(int(params["end"]), unit="s", tz="UTC")
        step = "h" if params["interval"] == "Min60" else "D"
        return pd.date_range(start.ceil(step), end.floor(step), freq=step)
    start = pd.Timestamp(int(params["startTime"]), unit="ms", tz="UTC")
    end = pd.Timestamp(int(params["endTime"]), unit="ms", tz="UTC")
    step = "h" if params["interval"] == "1h" else "D"
    return pd.date_range(start.ceil(step), end.floor(step), freq=step)[:int(params["limit"])]


async def install_http(service, handler):
    await service.http.aclose()
    service.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service.clients = {
        "binance_spot": BinanceSpotClient(service.http, retry_attempts=1),
        "mexc_futures": MexcFuturesClient(service.http, retry_attempts=1),
    }
    service.binance_futures = BinanceFuturesClient(service.http, retry_attempts=1)
    service.spot_history_clients = (
        MexcSpotHistoryClient(service.http, retry_attempts=1), service.clients["binance_spot"],
    )
    service.clients["mexc_futures"]._throttle_kline = AsyncMock()


@pytest.mark.parametrize("exchange", ["binance_spot", "mexc_futures"])
@pytest.mark.parametrize("raw,expected", [
    ("xau", "XAU"), ("XAUUSDT", "XAU"), ("xag", "XAG"), ("XAG_USDT", "XAG"),
    ("xag/usdt", "XAG"), ("usoil", "USOIL"), ("USOIL-USDT", "USOIL"),
])
def test_commodity_input_routes_canonically(exchange, raw, expected):
    assert normalize_symbol(exchange, raw) == expected


def test_mexc_silver_alias_and_unrelated_crypto_remain_distinct():
    assert normalize_symbol("mexc_futures", "SILVER_USDT") == "XAG"
    for base in ("DOGE", "POL", "SOL", "GRAM", "XAUT"):
        assert normalize_symbol("binance_spot", base) == base + "USDT"
        assert normalize_symbol("mexc_futures", base) == base + "_USDT"
    assert excluded_from_crypto_top("XAG") and excluded_from_crypto_top("SILVER")


@pytest.mark.asyncio
async def test_binance_uses_only_live_exact_usdt_perpetual_metadata():
    rows = [contract("XAU"), contract("XAG"), contract("XAUT"),
            contract("USOIL", status="PENDING_TRADING"),
            contract("XAU", symbol="XAUUSDT_261225", contractType="CURRENT_QUARTER"),
            contract("XAG", symbol="XAGUSDC", quoteAsset="USDC", marginAsset="USDC")]

    def handler(request):
        assert str(request.url) == "https://fapi.binance.com/fapi/v1/exchangeInfo"
        return httpx.Response(200, json={"symbols": rows})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BinanceFuturesClient(http)
        assert await client.control_symbols() == {"XAU": "XAUUSDT", "XAG": "XAGUSDT"}
        rows[:] = [contract("XAUT")]
        assert await client.control_symbols() == {}
        rows[:] = [contract("USOIL")]
        # Absence is discovered, never hardcoded permanently by ticker.
        assert await client.control_symbols() == {"USOIL": "USOILUSDT"}


@pytest.mark.asyncio
@pytest.mark.parametrize("base", ["XAU", "XAG"])
async def test_binance_commodity_snapshot_fetches_both_timeframes_from_futures(cfg, base):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.host == "fapi.binance.com"
        if request.url.path.endswith("exchangeInfo"):
            return httpx.Response(200, json={"symbols": [contract("XAU"), contract("XAG")]})
        assert request.url.path == "/fapi/v1/klines"
        assert request.url.params["symbol"] == base + "USDT"
        return httpx.Response(200, json=binance_rows(raw_times(request), request.url.params["interval"]))

    service = MarketDataService(cfg)
    await install_http(service, handler)
    try:
        snap = await service._snapshot_window("binance_spot", base, 0, now=NOW,
            h1_start=NOW-pd.Timedelta(hours=1200), d1_start=NOW-pd.Timedelta(days=50), check_freshness=True)
    finally:
        await service.close()
    assert snap.symbol == base and snap.exchange == "binance_futures"
    assert len(snap.hourly_closed) == 1200 and len(snap.daily_closed) == 50
    assert len(complete4h(snap.hourly_closed)) == 300
    assert sum(r.url.params.get("interval") == "1h" for r in calls) == 2
    assert snap.history_evidence == {} and snap.daily_context is None


@pytest.mark.asyncio
async def test_binance_usoil_absence_makes_no_candle_request_or_other_exchange_fallback(cfg):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        assert request.url.host == "fapi.binance.com"
        assert request.url.path.endswith("exchangeInfo")
        return httpx.Response(200, json={"symbols": [contract("XAU"), contract("XAG")]})

    service = MarketDataService(cfg)
    await install_http(service, handler)
    try:
        with pytest.raises(ControlUnavailable):
            await service.snapshot("binance_spot", "USOIL")
    finally:
        await service.close()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_mexc_xag_resolves_silver_and_preserves_commodity_calendar(cfg):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.host == "api.mexc.com"
        if request.url.path.endswith("ticker"):
            return httpx.Response(200, json={"success": True, "code": 0,
                "data": [{"symbol": "SILVER_USDT"}, {"symbol": "XAU_USDT"}]})
        assert request.url.path == "/api/v1/contract/kline/SILVER_USDT"
        times = raw_times(request, mexc=True)
        times = times[times.dayofweek < 5]  # actual calendars may omit weekend hours
        return httpx.Response(200, json={"success": True, "code": 0, "data": {
            "time": [int(t.timestamp()) for t in times], "open": [61.] * len(times),
            "high": [62.] * len(times), "low": [60.] * len(times),
            "close": [61.] * len(times), "vol": [100.] * len(times)}})

    service = MarketDataService(cfg)
    await install_http(service, handler)
    try:
        snap = await service._snapshot_window("mexc_futures", "XAG", 0, now=NOW,
            h1_start=NOW-pd.Timedelta(days=20), d1_start=NOW-pd.Timedelta(days=50), check_freshness=True)
    finally:
        await service.close()
    assert snap.symbol == "XAG" and snap.exchange == "mexc_futures"
    assert len(complete4h(snap.hourly_closed)) > 40
    assert {r.url.params.get("interval") for r in calls} >= {"Min60", "Day1"}


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["duplicate", "ohlc", "close_time"])
async def test_binance_metals_still_reject_corrupt_candles(bad):
    rows = binance_rows(pd.date_range("2026-07-01", periods=4, freq="h", tz="UTC"))
    if bad == "duplicate":
        rows.append(rows[-1])
    elif bad == "ohlc":
        rows[1][3] = "99"
    else:
        rows[1][6] += 1
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=rows))) as http:
        with pytest.raises(DataIntegrityError):
            await BinanceFuturesClient(http).candles("XAGUSDT", "1h", NOW-pd.Timedelta(days=3), NOW)


@pytest.mark.asyncio
async def test_binance_manual_only_usoil_is_unavailable_and_controls_do_not_enter_crypto(cfg, repo):
    def handler(request):
        if request.url.path.endswith("exchangeInfo"):
            return httpx.Response(200, json={"symbols": [contract("XAU"), contract("XAG")]})
        symbol = request.url.params["symbol"]
        if symbol in {"XAUUSDT", "XAGUSDT"}:
            assert request.url.host == "fapi.binance.com"
        else:
            assert request.url.host == "api.binance.com" and symbol == "DOGEUSDT"
        return httpx.Response(200, json=binance_rows(raw_times(request), request.url.params["interval"]))

    service = MarketDataService(cfg)
    await install_http(service, handler)
    service.universe = AsyncMock(return_value=[("DOGEUSDT", 1.)])

    async def snapshot(exchange, symbol, volume):
        return await service._snapshot_window(exchange, symbol, volume, now=NOW,
            h1_start=NOW-pd.Timedelta(days=50), d1_start=NOW-pd.Timedelta(days=60), check_freshness=True)

    service.snapshot = snapshot
    try:
        result = await ScannerService(cfg, repo, service).analyze_symbols(["xau", "xag", "usoil", "doge", "XAG_USDT"])
    finally:
        await service.close()
    assert result.errors == ["USOIL: UNAVAILABLE_ON_EXCHANGE"]
    by_symbol = {s.symbol: s for s in result.states}
    assert len(by_symbol) == 4
    for metal in ("XAU", "XAG"):
        assert by_symbol[metal].is_control and by_symbol[metal].exchange == "binance_futures"
        assert by_symbol[metal].status == "NO_SETUP"  # flat synthetic OHLC, not an invented wave
    assert by_symbol["DOGEUSDT"].exchange == "binance_spot" and not by_symbol["DOGEUSDT"].is_control
    assert await repo.active_session() is None


@pytest.mark.asyncio
async def test_reset_closes_and_recreates_futures_client_in_shared_http_pool(cfg):
    service = MarketDataService(cfg)
    old_http, old_client = service.http, service.binance_futures
    await service.reset_runtime_state()
    try:
        assert old_http.is_closed and service.binance_futures is not old_client
        assert service.binance_futures.http is service.http
        assert all(c.http is service.http for c in service.spot_history_clients)
    finally:
        await service.close()
    assert service.http.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize("archive_failure", [None, "checksum", "outage"])
async def test_binance_gram_snapshot_really_requests_predecessor_and_records_its_source(cfg, archive_failure):
    """Production HTTP path with real June TON ZIP bytes and synthetic new GRAM."""
    requests = []
    name = "TONUSDT-1d-2026-06.zip"

    def handler(request):
        requests.append(str(request.url))
        if request.url.host == "data.binance.vision":
            assert request.url.path.endswith(name) or request.url.path.endswith(name + ".CHECKSUM")
            if archive_failure == "outage":
                return httpx.Response(503, text="temporary outage")
            if request.url.path.endswith(".CHECKSUM"):
                checksum = "0" * 64 if archive_failure == "checksum" else (FIXTURES / "ticker_history" / (name + ".CHECKSUM")).read_text()
                return httpx.Response(200, text=checksum)
            return httpx.Response(200, content=(FIXTURES / "ticker_history" / name).read_bytes())
        assert request.url.host == "api.binance.com"
        assert request.url.params["symbol"] == "GRAMUSDT"
        times = raw_times(request)
        start = pd.Timestamp("2026-07-02T00:00Z") if request.url.params["interval"] == "1d" else pd.Timestamp("2026-07-02T08:00Z")
        return httpx.Response(200, json=binance_rows(times[times >= start], request.url.params["interval"]))

    service = MarketDataService(cfg)
    await install_http(service, handler)
    try:
        snap = await service._snapshot_window("binance_spot", "GRAMUSDT", 0, now=NOW,
            h1_start=pd.Timestamp("2026-07-02T08:00Z"), d1_start=pd.Timestamp("2026-06-01T00:00Z"), check_freshness=True)
    finally:
        await service.close()
    assert any("data.binance.vision" in url for url in requests)
    assert snap.exchange == "binance_spot" and len(snap.daily_closed) == 2
    if archive_failure:
        assert snap.daily_context is None and snap.history_evidence["status"] == "unavailable"
    else:
        assert snap.history_evidence["status"] == "restored"
        assert snap.history_evidence["prefix_rows"] == 29
        assert snap.history_evidence["archives"][0]["sha256"] == "3252e37cce2215426c8d7fb7dfc8b5734f64d180a83277213291e92ddbd7a76a"
        assert set(snap.daily_context.source_symbol) == {"TONUSDT", "GRAMUSDT"}
        assert snap.daily_context.source_exchange.eq("binance_spot").all()
        assert len(snap.daily_context) == 31


def test_gram_history_label_is_evidence_driven_and_does_not_change_calculations():
    reader = ArchiveReader(FIXTURES / "market_2057", FIXTURES / "ticker_history/binance_TON_1d.parquet")
    snapshot = reader.snapshot("GRAM")
    detected = SeniorWaveDetector().detect(snapshot, 30, 300)
    assert detected.wave_type == "W3-(2)"
    original = deepcopy(detected.to_dict())
    assert "TON→GRAM" in _row_values_flags(detected, 1)[0][4]
    assert detected.to_dict() == original
    detected.structure_evidence["history"] = {"status": "unavailable"}
    detected.structure_evidence["ancestry_incomplete"] = True
    assert "TON→GRAM" not in _row_values_flags(detected, 1)[0][4]
    assert "нет истории TON" in _row_values_flags(detected, 1)[0][4]


def test_xag_reporting_names_correct_market_without_changing_columns(cfg, repo):
    metal = state("XAG")
    metal.is_control = True
    metal.exchange = "binance_futures"
    result = RunResult("manual", [metal], 1, 0, [], "binance_spot", 300, ["XAG"])
    controller = BotController(cfg, repo, None)
    title, subtitle, _ = controller._report_titles(result)
    assert "Binance Futures" in subtitle
    text = technical_report_text([metal], heading=[title, subtitle])
    assert "XAU / XAG / USOIL" in text and "market=binance_futures" in text
    html = "\n".join(telegram_table_messages([metal], title))
    assert "XAU / XAG / USOIL" in html
    assert WaveState.from_dict(metal.to_dict()).exchange == "binance_futures"
