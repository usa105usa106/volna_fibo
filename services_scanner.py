from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import math
import time

import pandas as pd

from config import Settings
from core_models import MarketSnapshot, WaveState
from core_ranking import select_top_crypto
from core_senior import SeniorWaveDetector, control_no_setup, data_incomplete_state, no_setup_state
from core_symbols import WALK_MAJOR_BASES, display_symbol, normalize_symbol
from data_collector import MarketDataService
from data_exchanges import ControlUnavailable
from data_integrity import DataIntegrityError
from db_repository import Repository
from services_tasks import gather_owned, run_cpu


log = logging.getLogger(__name__)


class SearchIncompleteError(RuntimeError):
    pass


@dataclass(slots=True)
class RunResult:
    mode: str
    states: list[WaveState]
    checked: int
    found_crypto: int
    errors: list[str]
    exchange: str
    top_n: int
    requested_symbols: list[str] | None = None
    skipped_controls: list[str] = field(default_factory=list)
    successful_crypto_snapshots: int = 0
    analysis_seconds: float = 0.0


@dataclass(slots=True)
class WalkSignalRecord:
    symbol: str
    structure_id: str
    first_seen: str
    last_seen: str
    observations: int
    wave_type: str
    state_status: str
    rating: float
    entry: float
    origin: float | None
    impulse_high: float | None
    working_low: float | None
    strict_origin: float
    parent_w2_low: float | None
    w3_1_high: float | None
    retrace_depth: float | None
    fib_status: str
    fibs: dict[str, float]
    targets: list[float]
    base_zone: tuple[float, float] | None
    deep_zone: tuple[float, float] | None
    t1: float
    outcome: str
    outcome_at: str | None
    hours_to_outcome: float | None
    mfe_to_resolution_pct: float | None
    mae_to_resolution_pct: float | None
    mfe_30d_pct: float | None
    mae_30d_pct: float | None
    growth_from_low_pct: float | None
    strict_distance_pct: float | None
    liquidity_rank: int | None
    last_complete4h_bucket: str | None


@dataclass(slots=True)
class WalkExcludedRecord:
    symbol: str
    structure_id: str
    first_seen: str
    wave_type: str
    rating: float
    entry: float
    t1: float | None
    reason: str


@dataclass(slots=True)
class WalkAssetReport:
    symbol: str
    tested: bool
    error: str | None = None
    signals: list[WalkSignalRecord] = field(default_factory=list)
    excluded: list[WalkExcludedRecord] = field(default_factory=list)
    checkpoints_evaluated: int = 0
    qualifying_observations: int = 0
    duplicate_observations: int = 0


@dataclass(slots=True)
class WalkForwardResult:
    exchange: str
    top_n: int
    assets_requested: int
    assets_tested: int
    horizon_days: int
    history_days: int
    complete4h_checkpoints: int
    signals: int
    t1_first: int
    invalid_first: int
    unresolved: int
    ambiguous: int
    already_extended: int
    duplicate_observations: int
    data_errors: int
    median_mfe_pct: float | None
    median_mae_pct: float | None
    median_mfe_30d_pct: float | None
    median_mae_30d_pct: float | None
    rating_buckets: dict[str, tuple[int, int, int]]
    started_at: str
    finished_at: str
    analysis_seconds: float
    assets: list[WalkAssetReport] = field(default_factory=list)
    wave_buckets: dict[str, tuple[int, int, int, int]] = field(default_factory=dict)


class ScannerService:
    def __init__(self, cfg: Settings, repo: Repository, data: MarketDataService):
        self.cfg = cfg
        self.repo = repo
        self.data = data
        self.detector = SeniorWaveDetector()
        self.lock = asyncio.Lock()
        self._workers = asyncio.Semaphore(cfg.http_concurrency)
        self._cpu = asyncio.Semaphore(1)

    async def _compute(self, call, *args):
        async with self._cpu:
            return await run_cpu(call, *args)

    async def _map(self, call, values):
        async def bounded(value):
            async with self._workers:
                return await call(value)
        return await gather_owned(*(bounded(value) for value in values))

    @property
    def busy(self) -> bool:
        return self.lock.locked()

    async def reset_runtime_state(self) -> None:
        """Drop process-local detector/network state after all active scans are cancelled."""
        self.detector = SeniorWaveDetector()
        await self.data.reset_runtime_state()

    async def search(self) -> RunResult:
        """Fresh discovery only. Does NOT mutate the tracked session until commit_search()."""
        async with self.lock:
            started_perf = time.perf_counter()
            settings = await self.repo.get_settings()
            exchange = settings.exchange
            top_n = settings.top_n

            # Critical difference from older builds: the previous tracked session remains
            # active while a new Search is calculating. It is swapped only after the new
            # report reaches Telegram and commit_search() is called by the controller.
            universe = await self.data.universe(exchange, top_n)
            if len(universe) < top_n:
                raise SearchIncompleteError(
                    f"exchange returned only {len(universe)} instruments for Top-{top_n}; previous accompaniment is preserved"
                )

            errors: list[str] = []
            found: list[WaveState] = []
            successful_snapshots = 0
            success_lock = asyncio.Lock()

            rank_map = {symbol: idx + 1 for idx, (symbol, _) in enumerate(universe)}
            qv_map = dict(universe)

            async def one(symbol: str):
                nonlocal successful_snapshots
                try:
                    snap = await self.data.snapshot(exchange, symbol, qv_map.get(symbol, 0.0))
                    state = await self._compute(self.detector.detect, snap, rank_map.get(symbol), top_n)
                    async with success_lock:
                        successful_snapshots += 1
                    if state and (state.rating or 0) >= self.cfg.min_rating:
                        return state
                except DataIntegrityError as exc:
                    log.warning("DATA INCOMPLETE for %s: %s", symbol, exc)
                    errors.append(f"{symbol}: DATA_INCOMPLETE")
                except Exception as exc:  # isolate one bad market
                    log.exception("search failed for %s", symbol)
                    errors.append(f"{symbol}: {type(exc).__name__}")
                return None

            rows = await self._map(one, [symbol for symbol, _ in universe])
            found.extend([s for s in rows if s is not None])

            required = max(1, math.ceil(len(universe) * self.cfg.search_min_success_fraction))
            if successful_snapshots < required:
                raise SearchIncompleteError(
                    f"fresh scan rejected: only {successful_snapshots}/{len(universe)} crypto histories passed data checks; "
                    f"need at least {required}. Previous accompaniment is preserved."
                )

            found = select_top_crypto(found)

            # XAU/USOIL come only from the selected exchange. No Yahoo/proxy fallback.
            # If an instrument is absent, it is skipped without failing crypto discovery.
            controls: list[WaveState] = []
            skipped_controls: list[str] = []
            try:
                available_controls = await self.data.available_controls(exchange)
            except Exception as exc:
                log.exception("control discovery failed")
                available_controls = {}
                errors.append(f"CONTROLS: {type(exc).__name__}")

            for symbol in ("XAU", "USOIL"):
                if symbol not in available_controls:
                    skipped_controls.append(symbol)
                    continue
                try:
                    snap = await self.data.snapshot(exchange, symbol, 0.0)
                    state = await self._compute(self.detector.detect, snap, None, top_n)
                    if state:
                        state.is_control = True
                        controls.append(state)
                    else:
                        controls.append(control_no_setup(symbol, exchange, snap.live_price))
                except ControlUnavailable:
                    skipped_controls.append(symbol)
                except DataIntegrityError as exc:
                    log.warning("control DATA INCOMPLETE for %s: %s", symbol, exc)
                    errors.append(f"{symbol}: DATA_INCOMPLETE")
                    controls.append(data_incomplete_state(symbol, exchange, None, is_control=True, event=str(exc)))
                except Exception as exc:
                    log.exception("control failed for %s", symbol)
                    errors.append(f"{symbol}: {type(exc).__name__}")
                    # A control failure never breaks the main search and never fabricates data.
                    skipped_controls.append(symbol)

            states = found + controls
            return RunResult(
                "search",
                states,
                successful_snapshots + len(controls),
                len(found),
                errors,
                exchange,
                top_n,
                skipped_controls=skipped_controls,
                successful_crypto_snapshots=successful_snapshots,
                analysis_seconds=time.perf_counter() - started_perf,
            )

    async def commit_search(self, result: RunResult, completed_at: str | None = None) -> int:
        """Atomically replace accompaniment only after a Search report was delivered."""
        if result.mode != "search":
            raise ValueError("only search results can become an active tracking session")
        return await self.repo.replace_active_session(
            result.exchange, result.top_n, result.states,
            completed_at=completed_at or datetime.now(timezone.utc).isoformat(),
        )

    async def track(self) -> RunResult:
        async with self.lock:
            started_perf = time.perf_counter()
            session = await self.repo.active_session()
            if session is None:
                return RunResult("track", [], 0, 0, ["NO_ACTIVE_SEARCH_SESSION"], "", 0, analysis_seconds=time.perf_counter() - started_perf)
            session_id, exchange, top_n = session
            previous = await self.repo.tracked_states(session_id)
            errors: list[str] = []
            skipped_controls: list[str] = []

            async def one(state: WaveState) -> tuple[WaveState | None, WaveState | None]:
                """Return (state_for_report, state_safe_to_persist).

                Bad/incomplete fresh candles must never overwrite the last confirmed
                accompaniment state. The report still gets an explicit diagnostic row.
                """
                try:
                    snap = await self.data.snapshot(exchange, state.symbol, 0.0)
                    if state.last_complete4h_bucket:
                        since = pd.Timestamp(state.last_complete4h_bucket) + pd.Timedelta(hours=4)
                        if since.tzinfo is None:
                            since = since.tz_localize("UTC")
                        if snap.hourly_closed.empty or pd.to_datetime(snap.hourly_closed["timestamp"], utc=True).min() > since:
                            raise DataIntegrityError("tracking history does not cover the last confirmed state")
                    updated = await self._compute(self.detector.track, state, snap, top_n)
                    return updated, updated
                except ControlUnavailable:
                    if state.is_control:
                        skipped_controls.append(display_symbol(state.symbol))
                        return None, None
                    raise
                except DataIntegrityError as exc:
                    log.warning("track DATA INCOMPLETE for %s: %s", state.symbol, exc)
                    errors.append(f"{state.symbol}: DATA_INCOMPLETE")
                    shown = WaveState.from_dict(state.to_dict())
                    shown.status = "DATA_INCOMPLETE"
                    shown.rating = None
                    shown.current_price = None
                    shown.growth_from_low_pct = None
                    shown.fib_status = "DATA INCOMPLETE · previous senior-state preserved"
                    shown.last_event = f"DATA INCOMPLETE: {exc}"
                    return shown, None
                except Exception as exc:
                    log.exception("track failed for %s", state.symbol)
                    errors.append(f"{state.symbol}: {type(exc).__name__}")
                    shown = WaveState.from_dict(state.to_dict())
                    shown.status = "DATA_INCOMPLETE"
                    shown.rating = None
                    shown.current_price = None
                    shown.growth_from_low_pct = None
                    shown.fib_status = f"DATA ERROR · {type(exc).__name__} · previous senior-state preserved"
                    shown.last_event = f"DATA ERROR: {type(exc).__name__}"
                    return shown, None

            gathered = list(await self._map(one, previous))
            shown_states = [shown for shown, _ in gathered if shown is not None]
            persistable = [saved for _, saved in gathered if saved is not None]
            # Only successfully updated states are persisted. Diagnostic copies from bad
            # fresh data are report-only, so the previous confirmed DB state survives.
            await self.repo.update_states(session_id, persistable)
            return RunResult(
                "track",
                shown_states,
                len(shown_states),
                sum(1 for s in shown_states if not s.is_control),
                errors,
                exchange,
                top_n,
                skipped_controls=skipped_controls,
                analysis_seconds=time.perf_counter() - started_perf,
            )

    async def analyze_symbols(self, raw_symbols: list[str]) -> RunResult:
        """One-off fresh analysis of exactly the user-supplied symbols. No state mutation."""
        async with self.lock:
            started_perf = time.perf_counter()
            settings = await self.repo.get_settings()
            exchange = settings.exchange
            top_n = settings.top_n

            symbols: list[str] = []
            errors: list[str] = []
            for raw in raw_symbols:
                try:
                    symbol = normalize_symbol(exchange, raw)
                except ValueError as exc:
                    errors.append(str(exc))
                    continue
                if symbol not in symbols:
                    symbols.append(symbol)

            if not symbols:
                return RunResult("manual", [], 0, 0, errors or ["NO_SYMBOLS"], exchange, top_n, [], analysis_seconds=time.perf_counter() - started_perf)

            try:
                universe = await self.data.universe(exchange, top_n)
            except Exception as exc:
                log.exception("manual liquidity universe failed")
                universe = []
                errors.append(f"LIQUIDITY: {type(exc).__name__}")
            rank_map = {symbol: idx + 1 for idx, (symbol, _) in enumerate(universe)}
            qv_map = dict(universe)

            async def one(symbol: str) -> WaveState:
                is_control = symbol in {"XAU", "USOIL"}
                try:
                    snap = await self.data.snapshot(exchange, symbol, qv_map.get(symbol, 0.0))
                    rank = None if is_control else rank_map.get(symbol, top_n + 1)
                    state = await self._compute(self.detector.detect, snap, rank, top_n)
                    if state:
                        state.is_control = is_control
                        return state
                    return no_setup_state(symbol, exchange, snap.live_price, is_control=is_control)
                except ControlUnavailable:
                    errors.append(f"{symbol}: UNAVAILABLE_ON_EXCHANGE")
                    return data_incomplete_state(symbol, exchange, None, is_control=is_control, event="UNAVAILABLE ON SELECTED EXCHANGE")
                except DataIntegrityError as exc:
                    errors.append(f"{symbol}: DATA_INCOMPLETE")
                    return data_incomplete_state(symbol, exchange, None, is_control=is_control, event=str(exc))
                except Exception as exc:
                    log.exception("manual analysis failed for %s", symbol)
                    errors.append(f"{symbol}: {type(exc).__name__}")
                    return data_incomplete_state(symbol, exchange, None, is_control=is_control, event=f"DATA ERROR: {type(exc).__name__}")

            states = list(await self._map(one, symbols))
            return RunResult(
                "manual",
                states,
                len(symbols),
                sum(1 for s in states if not s.is_control and s.status not in {"NO_SETUP", "DATA_INCOMPLETE"}),
                errors,
                exchange,
                top_n,
                [display_symbol(s) for s in symbols],
                analysis_seconds=time.perf_counter() - started_perf,
            )

    async def walk_forward(self) -> WalkForwardResult:
        """Detailed fixed-10-major no-look-ahead detector diagnostic.

        The diagnostic is intentionally isolated from production Search/Tracking state.
        It evaluates every completed UTC 4H boundary across the historical diagnostic
        window, records each senior structure only once when it first becomes a fresh
        qualifying setup, and keeps future candles exclusively for outcome scoring.
        """
        async with self.lock:
            started = datetime.now(timezone.utc)
            started_perf = time.perf_counter()
            settings = await self.repo.get_settings()
            exchange = settings.exchange

            diagnostic_top_n = 300
            try:
                liquidity_universe = await self.data.universe(exchange, diagnostic_top_n)
            except Exception:
                log.exception("/walk liquidity universe failed; majors will use fallback ranks")
                liquidity_universe = []
            rank_map = {symbol: idx + 1 for idx, (symbol, _) in enumerate(liquidity_universe)}
            qv_map = dict(liquidity_universe)
            symbols = [normalize_symbol(exchange, base) for base in WALK_MAJOR_BASES]

            horizon = self.cfg.walk_horizon_days
            # Preserve the old 6 x 30d knobs as a simple 180d default history window,
            # while evaluating every COMPLETE4H instead of only six monthly snapshots.
            history_days = self.cfg.walk_history_days
            now = pd.Timestamp.now(tz="UTC").floor("h")
            latest_checkpoint = now - pd.Timedelta(days=horizon)
            earliest_checkpoint = latest_checkpoint - pd.Timedelta(days=history_days)
            history_span_days = max(0, math.ceil((now - earliest_checkpoint).total_seconds() / 86400))
            h1_days = history_span_days + self.cfg.lookback_1h_days + 3
            d1_days = history_span_days + self.cfg.lookback_1d_days + 3

            signals = t1_first = invalid_first = unresolved = ambiguous = 0
            already_extended = duplicate_observations = 0
            tested_assets = data_errors = complete4h_checkpoints = 0
            resolution_mfe_values: list[float] = []
            resolution_mae_values: list[float] = []
            full_mfe_values: list[float] = []
            full_mae_values: list[float] = []
            buckets: dict[str, list[int]] = {
                "9.0+": [0, 0, 0],
                "8.0–8.9": [0, 0, 0],
                f"{self.cfg.min_rating:.1f}–7.9": [0, 0, 0],
            }
            # wave -> [signals, t1, invalid, unresolved_or_ambiguous]
            wave_buckets: dict[str, list[int]] = {
                "W2": [0, 0, 0, 0],
                "W3-(2)": [0, 0, 0, 0],
            }
            asset_reports: dict[str, WalkAssetReport] = {
                display_symbol(symbol): WalkAssetReport(display_symbol(symbol), False)
                for symbol in symbols
            }
            aggregate_lock = asyncio.Lock()

            def rating_bucket(rating: float) -> str:
                if rating >= 9.0:
                    return "9.0+"
                if rating >= 8.0:
                    return "8.0–8.9"
                return f"{self.cfg.min_rating:.1f}–7.9"

            def _price_key(value: float | None) -> str:
                return "—" if value is None else f"{float(value):.12g}"

            def structure_key(state: WaveState) -> str:
                """Stable senior identity; re-observing the same living wave is not a new signal."""
                if state.wave_type == "W3-(2)":
                    left = state.parent_w2_ts or _price_key(state.parent_w2_low or state.strict_origin)
                    right = state.w3_1_high_ts or _price_key(state.w3_1_high or state.impulse_high)
                else:
                    left = state.impulse_start_ts or _price_key(state.origin or state.strict_origin)
                    right = state.impulse_high_ts or _price_key(state.impulse_high)
                return "|".join(
                    [
                        state.wave_type,
                        left,
                        right,
                        _price_key(state.strict_origin),
                    ]
                )

            async def one_asset(symbol: str):
                nonlocal signals, t1_first, invalid_first, unresolved, ambiguous
                nonlocal already_extended, duplicate_observations, tested_assets, data_errors, complete4h_checkpoints
                shown_symbol = display_symbol(symbol)
                local_signals = local_t1 = local_invalid = local_unresolved = local_ambiguous = 0
                local_extended = local_duplicates = local_qualifying = local_checkpoints = 0
                local_resolution_mfe: list[float] = []
                local_resolution_mae: list[float] = []
                local_full_mfe: list[float] = []
                local_full_mae: list[float] = []
                local_buckets = {k: [0, 0, 0] for k in buckets}
                local_wave_buckets = {k: [0, 0, 0, 0] for k in wave_buckets}
                local_records: list[WalkSignalRecord] = []
                local_excluded: list[WalkExcludedRecord] = []
                try:
                    full = await self.data.walk_history(
                        exchange,
                        symbol,
                        qv_map.get(symbol, 0.0),
                        h1_days=h1_days,
                        d1_days=d1_days,
                    )
                except Exception as exc:
                    log.exception("/walk data failed for %s", symbol)
                    async with aggregate_lock:
                        data_errors += 1
                        asset_reports[shown_symbol] = WalkAssetReport(
                            shown_symbol,
                            False,
                            f"{type(exc).__name__}: {exc}",
                        )
                    return

                def evaluate_history():
                    nonlocal local_signals, local_t1, local_invalid, local_unresolved, local_ambiguous
                    nonlocal local_extended, local_duplicates, local_qualifying, local_checkpoints
                    h1_all = full.hourly_closed.copy()
                    d1_all = full.daily_closed.copy()
                    h1_all["timestamp"] = pd.to_datetime(h1_all["timestamp"], utc=True)
                    d1_all["timestamp"] = pd.to_datetime(d1_all["timestamp"], utc=True)
                    h1_all = h1_all.sort_values("timestamp").reset_index(drop=True)
                    d1_all = d1_all.sort_values("timestamp").reset_index(drop=True)

                    h1_end = h1_all["timestamp"] + pd.Timedelta(hours=1)
                    checkpoint_mask = (
                        (h1_end >= earliest_checkpoint)
                        & (h1_end <= latest_checkpoint)
                        & (h1_end.dt.minute == 0)
                        & (h1_end.dt.second == 0)
                        & ((h1_end.dt.hour % 4) == 0)
                    )
                    checkpoints = list(pd.DatetimeIndex(h1_end[checkpoint_mask]).unique().sort_values())
                    local_checkpoints = len(checkpoints)

                    # key -> fresh record index, or None when first qualifying observation
                    # was already extended and intentionally excluded.
                    seen: dict[str, int | None] = {}

                    for checkpoint in checkpoints:
                        h1_hist = h1_all[(h1_all["timestamp"] + pd.Timedelta(hours=1)) <= checkpoint].copy()
                        d1_hist = d1_all[(d1_all["timestamp"] + pd.Timedelta(days=1)) <= checkpoint].copy()
                        if h1_hist.empty or d1_hist.empty:
                            continue
                        snap = MarketSnapshot(
                            symbol=symbol,
                            exchange=exchange,
                            quote_volume=qv_map.get(symbol, 0.0),
                            live_price=float(h1_hist["close"].iloc[-1]),
                            live_low=float(h1_hist["low"].iloc[-1]),
                            hourly_closed=h1_hist.tail(self.cfg.lookback_1h_days * 24 + 48).copy(),
                            daily_closed=d1_hist.tail(self.cfg.lookback_1d_days + 5).copy(),
                        )
                        rank = rank_map.get(symbol, diagnostic_top_n + 1)
                        state = self.detector.detect(snap, rank, diagnostic_top_n)
                        if state is None or (state.rating or 0.0) < self.cfg.min_rating:
                            continue
                        if not state.targets or state.strict_origin is None or state.current_price is None:
                            continue

                        local_qualifying += 1
                        key = structure_key(state)
                        if key in seen:
                            local_duplicates += 1
                            record_index = seen[key]
                            if record_index is not None:
                                rec = local_records[record_index]
                                rec.observations += 1
                                rec.last_seen = checkpoint.isoformat()
                            continue

                        entry = float(state.current_price)
                        t1 = float(state.targets[0])
                        seen[key] = None
                        if state.status == "EXTENDED" or entry >= t1:
                            local_extended += 1
                            local_excluded.append(
                                WalkExcludedRecord(
                                    symbol=shown_symbol,
                                    structure_id=key,
                                    first_seen=checkpoint.isoformat(),
                                    wave_type=state.wave_type,
                                    rating=float(state.rating or 0.0),
                                    entry=entry,
                                    t1=t1,
                                    reason="ALREADY_T1/EXTENDED_AT_FIRST_QUALIFYING_OBSERVATION",
                                )
                            )
                            continue

                        bucket = rating_bucket(float(state.rating or 0.0))
                        wave = state.wave_type if state.wave_type in local_wave_buckets else "W2"
                        local_signals += 1
                        local_buckets[bucket][0] += 1
                        local_wave_buckets[wave][0] += 1

                        future_end = checkpoint + pd.Timedelta(days=horizon)
                        future = h1_all[(h1_all["timestamp"] >= checkpoint) & (h1_all["timestamp"] < future_end)].copy()
                        strict = float(state.strict_origin)
                        outcome = "unresolved"
                        outcome_at: str | None = None
                        hours_to_outcome: float | None = None
                        resolution_highs: list[float] = []
                        resolution_lows: list[float] = []
                        mfe_to_resolution: float | None = None
                        mae_to_resolution: float | None = None
                        mfe_30d: float | None = None
                        mae_30d: float | None = None

                        if not future.empty:
                            mfe_30d = (float(future["high"].max()) / entry - 1.0) * 100.0
                            mae_30d = (float(future["low"].min()) / entry - 1.0) * 100.0
                            local_full_mfe.append(mfe_30d)
                            local_full_mae.append(mae_30d)
                            for row in future.itertuples(index=False):
                                resolution_highs.append(float(row.high))
                                resolution_lows.append(float(row.low))
                                hit_t1 = float(row.high) >= t1
                                hit_invalid = float(row.low) < strict
                                if hit_t1 and hit_invalid:
                                    outcome = "ambiguous"
                                    outcome_at = pd.Timestamp(row.timestamp).isoformat()
                                    break
                                if hit_t1:
                                    outcome = "t1"
                                    outcome_at = pd.Timestamp(row.timestamp).isoformat()
                                    break
                                if hit_invalid:
                                    outcome = "invalid"
                                    outcome_at = pd.Timestamp(row.timestamp).isoformat()
                                    break
                            if resolution_highs:
                                mfe_to_resolution = (max(resolution_highs) / entry - 1.0) * 100.0
                                mae_to_resolution = (min(resolution_lows) / entry - 1.0) * 100.0
                                local_resolution_mfe.append(mfe_to_resolution)
                                local_resolution_mae.append(mae_to_resolution)
                            if outcome_at is not None:
                                hours_to_outcome = max(
                                    0.0,
                                    (pd.Timestamp(outcome_at) - checkpoint).total_seconds() / 3600.0,
                                )

                        if outcome == "t1":
                            local_t1 += 1
                            local_buckets[bucket][1] += 1
                            local_wave_buckets[wave][1] += 1
                        elif outcome == "invalid":
                            local_invalid += 1
                            local_buckets[bucket][2] += 1
                            local_wave_buckets[wave][2] += 1
                        elif outcome == "ambiguous":
                            local_ambiguous += 1
                            local_wave_buckets[wave][3] += 1
                        else:
                            local_unresolved += 1
                            local_wave_buckets[wave][3] += 1

                        record = WalkSignalRecord(
                            symbol=shown_symbol,
                            structure_id=key,
                            first_seen=checkpoint.isoformat(),
                            last_seen=checkpoint.isoformat(),
                            observations=1,
                            wave_type=state.wave_type,
                            state_status=state.status,
                            rating=float(state.rating or 0.0),
                            entry=entry,
                            origin=state.origin,
                            impulse_high=state.impulse_high,
                            working_low=state.working_low,
                            strict_origin=strict,
                            parent_w2_low=state.parent_w2_low,
                            w3_1_high=state.w3_1_high,
                            retrace_depth=state.retrace_depth,
                            fib_status=state.fib_status,
                            fibs=dict(state.fibs),
                            targets=list(state.targets),
                            base_zone=state.base_zone,
                            deep_zone=state.deep_zone,
                            t1=t1,
                            outcome=outcome,
                            outcome_at=outcome_at,
                            hours_to_outcome=hours_to_outcome,
                            mfe_to_resolution_pct=mfe_to_resolution,
                            mae_to_resolution_pct=mae_to_resolution,
                            mfe_30d_pct=mfe_30d,
                            mae_30d_pct=mae_30d,
                            growth_from_low_pct=state.growth_from_low_pct,
                            strict_distance_pct=state.strict_distance_pct,
                            liquidity_rank=rank,
                            last_complete4h_bucket=state.last_complete4h_bucket,
                        )
                        local_records.append(record)
                        seen[key] = len(local_records) - 1

                await self._compute(evaluate_history)

                async with aggregate_lock:
                    tested_assets += 1
                    complete4h_checkpoints = max(complete4h_checkpoints, local_checkpoints)
                    signals += local_signals
                    t1_first += local_t1
                    invalid_first += local_invalid
                    unresolved += local_unresolved
                    ambiguous += local_ambiguous
                    already_extended += local_extended
                    duplicate_observations += local_duplicates
                    resolution_mfe_values.extend(local_resolution_mfe)
                    resolution_mae_values.extend(local_resolution_mae)
                    full_mfe_values.extend(local_full_mfe)
                    full_mae_values.extend(local_full_mae)
                    for key, vals in local_buckets.items():
                        for i in range(3):
                            buckets[key][i] += vals[i]
                    for key, vals in local_wave_buckets.items():
                        for i in range(4):
                            wave_buckets[key][i] += vals[i]
                    asset_reports[shown_symbol] = WalkAssetReport(
                        shown_symbol,
                        True,
                        None,
                        local_records,
                        local_excluded,
                        local_checkpoints,
                        local_qualifying,
                        local_duplicates,
                    )

            await self._map(one_asset, symbols)
            finished = datetime.now(timezone.utc)
            return WalkForwardResult(
                exchange=exchange,
                top_n=len(symbols),
                assets_requested=len(symbols),
                assets_tested=tested_assets,
                horizon_days=horizon,
                history_days=history_days,
                complete4h_checkpoints=complete4h_checkpoints,
                signals=signals,
                t1_first=t1_first,
                invalid_first=invalid_first,
                unresolved=unresolved,
                ambiguous=ambiguous,
                already_extended=already_extended,
                duplicate_observations=duplicate_observations,
                data_errors=data_errors,
                median_mfe_pct=float(pd.Series(resolution_mfe_values).median()) if resolution_mfe_values else None,
                median_mae_pct=float(pd.Series(resolution_mae_values).median()) if resolution_mae_values else None,
                median_mfe_30d_pct=float(pd.Series(full_mfe_values).median()) if full_mfe_values else None,
                median_mae_30d_pct=float(pd.Series(full_mae_values).median()) if full_mae_values else None,
                rating_buckets={k: tuple(v) for k, v in buckets.items()},
                started_at=started.isoformat(),
                finished_at=finished.isoformat(),
                analysis_seconds=time.perf_counter() - started_perf,
                assets=[asset_reports[base] for base in WALK_MAJOR_BASES],
                wave_buckets={k: tuple(v) for k, v in wave_buckets.items()},
            )

