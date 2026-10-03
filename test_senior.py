import pandas as pd
import pytest
from core_senior import complete4h, _fib_prices, _targets
from core_models import MarketSnapshot, WaveState
from core_senior import SeniorWaveDetector
from core_symbols import normalize_symbol
from services_formatter import telegram_table_messages
from core_ranking import SEARCH_TOP_CRYPTO, select_top_crypto
import asyncio
from data_exchanges import BinanceSpotClient, MexcFuturesClient
from pathlib import Path

ROOT = Path(__file__).resolve().parent
import ast
from config import Settings
from data_collector import MarketDataService
from core_toggles import next_exchange, next_interval_minutes, next_top_n
from data_integrity import DataIntegrityError, IntegrityPolicy, validate_candles
from data_http import request_with_retry
from services_runtime import cleanup_runtime_files
from services_formatter import _fmt


def test_complete4h_requires_exactly_four_closed_1h():
    ts = pd.date_range("2026-01-01T00:00:00Z", periods=7, freq="1h")
    df = pd.DataFrame(
        {
            "timestamp": ts,
            "open": range(7),
            "high": [x + 1 for x in range(7)],
            "low": [x - 1 for x in range(7)],
            "close": [x + 0.5 for x in range(7)],
            "volume": [1] * 7,
        }
    )
    out = complete4h(df)
    assert len(out) == 1
    assert out.iloc[0]["n"] == 4
    assert out.iloc[0]["timestamp"] == pd.Timestamp("2026-01-01T00:00:00Z")


def test_doge_fib_example_matches_migration_logic():
    fibs = _fib_prices(0.07831, 0.10589)
    assert abs(fibs["0.500"] - 0.09210) < 1e-8
    assert abs(fibs["0.382"] - 0.09535444) < 1e-8
    assert abs(fibs["0.236"] - 0.09938112) < 1e-8


def test_nested_targets_match_doge_example():
    targets = _targets(0.09113, 0.10589 - 0.07831)
    assert abs(targets[0] - 0.11871) < 1e-8
    assert abs(targets[1] - 0.13575444) < 1e-8
    assert abs(targets[2] - 0.16333444) < 1e-8


def _previous_state() -> WaveState:
    return WaveState(
        symbol="TESTUSDT",
        exchange="binance_spot",
        wave_type="W2",
        status="DEEP",
        origin=100.0,
        impulse_high=200.0,
        working_low=130.0,
        strict_origin=100.0,
        impulse_start_ts="2025-12-01T00:00:00+00:00",
        impulse_high_ts="2025-12-15T00:00:00+00:00",
        working_low_ts="2026-01-01T01:00:00+00:00",
        retrace_depth=0.70,
        fibs={},
        fib_status="< .950",
        targets=[230.0],
        current_price=135.0,
        rating=8.0,
        last_complete4h_bucket="2026-01-01T00:00:00+00:00",
        last_complete4h_close=132.0,
    )


def test_tracking_reanchors_only_on_new_complete4h_low():
    ts = pd.date_range("2026-01-01T00:00:00Z", periods=8, freq="1h")
    lows = [128, 127, 126, 125, 124, 123, 122, 120]
    df = pd.DataFrame(
        {
            "timestamp": ts,
            "open": [130] * 8,
            "high": [136] * 8,
            "low": lows,
            "close": [132] * 8,
            "volume": [1] * 8,
        }
    )
    snap = MarketSnapshot(
        "TESTUSDT", "binance_spot", 1.0, 132.0, 120.0, df, pd.DataFrame()
    )
    out = SeniorWaveDetector().track(_previous_state(), snap, 300)
    assert out.status != "INVALID"
    assert out.working_low == 120.0
    assert out.last_event == "RE-ANCHOR COMPLETE4H"


def test_tracking_invalidates_from_incomplete_4h_hourly_wick():
    ts = pd.date_range("2026-01-01T00:00:00Z", periods=9, freq="1h")
    lows = [128, 127, 126, 125, 124, 123, 122, 120, 99]
    df = pd.DataFrame(
        {
            "timestamp": ts,
            "open": [130] * 9,
            "high": [136] * 9,
            "low": lows,
            "close": [132] * 9,
            "volume": [1] * 9,
        }
    )
    snap = MarketSnapshot(
        "TESTUSDT", "binance_spot", 1.0, 132.0, 101.0, df, pd.DataFrame()
    )
    out = SeniorWaveDetector().track(_previous_state(), snap, 300)
    assert out.status == "INVALID"
    assert out.targets == []
    assert "FULL SENIOR RECOUNT" in out.last_event


def test_manual_ticker_normalization():
    assert normalize_symbol("binance_spot", "doge") == "DOGEUSDT"
    assert normalize_symbol("binance_spot", "DOGE/USDT") == "DOGEUSDT"
    assert normalize_symbol("mexc_futures", "pol") == "POL_USDT"
    assert normalize_symbol("mexc_futures", "SOL_USDT") == "SOL_USDT"
    assert normalize_symbol("binance_spot", "xau") == "XAU"
    assert normalize_symbol("mexc_futures", "usoil") == "USOIL"


def test_favorable_fields_are_bold_in_telegram_table():
    state = WaveState(
        symbol="DOGEUSDT",
        exchange="binance_spot",
        wave_type="W3-(2)",
        status="CONFIRMED",
        origin=0.07831,
        impulse_high=0.10589,
        working_low=0.09113,
        strict_origin=0.07831,
        retrace_depth=0.535,
        fib_status="> .382 · 2/3 C4H",
        targets=[0.11871, 0.13575],
        base_zone=(0.086, 0.090),
        deep_zone=(0.080, 0.086),
        current_price=0.093,
        growth_from_low_pct=2.05,
        strict_distance_pct=16.37,
        rating=9.2,
    )
    msg = telegram_table_messages([state], "TEST")[0]
    assert "DOGE" in msg
    assert "<b>DOGE</b>" not in msg
    assert "<b>9.2</b>" in msg
    # 2/3 recovery is valid but not strong enough for the sparse 3/3 emphasis rule.
    assert "<b>&gt; .382 · 2/3 C4H</b>" not in msg
    assert "&gt; .382 · 2/3 C4H" in msg
    assert "DOGEUSDT" not in msg


def test_complete4h_rejects_offset_hourly_timestamps_even_if_count_is_four():
    ts = pd.date_range("2026-01-01T00:30:00Z", periods=4, freq="1h")
    df = pd.DataFrame(
        {
            "timestamp": ts,
            "open": [1.0] * 4,
            "high": [2.0] * 4,
            "low": [0.5] * 4,
            "close": [1.5] * 4,
            "volume": [1.0] * 4,
        }
    )
    assert complete4h(df).empty


def test_search_selection_hard_limits_crypto_to_top10_and_excludes_controls():
    states = []
    for i in range(15):
        states.append(
            WaveState(
                symbol=f"C{i}USDT",
                exchange="binance_spot",
                wave_type="W2",
                status="DEEP",
                origin=80.0,
                impulse_high=120.0,
                working_low=90.0,
                strict_origin=80.0,
                rating=float(i),
                current_price=100.0,
            )
        )
    states.append(
        WaveState(
            symbol="XAU",
            exchange="binance_spot",
            wave_type="CONTROL",
            status="NO_SETUP",
            origin=None,
            impulse_high=None,
            working_low=None,
            strict_origin=None,
            rating=10.0,
            current_price=4000.0,
            is_control=True,
        )
    )

    selected = select_top_crypto(states)
    assert SEARCH_TOP_CRYPTO == 10
    assert len(selected) == 10
    assert [s.symbol for s in selected] == [f"C{i}USDT" for i in range(14, 4, -1)]
    assert all(not s.is_control for s in selected)


def test_formatter_renders_controls_in_separate_unranked_section():
    crypto = WaveState(
        symbol="DOGEUSDT",
        exchange="binance_spot",
        wave_type="W2",
        status="DEEP",
        origin=1.0,
        impulse_high=2.0,
        working_low=1.2,
        strict_origin=1.0,
        rating=9.0,
        current_price=1.25,
    )
    xau = WaveState(
        symbol="XAU",
        exchange="binance_spot",
        wave_type="CONTROL",
        status="NO_SETUP",
        origin=None,
        impulse_high=None,
        working_low=None,
        strict_origin=None,
        rating=None,
        current_price=4000.0,
        is_control=True,
    )
    usoil = WaveState(
        symbol="USOIL",
        exchange="binance_spot",
        wave_type="CONTROL",
        status="NO_SETUP",
        origin=None,
        impulse_high=None,
        working_low=None,
        strict_origin=None,
        rating=None,
        current_price=90.0,
        is_control=True,
    )
    msgs = telegram_table_messages([xau, crypto, usoil], "TEST", crypto_label="TOP-10")
    joined = "\n".join(msgs)
    assert "TOP-10" in joined
    assert "XAU / USOIL — ДОПОЛНИТЕЛЬНО, ВНЕ РЕЙТИНГА" in joined
    assert "1 | DOGE" in joined
    assert "— | XAU" in joined
    assert "— | USOIL" in joined
    assert "2 | XAU" not in joined


def test_no_setup_does_not_claim_targets_are_after_recount():
    state = WaveState(
        symbol="XAU",
        exchange="binance_spot",
        wave_type="CONTROL",
        status="NO_SETUP",
        origin=None,
        impulse_high=None,
        working_low=None,
        strict_origin=None,
        rating=None,
        current_price=4000.0,
        is_control=True,
    )
    msg = "\n".join(telegram_table_messages([state], "TEST"))
    assert "after recount" not in msg
    assert "NO_SETUP / CONTROL" in msg


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeBinanceHTTP:
    async def get(self, url, **kwargs):
        if url.endswith("/exchangeInfo"):
            return _FakeResponse(
                {
                    "symbols": [
                        {
                            "symbol": "DOGEUSDT",
                            "status": "TRADING",
                            "quoteAsset": "USDT",
                            "baseAsset": "DOGE",
                            "isSpotTradingAllowed": True,
                        },
                        {
                            "symbol": "SOLUSDT",
                            "status": "TRADING",
                            "quoteAsset": "USDT",
                            "baseAsset": "SOL",
                            "isSpotTradingAllowed": True,
                        },
                        {
                            "symbol": "USDCUSDT",
                            "status": "TRADING",
                            "quoteAsset": "USDT",
                            "baseAsset": "USDC",
                            "isSpotTradingAllowed": True,
                        },
                        {
                            "symbol": "BTCUPUSDT",
                            "status": "TRADING",
                            "quoteAsset": "USDT",
                            "baseAsset": "BTCUP",
                            "isSpotTradingAllowed": True,
                        },
                        {
                            "symbol": "XAUUSDT",
                            "status": "TRADING",
                            "quoteAsset": "USDT",
                            "baseAsset": "XAU",
                            "isSpotTradingAllowed": True,
                        },
                        {
                            "symbol": "USOILUSDT",
                            "status": "TRADING",
                            "quoteAsset": "USDT",
                            "baseAsset": "USOIL",
                            "isSpotTradingAllowed": True,
                        },
                    ]
                }
            )
        return _FakeResponse(
            [
                {"symbol": "DOGEUSDT", "quoteVolume": "100"},
                {"symbol": "SOLUSDT", "quoteVolume": "200"},
                {"symbol": "USDCUSDT", "quoteVolume": "999999"},
                {"symbol": "BTCUPUSDT", "quoteVolume": "999998"},
                {"symbol": "XAUUSDT", "quoteVolume": "999997"},
                {"symbol": "USOILUSDT", "quoteVolume": "999996"},
            ]
        )


class _FakeMexcHTTP:
    async def get(self, url, **kwargs):
        return _FakeResponse(
            {
                "success": True,
                "code": 0,
                "data": [
                    {"symbol": "DOGE_USDT", "amount24": "100"},
                    {"symbol": "SOL_USDT", "amount24": "200"},
                    {"symbol": "USDC_USDT", "amount24": "999999"},
                    {"symbol": "ETHDOWN_USDT", "amount24": "999998"},
                    {"symbol": "XAU_USDT", "amount24": "999997"},
                    {"symbol": "USOIL_USDT", "amount24": "999996"},
                ],
            }
        )


def test_binance_top_n_excludes_stables_and_leveraged_tokens_before_ranking():
    rows = asyncio.run(BinanceSpotClient(_FakeBinanceHTTP()).top_symbols(10))
    assert rows == [("SOLUSDT", 200.0), ("DOGEUSDT", 100.0)]
    assert all(symbol not in {"XAUUSDT", "USOILUSDT"} for symbol, _ in rows)


def test_mexc_top_n_excludes_stables_and_leveraged_tokens_before_ranking():
    rows = asyncio.run(MexcFuturesClient(_FakeMexcHTTP()).top_symbols(10))
    assert rows == [("SOL_USDT", 200.0), ("DOGE_USDT", 100.0)]
    assert all(symbol not in {"XAU_USDT", "USOIL_USDT"} for symbol, _ in rows)


class _FreshProvider:
    def __init__(self):
        self.calls = []

    async def candles(self, symbol, timeframe, start, end):
        self.calls.append((symbol, timeframe, start, end))
        ts = (
            (pd.Timestamp(end).floor("h") - pd.Timedelta(hours=2))
            if timeframe == "1h"
            else (pd.Timestamp(end).floor("D") - pd.Timedelta(days=1))
        )
        frame = pd.DataFrame(
            {
                "timestamp": [ts],
                "open": [1.0],
                "high": [1.1],
                "low": [0.9],
                "close": [1.0],
                "volume": [1.0],
            }
        )
        return frame, 1.0, 0.9


async def _exercise_fresh_snapshot(tmp_path: Path):
    cfg = Settings(BOT_TOKEN="test", DATA_DIR=tmp_path)
    service = MarketDataService(cfg)
    provider = _FreshProvider()
    service.clients["binance_spot"] = provider
    try:
        await service.snapshot("binance_spot", "DOGEUSDT", 123.0)
        await service.snapshot("binance_spot", "DOGEUSDT", 123.0)
    finally:
        await service.close()
    return cfg, provider.calls


def test_market_snapshot_redownloads_full_lookback_every_time(tmp_path):
    cfg, calls = asyncio.run(_exercise_fresh_snapshot(tmp_path))
    assert [c[1] for c in calls] == ["1h", "1d", "1h", "1d"]
    for _, timeframe, start, end in calls:
        span = end - start
        if timeframe == "1h":
            assert abs(span.total_seconds() / 86400 - cfg.lookback_1h_days) < 0.01
        else:
            assert abs(span.total_seconds() / 86400 - cfg.lookback_1d_days) < 0.01
    assert not (tmp_path / "parquet").exists()


def test_controller_marks_interval_anchor_after_report_call():
    """AST regression test: reporting must precede mark_report_complete in both run paths."""
    source = (ROOT / "bot_handlers.py").read_text()
    tree = ast.parse(source)
    cls = next(
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "BotController"
    )
    for name in ("run_and_report", "scheduled_run"):
        fn = next(
            n
            for n in cls.body
            if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == name
        )
        report_lines = []
        mark_lines = []
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "_report":
                    report_lines.append(node.lineno)
                if node.func.attr == "mark_report_complete":
                    mark_lines.append(node.lineno)
        assert report_lines and mark_lines
        assert min(report_lines) < min(mark_lines)


def test_cyclic_toggle_sequences_are_exact():
    assert [next_top_n(x) for x in (100, 200, 300)] == [200, 300, 100]
    assert [next_interval_minutes(x) for x in (30, 60, 240, 720)] == [60, 240, 720, 30]
    assert next_exchange("binance_spot") == "mexc_futures"
    assert next_exchange("mexc_futures") == "binance_spot"


def test_keyboard_source_uses_single_cyclic_setting_buttons_and_mode_dot():
    source = (ROOT / "bot_keyboards.py").read_text()
    tree = ast.parse(source)

    # The active action mode is marked with a dot, while toggle buttons use only
    # the current visible value (no checkmark clutter).
    assert 'f"● {text}"' in source
    assert 'f"✅ {text}"' not in source
    assert "KeyboardButton(text=TOP_LABELS.get(s.top_n" in source
    assert "KeyboardButton(text=INTERVAL_LABELS.get(s.interval_minutes" in source
    assert "KeyboardButton(text=EXCHANGE_LABELS.get(s.exchange" in source

    keyboard_fn = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "keyboard"
    )
    button_calls = [
        n
        for n in ast.walk(keyboard_fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "KeyboardButton"
    ]
    # 2 action buttons + 3 cyclic-setting buttons + Reset + Ping.
    assert len(button_calls) == 7
    assert 'KeyboardButton(text="Сброс")' in source


def test_clean_button_source_accepts_current_dot_and_legacy_checkmark():
    source = (ROOT / "bot_keyboards.py").read_text()
    assert '("● ", "✅ ")' in source


def _ohlc_frame(timestamps):
    n = len(timestamps)
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [10.0] * n,
            "high": [11.0] * n,
            "low": [9.0] * n,
            "close": [10.5] * n,
            "volume": [1.0] * n,
        }
    )


def test_integrity_rejects_internal_crypto_hourly_gap():
    ts = [
        pd.Timestamp("2026-01-01T00:00:00Z"),
        pd.Timestamp("2026-01-01T01:00:00Z"),
        pd.Timestamp("2026-01-01T03:00:00Z"),
    ]
    with pytest.raises(DataIntegrityError, match="internal candle gap"):
        validate_candles(
            _ohlc_frame(ts),
            IntegrityPolicy("1h", is_control=False, check_freshness=False),
        )


def test_integrity_allows_weekend_sized_control_gap_but_not_bad_ohlc():
    ts = [
        pd.Timestamp("2026-01-02T20:00:00Z"),
        pd.Timestamp("2026-01-05T00:00:00Z"),
    ]
    out = validate_candles(
        _ohlc_frame(ts),
        IntegrityPolicy("1h", is_control=True, check_freshness=False),
    )
    assert len(out) == 2
    bad = _ohlc_frame([pd.Timestamp("2026-01-01T00:00:00Z")])
    bad.loc[0, "high"] = 8.0
    with pytest.raises(DataIntegrityError, match="impossible OHLC"):
        validate_candles(bad, IntegrityPolicy("1h", check_freshness=False))


def test_selected_exchange_control_resolution_is_exact_and_no_proxy_alias():
    b = asyncio.run(BinanceSpotClient(_FakeBinanceHTTP()).control_symbols())
    m = asyncio.run(MexcFuturesClient(_FakeMexcHTTP()).control_symbols())
    assert b == {"XAU": "XAUUSDT", "USOIL": "USOILUSDT"}
    assert m == {"XAU": "XAU_USDT", "USOIL": "USOIL_USDT"}


class _RetryResponse:
    def __init__(self, status_code):
        self.status_code = status_code
        self.headers = {}


class _RetryHTTP:
    def __init__(self):
        self.calls = 0

    async def request(self, method, url, **kwargs):
        self.calls += 1
        return _RetryResponse(500 if self.calls < 3 else 200)


def test_api_retry_backoff_reaches_success_after_retryable_5xx():
    client = _RetryHTTP()
    response = asyncio.run(
        request_with_retry(
            client, "GET", "https://example.invalid", attempts=4, base_delay=0.001
        )
    )
    assert response.status_code == 200
    assert client.calls == 3


def test_safe_search_reset_commits_only_after_report_delivery():
    scanner_source = (
        (ROOT / "services_scanner.py").read_text()
    )
    scanner_tree = ast.parse(scanner_source)
    scanner_cls = next(
        n
        for n in scanner_tree.body
        if isinstance(n, ast.ClassDef) and n.name == "ScannerService"
    )
    search_fn = next(
        n
        for n in scanner_cls.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "search"
    )
    attrs = [
        n.func.attr
        for n in ast.walk(search_fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    ]
    assert "clear_active_session" not in attrs
    assert "replace_active_session" not in attrs

    handler_source = (
        (ROOT / "bot_handlers.py").read_text()
    )
    handler_tree = ast.parse(handler_source)
    controller_cls = next(
        n
        for n in handler_tree.body
        if isinstance(n, ast.ClassDef) and n.name == "BotController"
    )
    run_fn = next(
        n
        for n in controller_cls.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_and_report"
    )
    calls = {}
    for node in ast.walk(run_fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            calls.setdefault(node.func.attr, []).append(node.lineno)
    assert (
        min(calls["_report"])
        < min(calls["commit_search"])
        < min(calls["mark_report_complete"])
    )


def test_walk_forward_is_state_read_only_and_separate_from_search():
    source = (ROOT / "services_scanner.py").read_text()
    tree = ast.parse(source)
    cls = next(
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "ScannerService"
    )
    fn = next(
        n
        for n in cls.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "walk_forward"
    )
    called_attrs = {
        n.func.attr
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert "replace_active_session" not in called_attrs
    assert "update_states" not in called_attrs
    assert "set_mode" not in called_attrs
    assert "mark_report_complete" not in called_attrs


def test_no_yahoo_commodity_proxy_remains_in_runtime_source():
    root = ROOT
    joined = "\n".join(
        p.read_text() for p in root.rglob("*.py") if not p.name.startswith("test_")
    )
    assert "YahooCommodityClient" not in joined
    assert "GC=F" not in joined
    assert "CL=F" not in joined
    assert "query1.finance.yahoo.com" not in joined


def test_version_0015_is_default():
    cfg = Settings(BOT_TOKEN="test")
    assert cfg.bot_version == "0015"
    assert cfg.default_top_n == 100


def test_repository_reset_source_clears_sessions_kv_and_restores_idle_defaults():
    source = (ROOT / "db_repository.py").read_text()
    tree = ast.parse(source)
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Repository"
    )
    fn = next(
        n
        for n in cls.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "reset_to_defaults"
    )
    assert "DELETE FROM tracked_setups" in source
    assert "DELETE FROM search_sessions" in source
    assert "DELETE FROM kv" in source
    assert 'mode="idle"' in source
    attrs = [
        n.func.attr
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    ]
    assert "commit" in attrs
    assert "rollback" in attrs


def test_runtime_cleanup_removes_temp_artifacts_but_preserves_sqlite(tmp_path):
    db = tmp_path / "senior_wave_bot.sqlite3"
    db.write_bytes(b"db")
    (tmp_path / "tmp").mkdir()
    (tmp_path / "tmp" / "a.bin").write_bytes(b"x")
    (tmp_path / "parquet").mkdir()
    (tmp_path / "stale.part").write_bytes(b"x")
    (tmp_path / "tmp_report.tmp").write_bytes(b"x")
    (tmp_path / "keep.txt").write_text("keep")

    cleanup_runtime_files(tmp_path, db)

    assert db.exists()
    assert (tmp_path / "keep.txt").exists()
    assert not (tmp_path / "tmp").exists()
    assert not (tmp_path / "parquet").exists()
    assert not (tmp_path / "stale.part").exists()
    assert not (tmp_path / "tmp_report.tmp").exists()


def test_reset_handler_stops_scheduler_cancels_tasks_and_clears_state():
    source = (ROOT / "bot_handlers.py").read_text()
    tree = ast.parse(source)
    cls = next(
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "BotController"
    )
    fn = next(
        n
        for n in cls.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_reset"
    )
    attrs = [
        n.func.attr
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    ]
    assert "stop" in attrs
    assert "cancel" in attrs
    assert "reset_to_defaults" in attrs
    assert "reset_runtime_state" in attrs
    assert "clear" in attrs
    assert "start" in attrs


def test_scheduler_can_restart_after_stop():
    source = (ROOT / "services_scheduler.py").read_text()
    assert "self._task = None" in source


def test_fixed_decimal_format_preserves_dogs_style_price_without_scientific_notation():
    text = _fmt(0.00004901)
    assert text == "0.00004901"
    assert "e" not in text.lower()


def test_fixed_decimal_format_handles_ultra_small_nonzero_values_without_e_notation_or_zero_collapse():
    for value in (1e-8, 1.23456789e-10, 9.87654321e-13, 1e-20):
        text = _fmt(value)
        assert "e" not in text.lower()
        assert text.startswith("0.")
        assert text != "0"


def test_telegram_table_never_uses_scientific_notation_for_price_low_zones_or_targets():
    state = WaveState(
        symbol="DOGSUSDT",
        exchange="binance_spot",
        wave_type="W2",
        status="DEEP",
        origin=0.00004001,
        impulse_high=0.00006001,
        working_low=0.00004901,
        strict_origin=0.00004001,
        rating=9.1,
        current_price=4.912e-05,
        growth_from_low_pct=0.2244,
        strict_distance_pct=22.49,
        retrace_depth=0.55,
        fib_status="> .786 · 2/3 C4H",
        base_zone=(4.701e-05, 4.901e-05),
        deep_zone=(4.301e-05, 4.601e-05),
        targets=[6.901e-05, 8.137e-05, 0.00010137],
    )
    msg = "\n".join(telegram_table_messages([state], "TEST"))
    assert "0.00004912" in msg
    assert "0.00004901" in msg
    assert "0.00004701" in msg
    assert "0.00006901" in msg
    assert "e-" not in msg.lower()
    assert "e+" not in msg.lower()


def test_readme_and_runtime_have_no_stale_v0007_markers():
    root = ROOT
    files = [root / "README.md", root / ".env.example", root / "Dockerfile"]
    joined = "\n".join(p.read_text() for p in files)
    assert "0007" not in joined
    assert "0015" in joined


def test_tracking_bad_fresh_data_is_report_only_and_not_persisted_source_contract():
    source = (ROOT / "services_scanner.py").read_text()
    # Diagnostic rows must be visibly marked and returned with no persistable state.
    assert 'shown.status = "DATA_INCOMPLETE"' in source
    assert "shown.current_price = None" in source
    assert "return shown, None" in source
    assert (
        "persistable = [saved for _, saved in gathered if saved is not None]" in source
    )
    assert "await self.repo.update_states(session_id, persistable)" in source


def test_active_session_swap_is_explicit_transaction_with_rollback():
    source = (ROOT / "db_repository.py").read_text()
    assert "BEGIN IMMEDIATE" in source
    assert "await db.rollback()" in source
    assert "UPDATE search_sessions SET active=0 WHERE active=1" in source


def test_runtime_cleanup_preserves_sqlite_recovery_sidecars(tmp_path):
    db = tmp_path / "senior_wave_bot.sqlite3"
    db.write_bytes(b"db")
    for suffix in ("-wal", "-shm", "-journal"):
        Path(str(db) + suffix).write_bytes(b"tmp")
    cleanup_runtime_files(tmp_path, db)
    assert db.exists()
    for suffix in ("-wal", "-shm", "-journal"):
        assert Path(str(db) + suffix).exists()
