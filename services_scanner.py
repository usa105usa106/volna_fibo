from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import math

import pandas as pd

from config import Settings
from core_models import MarketSnapshot, WaveState
from core_ranking import select_top_crypto
from core_senior import SeniorWaveDetector, control_no_setup, data_incomplete_state, no_setup_state
from core_symbols import display_symbol, normalize_symbol
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


@dataclass(slots=True)
class WalkForwardResult:
    exchange: str
    top_n: int
    assets_requested: int
    assets_tested: int
    checkpoints: int
    spacing_days: int
    horizon_days: int
    signals: int
    t1_first: int
    invalid_first: int
    unresolved: int
    ambiguous: int
    data_errors: int
    median_mfe_pct: float | None
    median_mae_pct: float | None
    rating_buckets: dict[str, tuple[int, int, int]]
    started_at: str
    finished_at: str


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
            session = await self.repo.active_session()
            if session is None:
                return RunResult("track", [], 0, 0, ["NO_ACTIVE_SEARCH_SESSION"], "", 0)
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
            )

    async def analyze_symbols(self, raw_symbols: list[str]) -> RunResult:
        """One-off fresh analysis of exactly the user-supplied symbols. No state mutation."""
        async with self.lock:
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
                return RunResult("manual", [], 0, 0, errors or ["NO_SYMBOLS"], exchange, top_n, [])

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
            )

    async def walk_forward(self) -> WalkForwardResult:
        """Diagnostic no-look-ahead replay. Does not touch Search/Tracking state or timers."""
        async with self.lock:
            started = datetime.now(timezone.utc)
            settings = await self.repo.get_settings()
            exchange = settings.exchange
            top_n = settings.top_n
            universe = await self.data.universe(exchange, top_n)
            if len(universe) < top_n:
                raise RuntimeError(f"/walk: exchange returned only {len(universe)} instruments for Top-{top_n}")

            horizon = self.cfg.walk_horizon_days
            spacing = self.cfg.walk_spacing_days
            count = self.cfg.walk_checkpoints
            now = pd.Timestamp.now(tz="UTC").floor("h")
            latest = now - pd.Timedelta(days=horizon)
            checkpoints = [latest - pd.Timedelta(days=spacing * i) for i in range(count - 1, -1, -1)]
            earliest = checkpoints[0]
            history_span_days = max(0, math.ceil((now - earliest).total_seconds() / 86400))
            h1_days = history_span_days + self.cfg.lookback_1h_days + 3
            d1_days = history_span_days + self.cfg.lookback_1d_days + 3

            signals = t1_first = invalid_first = unresolved = ambiguous = 0
            tested_assets = data_errors = 0
            mfe_values: list[float] = []
            mae_values: list[float] = []
            # bucket -> [signals, t1_first, invalid_first]
            buckets: dict[str, list[int]] = {
                "9.0+": [0, 0, 0],
                "8.0–8.9": [0, 0, 0],
                f"{self.cfg.min_rating:.1f}–7.9": [0, 0, 0],
            }

            rank_map = {symbol: idx + 1 for idx, (symbol, _) in enumerate(universe)}
            qv_map = dict(universe)
            aggregate_lock = asyncio.Lock()

            def rating_bucket(rating: float) -> str:
                if rating >= 9.0:
                    return "9.0+"
                if rating >= 8.0:
                    return "8.0–8.9"
                return f"{self.cfg.min_rating:.1f}–7.9"

            async def one_asset(symbol: str):
                nonlocal signals, t1_first, invalid_first, unresolved, ambiguous, tested_assets, data_errors
                local_signals = local_t1 = local_invalid = local_unresolved = local_ambiguous = 0
                local_mfe: list[float] = []
                local_mae: list[float] = []
                local_buckets = {k: [0, 0, 0] for k in buckets}
                try:
                    full = await self.data.walk_history(
                        exchange,
                        symbol,
                        qv_map.get(symbol, 0.0),
                        h1_days=h1_days,
                        d1_days=d1_days,
                    )
                except Exception:
                    log.exception("/walk data failed for %s", symbol)
                    async with aggregate_lock:
                        data_errors += 1
                    return

                def evaluate_history():
                    nonlocal local_signals, local_t1, local_invalid, local_unresolved, local_ambiguous
                    h1_all = full.hourly_closed.copy()
                    d1_all = full.daily_closed.copy()
                    h1_all["timestamp"] = pd.to_datetime(h1_all["timestamp"], utc=True)
                    d1_all["timestamp"] = pd.to_datetime(d1_all["timestamp"], utc=True)

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
                        state = self.detector.detect(snap, rank_map.get(symbol), top_n)
                        if state is None or (state.rating or 0.0) < self.cfg.min_rating:
                            continue
                        if not state.targets or state.strict_origin is None or state.current_price is None:
                            continue

                        local_signals += 1
                        bucket = rating_bucket(float(state.rating or 0.0))
                        local_buckets[bucket][0] += 1

                        future_end = checkpoint + pd.Timedelta(days=horizon)
                        future = h1_all[(h1_all["timestamp"] >= checkpoint) & (h1_all["timestamp"] < future_end)].copy()
                        if future.empty:
                            local_unresolved += 1
                            continue

                        entry = float(state.current_price)
                        local_mfe.append((float(future["high"].max()) / entry - 1.0) * 100.0)
                        local_mae.append((float(future["low"].min()) / entry - 1.0) * 100.0)
                        t1 = float(state.targets[0])
                        strict = float(state.strict_origin)

                        outcome = "unresolved"
                        for row in future.itertuples(index=False):
                            hit_t1 = float(row.high) >= t1
                            hit_invalid = float(row.low) < strict
                            if hit_t1 and hit_invalid:
                                outcome = "ambiguous"
                                break
                            if hit_t1:
                                outcome = "t1"
                                break
                            if hit_invalid:
                                outcome = "invalid"
                                break

                        if outcome == "t1":
                            local_t1 += 1
                            local_buckets[bucket][1] += 1
                        elif outcome == "invalid":
                            local_invalid += 1
                            local_buckets[bucket][2] += 1
                        elif outcome == "ambiguous":
                            local_ambiguous += 1
                        else:
                            local_unresolved += 1

                await self._compute(evaluate_history)

                async with aggregate_lock:
                    tested_assets += 1
                    signals += local_signals
                    t1_first += local_t1
                    invalid_first += local_invalid
                    unresolved += local_unresolved
                    ambiguous += local_ambiguous
                    mfe_values.extend(local_mfe)
                    mae_values.extend(local_mae)
                    for key, vals in local_buckets.items():
                        for i in range(3):
                            buckets[key][i] += vals[i]

            await self._map(one_asset, [symbol for symbol, _ in universe])
            finished = datetime.now(timezone.utc)
            return WalkForwardResult(
                exchange=exchange,
                top_n=top_n,
                assets_requested=len(universe),
                assets_tested=tested_assets,
                checkpoints=count,
                spacing_days=spacing,
                horizon_days=horizon,
                signals=signals,
                t1_first=t1_first,
                invalid_first=invalid_first,
                unresolved=unresolved,
                ambiguous=ambiguous,
                data_errors=data_errors,
                median_mfe_pct=float(pd.Series(mfe_values).median()) if mfe_values else None,
                median_mae_pct=float(pd.Series(mae_values).median()) if mae_values else None,
                rating_buckets={k: tuple(v) for k, v in buckets.items()},
                started_at=started.isoformat(),
                finished_at=finished.isoformat(),
            )
