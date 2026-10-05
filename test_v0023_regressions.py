"""v0023: real candle regressions and explicitly synthetic API/lifecycle controls."""
import asyncio
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import httpx
import pandas as pd
import pytest

from core_models import WaveState
from core_ranking import select_top_crypto
from core_senior import SeniorWaveDetector
from core_symbols import excluded_from_crypto_top
from data_exchanges import BinanceSpotClient, MexcFuturesClient, MexcSpotHistoryClient
from data_integrity import DataIntegrityError
from data_lineage import TRANSITIONS, load_daily_context, validate_spot_context
from replay_archive import ArchiveReader
from services_formatter import _row_values_flags, _targets, technical_report_text
from test_v0022_phase_regressions import append_hours

FIXTURES = Path(__file__).parent / 'fixtures'


@pytest.fixture(scope='module')
def archive():
    return ArchiveReader(FIXTURES / 'market_2057', FIXTURES / 'ticker_history/binance_TON_1d.parquet',
                         FIXTURES / 'hourly_history/binance_BCH_ZEC_1h_prefix.parquet')


@pytest.mark.parametrize('base', ['MUSTOCK', 'intcstock', 'STOCKABC', 'ABCstockXYZ'])
def test_stock_substring_is_case_insensitive(base):
    assert excluded_from_crypto_top(base)


@pytest.mark.parametrize('base', ['JUP', 'SOL', 'DOGE', 'POL', 'TON', 'GRAM'])
def test_regular_assets_remain_eligible(base):
    assert not excluded_from_crypto_top(base)


@pytest.mark.parametrize('client_cls', [BinanceSpotClient, MexcFuturesClient])
@pytest.mark.asyncio
async def test_stock_does_not_consume_top_n_slot(client_cls):
    bases = [('MUSTOCK', 9000.), ('INTCSTOCK', 8000.), ('USDC', 7000.), ('SOL', 6000.), ('DOGE', 5000.)]
    def respond(request):
        if client_cls is MexcFuturesClient:
            payload = {'success': True, 'code': 0, 'data': [{'symbol': b + '_USDT', 'amount24': v} for b, v in bases]}
        elif request.url.path.endswith('exchangeInfo'):
            payload = {'symbols': [{'symbol': b + 'USDT', 'baseAsset': b, 'quoteAsset': 'USDT',
                                   'status': 'TRADING', 'isSpotTradingAllowed': True} for b, _ in bases]}
        else:
            payload = [{'symbol': b + 'USDT', 'quoteVolume': v} for b, v in bases]
        return httpx.Response(200, json=payload)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        rows = await client_cls(http).top_symbols(2)
    assert [symbol.replace('_', '') for symbol, _ in rows] == ['SOLUSDT', 'DOGEUSDT']


def test_saved_stock_is_not_reintroduced_by_top_selector(archive):
    state = SeniorWaveDetector().detect(archive.snapshot('BCH'), 1, 300)
    state.symbol = 'MUSTOCK_USDT'
    assert select_top_crypto([state]) == []


@pytest.mark.parametrize('symbol', ['BCH', 'XAU', 'USOIL', 'DOGE', 'ZEC'])
def test_low_column_contains_exact_depth_and_keeps_recovery_column(archive, symbol):
    state = SeniorWaveDetector().detect(archive.snapshot(symbol), 20, 300)
    values, _ = _row_values_flags(state, 1)
    depth = (Decimal(str(state.impulse_high)) - Decimal(str(state.working_low))) / (Decimal(str(state.impulse_high)) - Decimal(str(state.origin)))
    assert values[5].endswith(f' · {depth * 100:.2f}%')
    assert values[6] == state.fib_status
    assert values[5] in technical_report_text([state], heading=['v0023'], errors=[])


def test_tiny_low_keeps_nonzero_fixed_decimal_and_invalid_has_no_stale_depth():
    s = WaveState('TINY', 'mexc_futures', 'W2', 'RECOVERING', 1e-10, 9e-10, 4e-10, 1e-10,
                  retrace_depth=.625, fib_status='> .618 · 3/3 C4H')
    assert _row_values_flags(s, 1)[0][5] == '0.0000000004 · 62.50%'
    s.status = 'INVALID'
    assert '%' not in _row_values_flags(s, 1)[0][5]


@pytest.mark.parametrize('scale,shift', [(1., 0), (1e-8, -200), (1000., 150)])
def test_real_usoil_parent_changes_degree_without_changing_targets_or_using_ticker(archive, scale, shift):
    snap = deepcopy(archive.snapshot('USOIL'))
    snap.symbol = 'UNSEEN_ASSET'
    for frame in (snap.hourly_closed, snap.daily_closed):
        frame[['open','high','low','close']] *= scale
        frame.timestamp += pd.Timedelta(days=shift)
    snap.live_price *= scale
    state = SeniorWaveDetector().detect(snap, 100, 300)
    assert state.wave_type == 'W3-(2)'
    assert state.origin == pytest.approx(74.36 * scale, rel=1e-12, abs=0)
    assert state.targets == pytest.approx([t * scale for t in [122.83, 144.16336, 178.68336, 234.53672]], rel=1e-12, abs=0)
    parent = state.structure_evidence['senior_parent']
    assert parent['origin'] == pytest.approx(67.14 * scale, rel=1e-12, abs=0)
    assert parent['high'] == pytest.approx(93.41 * scale, rel=1e-12, abs=0)
    assert parent['w2_timeframe'] == '1D'  # actual archive lacks the August 5 H1 bars


def test_missing_oil_parent_is_not_replaced_by_symbol_rule(archive):
    snap = deepcopy(archive.snapshot('USOIL'))
    snap.daily_closed = snap.daily_closed[snap.daily_closed.timestamp >= '2026-08-01'].copy()
    state = SeniorWaveDetector().detect(snap, 50, 300)
    assert state.wave_type == 'W2'
    assert 'senior_parent' not in state.structure_evidence


def test_saved_v22_usoil_migrates_without_reset(archive):
    snap = archive.snapshot('USOIL')
    state = SeniorWaveDetector().detect(snap, 20, 300)
    state.wave_type = 'W2'; state.detector_version = '0022'; state.structure_evidence = {}
    result = SeniorWaveDetector().track(state, snap, 300)
    assert result.wave_type == 'W3-(2)' and result.detector_version == '0023'
    assert result.origin == 74.36


def test_w4_uses_outer_w1_not_whole_w3_and_reports_possible_truncation(archive):
    s = SeniorWaveDetector().detect(archive.snapshot('ZEC'), 5, 300)
    expected = [Decimal('1271.09') + m * (Decimal('544.28') - Decimal('250.12')) for m in map(Decimal, ['.618','1','1.618'])]
    assert s.targets == pytest.approx(list(map(float, expected)))
    assert s.retrace_depth == pytest.approx((1698 - 1271.09) / (1698 - 368.03))
    assert s.structure_evidence['mature_impulse']['w5_below_w3_high'] == [1, 2]
    assert 'W5 scenario' in s.target_source and _targets(s).startswith('W5: ')
    assert s.status != 'PHASE_UNCERTAIN'


def test_v22_w4_state_gets_calculations_even_without_new_four_hour_bar(archive):
    snap = archive.snapshot('ZEC')
    state = SeniorWaveDetector().detect(snap, 5, 300)
    state.detector_version = '0022'; state.status = 'PHASE_UNCERTAIN'
    SeniorWaveDetector._clear_derived(state, fib_status='СТАДИЯ ТРЕБУЕТ ПРОВЕРКИ')
    result = SeniorWaveDetector().track(WaveState.from_dict(state.to_dict()), snap, 300)
    assert result.wave_type == 'W4' and result.targets == pytest.approx([1452.88088,1565.25,1747.04088])
    assert result.detector_version == '0023' and result.retrace_depth > 0


def test_live_price_above_w3_does_not_promote_without_complete4h(archive):
    snap = deepcopy(archive.snapshot('ZEC'))
    state = SeniorWaveDetector().detect(snap, 5, 300)
    snap.live_price = 1800.
    result = SeniorWaveDetector().track(state, snap, 300)
    assert result.wave_type == 'W4'
    assert 'next_high_accepted_at' not in result.structure_evidence['mature_impulse']


def test_w5_locks_low_and_invalidates_on_later_origin_break(archive):
    snap = archive.snapshot('ZEC'); detector = SeniorWaveDetector()
    state = detector.detect(snap, 5, 300)
    accepted = append_hours(snap, [1701.,1702.,1703.])
    state = detector.track(state, accepted, 300)
    targets = state.targets.copy()
    later = append_hours(accepted, [1650.] * 4)
    state = detector.track(state, later, 300)
    assert state.wave_type == 'W5' and state.working_low == 1271.09 and state.targets == targets
    assert state.base_zone is None and state.deep_zone is None
    later.live_low = 1270.
    state = detector.track(state, later, 300)
    assert state.status == 'RECOUNT' and not state.targets
    later.live_low = 1600.
    assert detector.track(state, later, 300).status == 'RECOUNT'


def test_w5_acceptance_then_origin_break_in_single_batch_is_not_lost(archive):
    snap = archive.snapshot('ZEC'); detector = SeniorWaveDetector()
    state = detector.detect(snap, 5, 300)
    batch = append_hours(snap, [1701.,1702.,1703.,1260.,1300.,1500.,1700.])
    result = detector.track(state, batch, 300)
    assert result.status == 'RECOUNT' and not result.targets


def test_w4_malformed_saved_parent_does_not_publish_targets(archive):
    snap = archive.snapshot('ZEC'); detector = SeniorWaveDetector()
    state = detector.detect(snap, 5, 300)
    state.structure_evidence['mature_impulse']['parent'].pop('origin')
    assert detector.track(state, snap, 300).status == 'RECOUNT'
    assert not state.targets


# Synthetic API candles; these test transport/identity and do not claim live MEXC evidence.
def daily(start, end, price=1.5):
    return pd.DataFrame({'timestamp': pd.date_range(start, end, freq='D', tz='UTC', inclusive='left'),
                         'open': price, 'high': price * 1.05, 'low': price * .95, 'close': price, 'volume': 10.})


class UnavailableFutures:
    name = 'mexc_futures'
    async def candles(self, *args):
        raise httpx.ConnectError('delisted predecessor')


class SpotFixture:
    name = 'mexc_spot'
    def __init__(self, defect=None):
        self.calls = []; self.defect = defect
    async def candles(self, symbol, timeframe, start, end):
        self.calls.append(symbol)
        if self.defect == 'cancel':
            raise asyncio.CancelledError()
        if symbol == 'GRAMUSDT':
            frame = daily('2026-06-15', '2026-07-25', 2. if self.defect == 'basis' else 1.5)
            if self.defect == 'empty':
                frame = frame.iloc[:0]
        else:
            frame = daily('2026-06-01', '2026-06-15')
            if self.defect == 'gap':
                frame = frame.drop(4)
            elif self.defect == 'duplicate':
                frame = pd.concat([frame, frame.tail(1)])
            elif self.defect == 'truncated':
                frame = frame.iloc[:-1]
        return frame, None, None


@pytest.mark.asyncio
async def test_futures_outage_uses_spot_context_without_mutating_futures():
    current = daily('2026-06-15', '2026-07-25'); before = current.copy(deep=True); spot = SpotFixture()
    context, info = await load_daily_context(UnavailableFutures(), 'GRAM_USDT', current,
        pd.Timestamp('2026-06-01T00:00Z'), pd.Timestamp('2026-07-25T00:00Z'), spot_clients=[spot])
    assert info['status'] == 'restored_spot' and info['spot_exchange'] == 'mexc_spot'
    assert spot.calls == ['GRAMUSDT', 'TONUSDT']
    assert context.source_exchange.eq('mexc_spot').all()
    pd.testing.assert_frame_equal(current, before)


@pytest.mark.parametrize('defect', ['gap', 'duplicate', 'truncated', 'basis', 'empty'])
@pytest.mark.asyncio
async def test_bad_spot_context_remains_explicitly_unavailable(defect):
    context, info = await load_daily_context(UnavailableFutures(), 'GRAM_USDT', daily('2026-06-15','2026-07-25'),
        pd.Timestamp('2026-06-01T00:00Z'), pd.Timestamp('2026-07-25T00:00Z'), spot_clients=[SpotFixture(defect)])
    assert context is None and info['status'] == 'unavailable' and len(info['attempts']) == 2


@pytest.mark.asyncio
async def test_reset_cancellation_is_not_swallowed_in_spot_fallback():
    with pytest.raises(asyncio.CancelledError):
        await load_daily_context(UnavailableFutures(), 'GRAM_USDT', daily('2026-06-15','2026-07-25'),
            pd.Timestamp('2026-06-01T00:00Z'), pd.Timestamp('2026-07-25T00:00Z'), spot_clients=[SpotFixture('cancel')])


@pytest.mark.asyncio
async def test_native_futures_history_takes_precedence_over_spot():
    class Native:
        name = 'mexc_futures'
        async def candles(self, *args):
            return daily('2026-06-01','2026-06-15'), None, None
    spot = SpotFixture('cancel')
    context, info = await load_daily_context(Native(), 'GRAM_USDT', daily('2026-06-15','2026-07-25'),
        pd.Timestamp('2026-06-01T00:00Z'), pd.Timestamp('2026-07-25T00:00Z'), spot_clients=[spot])
    assert context is not None and info['status'] == 'restored' and spot.calls == []


@pytest.mark.asyncio
async def test_binance_spot_is_attempted_after_mexc_spot_failure(monkeypatch):
    async def prefix(*args):
        return daily('2026-06-01','2026-06-30'), []
    monkeypatch.setattr('data_lineage.binance_predecessor_daily', prefix)
    class Binance:
        name = 'binance_spot'
        async def candles(self, *args):
            return daily('2026-07-02','2026-07-25'), None, None
    context, info = await load_daily_context(UnavailableFutures(), 'GRAM_USDT', daily('2026-06-15','2026-07-25'),
        pd.Timestamp('2026-06-01T00:00Z'), pd.Timestamp('2026-07-25T00:00Z'), spot_clients=[SpotFixture('gap'), Binance()])
    assert info['status'] == 'restored_spot' and info['spot_exchange'] == 'binance_spot'
    assert len(info['attempts']) == 2 and context.source_exchange.eq('binance_spot').all()


def test_real_spot_ancestry_on_synthetic_futures_does_not_copy_spot_targets(archive):
    # Real Binance candles provide context; 0.998 scaling is a SYNTHETIC futures basis.
    snap = deepcopy(archive.snapshot('GRAM'))
    context = snap.daily_context.copy(deep=True)
    snap.exchange = 'mexc_futures'; snap.symbol = 'GRAM_USDT'
    for frame in (snap.hourly_closed, snap.daily_closed):
        frame[['open','high','low','close']] *= .998
    snap.live_price *= .998
    snap.history_evidence = {'status': 'restored_spot', 'spot_exchange': 'binance_spot'}
    state = SeniorWaveDetector().detect(snap, 30, 300)
    assert state.wave_type == 'W3-(2)'
    assert state.targets == pytest.approx([t * .998 for t in [1.914,2.194572,2.648572,3.383144]])
    assert state.target_origin == pytest.approx(1.286 * .998)
    assert state.structure_evidence['daily_parent']['context_market'] == 'binance_spot'
    assert 'спот-контекст' in _row_values_flags(state, 1)[0][4]
    pd.testing.assert_frame_equal(context, snap.daily_context)


def test_spot_identity_mismatch_is_rejected(archive):
    snap = archive.snapshot('GRAM'); context = snap.daily_context.copy()
    context.source_symbol = 'UNRELATED'
    with pytest.raises(DataIntegrityError, match='ticker mismatch'):
        validate_spot_context(context, snap.daily_closed, TRANSITIONS['binance_spot'], archive.asof)


def kline_rows(frame):
    return [[int(row.timestamp.timestamp() * 1000), row.open, row.high, row.low, row.close, row.volume,
             int((row.timestamp + pd.Timedelta(days=1)).timestamp() * 1000), 100.] for row in frame.itertuples()]


@pytest.mark.asyncio
async def test_mexc_spot_daily_paginates_at_500_and_shares_retry_client():
    start = pd.Timestamp('2025-01-01T00:00Z'); end = start + pd.Timedelta(days=501)
    rows = kline_rows(daily(start.tz_localize(None), end.tz_localize(None)))
    calls = []
    def respond(request):
        calls.append(request)
        assert request.url.params['limit'] == '500'
        if len(calls) == 1:
            return httpx.Response(503)
        cursor = int(request.url.params['startTime'])
        return httpx.Response(200, json=[row for row in rows if row[0] >= cursor][:500])
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result, _, _ = await MexcSpotHistoryClient(http, retry_base_delay=0).candles('TONUSDT','1d',start,end)
        assert not http.is_closed
    assert len(result) == 501 and len(calls) == 3


@pytest.mark.parametrize('defect', ['duplicate','gap','close_time','html','payload','outside','stale_order','unaligned'])
@pytest.mark.asyncio
async def test_mexc_spot_malformed_responses_are_rejected(defect):
    start = pd.Timestamp('2026-05-01T00:00Z'); end = pd.Timestamp('2026-05-05T00:00Z')
    rows = kline_rows(daily('2026-05-01','2026-05-05'))
    if defect == 'duplicate': rows.append(rows[-1])
    if defect == 'gap': rows.pop(1)
    if defect == 'close_time': rows[0][6] -= 5000
    if defect == 'outside': rows[-1][0] += 86400000
    if defect == 'stale_order': rows.reverse()
    if defect == 'unaligned': rows[0][0] += 3600000
    def respond(request):
        if defect == 'html': return httpx.Response(200, text='<html>Site Unavailable</html>')
        return httpx.Response(200, json={'code': -1} if defect == 'payload' else rows)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises((ValueError, RuntimeError)):
            await MexcSpotHistoryClient(http, retry_base_delay=0).candles('TONUSDT','1d',start,end)


@pytest.mark.asyncio
async def test_track_does_not_fetch_legacy_stock_or_destroy_saved_session(cfg, repo):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from conftest import state
    from services_scanner import ScannerService
    old = state('MUSTOCK_USDT')
    sid = await repo.replace_active_session('mexc_futures', 300, [old])
    data = SimpleNamespace(snapshot=AsyncMock(side_effect=AssertionError('stock fetched')))
    result = await ScannerService(cfg, repo, data).track()
    assert not result.states and not result.errors
    data.snapshot.assert_not_called()
    assert (await repo.tracked_states(sid))[0].to_dict() == old.to_dict()


def test_future_spot_context_does_not_change_historical_detection(archive):
    snap = deepcopy(archive.snapshot('GRAM'))
    snap.exchange = 'mexc_futures'; snap.symbol = 'GRAM_USDT'
    snap.history_evidence = {'status': 'restored_spot', 'spot_exchange': 'binance_spot'}
    detector = SeniorWaveDetector()
    before = detector.detect(snap, 30, 300)
    future = snap.daily_context.tail(1).copy()
    future.timestamp += pd.Timedelta(days=10)
    future[['open','high','low','close']] = .01
    snap.daily_context = pd.concat([snap.daily_context, future], ignore_index=True)
    after = detector.detect(snap, 30, 300)
    assert after.wave_type == before.wave_type == 'W3-(2)'
    assert after.targets == before.targets and after.origin == before.origin


def test_spot_parent_with_different_low_day_cannot_force_futures_w3_2(archive):
    snap = deepcopy(archive.snapshot('GRAM'))
    snap.exchange = 'mexc_futures'; snap.symbol = 'GRAM_USDT'
    snap.history_evidence = {'status': 'restored_spot', 'spot_exchange': 'binance_spot'}
    # Different market makes its correction low one day earlier; do not remap it.
    snap.daily_context.loc[snap.daily_context.timestamp.eq(pd.Timestamp('2026-09-15T00:00Z')), 'low'] = 1.28
    state = SeniorWaveDetector().detect(snap, 30, 300)
    assert state.wave_type == 'W2' and 'daily_parent' not in state.structure_evidence
