from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Iterable

import pandas as pd

from core_models import MarketSnapshot, WaveState
from data_integrity import DataIntegrityError


FIB_RATIOS = (0.236, 0.382, 0.500, 0.618, 0.705, 0.786, 0.886, 0.950)
TARGET_MULTIPLIERS = (1.0, 1.618, 2.618, 4.236)


@dataclass(slots=True)
class DetectorConfig:
    daily_pivot_window: int = 3
    fourh_pivot_window: int = 3
    min_global_impulse_pct: float = 0.18
    min_global_atr_mult: float = 3.0
    min_w2_retrace: float = 0.50
    max_w2_retrace: float = 0.995
    min_nested_impulse_pct: float = 0.10
    min_nested_retrace: float = 0.50
    max_nested_retrace: float = 0.97
    max_w1_age_days: int = 180


def complete4h(hourly_closed: pd.DataFrame) -> pd.DataFrame:
    """Build COMPLETE4H only from the exact UTC hourly slots 00/01/02/03, 04/05/06/07, etc."""
    columns = ["timestamp", "open", "high", "low", "close", "volume", "n"]
    if hourly_closed.empty:
        return pd.DataFrame(columns=columns)
    df = hourly_closed.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values("timestamp").drop_duplicates("timestamp", keep="last")

    # Four rows in one floor('4h') bucket are not enough by themselves: malformed or
    # offset timestamps (e.g. 00:30/01:30/02:30/03:30) must never become a senior C4H.
    aligned = (
        df["timestamp"].dt.minute.eq(0)
        & df["timestamp"].dt.second.eq(0)
        & df["timestamp"].dt.microsecond.eq(0)
    )
    df = df.loc[aligned].copy()
    if df.empty:
        return pd.DataFrame(columns=columns)

    df["bucket"] = df["timestamp"].dt.floor("4h")
    df["slot"] = ((df["timestamp"] - df["bucket"]) / pd.Timedelta(hours=1)).astype(int)
    agg = (
        df.groupby("bucket", sort=True)
        .agg(
            n=("close", "size"),
            slots=("slot", "nunique"),
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        )
        .reset_index()
        .rename(columns={"bucket": "timestamp"})
    )
    return agg.loc[(agg["n"] == 4) & (agg["slots"] == 4), columns].reset_index(drop=True)


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev = close.shift(1)
    tr = pd.concat([(high - low).abs(), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=max(3, period // 2)).mean()


def _pivot_mask(series: pd.Series, window: int, kind: str) -> pd.Series:
    roll = series.rolling(window * 2 + 1, center=True, min_periods=window * 2 + 1)
    if kind == "low":
        return series.eq(roll.min())
    return series.eq(roll.max())


def _iso(ts: pd.Timestamp | datetime | str | None) -> str | None:
    if ts is None:
        return None
    return pd.Timestamp(ts).tz_convert("UTC").isoformat() if pd.Timestamp(ts).tzinfo else pd.Timestamp(ts, tz="UTC").isoformat()


def _fib_prices(origin: float, high: float) -> dict[str, float]:
    length = high - origin
    return {f"{r:.3f}": high - r * length for r in FIB_RATIOS}


def _fib_status(close: float, fibs: dict[str, float], recent_closes: Iterable[float]) -> tuple[str, int]:
    # Recovery is evaluated on COMPLETE4H closes, not live price.
    recent = list(recent_closes)[-3:]
    thresholds = [
        (0.236, fibs["0.236"]),
        (0.382, fibs["0.382"]),
        (0.500, fibs["0.500"]),
        (0.618, fibs["0.618"]),
        (0.705, fibs["0.705"]),
        (0.786, fibs["0.786"]),
        (0.886, fibs["0.886"]),
        (0.950, fibs["0.950"]),
    ]
    for ratio, level in thresholds:
        if close >= level:
            holds = sum(1 for c in recent if c >= level)
            suffix = f" · {holds}/{len(recent)} C4H" if recent else ""
            ratio_text = f"{ratio:.3f}".split(".")[1]
            return f"> .{ratio_text}{suffix}", holds
    return "< .950", 0


def _zones(origin: float, high: float, strict_origin: float) -> tuple[tuple[float, float], tuple[float, float]]:
    fibs = _fib_prices(origin, high)
    base = tuple(sorted((fibs["0.886"], fibs["0.786"])))
    deep_low = max(strict_origin * 1.0005, fibs["0.950"])
    deep = tuple(sorted((deep_low, fibs["0.886"])))
    return base, deep


def _targets(working_low: float, impulse_length: float) -> list[float]:
    return [working_low + m * impulse_length for m in TARGET_MULTIPLIERS]


def _rating(
    retrace: float,
    growth_pct: float,
    strict_distance_pct: float,
    fib_status: str,
    liquidity_rank: int | None,
    top_n: int,
    wave_type: str,
) -> float:
    score = 2.0  # valid senior structure

    if 0.618 <= retrace <= 0.886:
        score += 2.5
    elif 0.886 < retrace <= 0.97:
        score += 2.3
    elif 0.50 <= retrace < 0.618:
        score += 1.8
    else:
        score += 1.3

    if growth_pct <= 3:
        score += 2.0
    elif growth_pct <= 7:
        score += 1.6
    elif growth_pct <= 12:
        score += 1.0
    elif growth_pct <= 20:
        score += 0.4

    if "> .236" in fib_status:
        score += 1.4
    elif "> .382" in fib_status:
        score += 1.2
    elif "> .500" in fib_status or "> .5" in fib_status:
        score += 0.9
    elif "> .618" in fib_status:
        score += 0.6
    elif "> .705" in fib_status or "> .786" in fib_status:
        score += 0.3

    if liquidity_rank is None:
        score += 0.4
    else:
        score += max(0.2, 1.0 - (liquidity_rank - 1) / max(top_n, 1) * 0.8)

    if wave_type == "W3-(2)":
        score += 0.3

    if strict_distance_pct < 0.75:
        score -= 1.0
    elif strict_distance_pct < 1.5:
        score -= 0.4

    if growth_pct > 20:
        score -= min(1.5, (growth_pct - 20) / 20)

    return round(max(0.0, min(10.0, score)), 1)


def _status_from_state(growth_pct: float, fib_status: str) -> str:
    if growth_pct > 30:
        return "EXTENDED"
    if "> .382" in fib_status or "> .236" in fib_status:
        return "CONFIRMED"
    if "> .618" in fib_status or "> .500" in fib_status or "> .5" in fib_status:
        return "RECOVERING"
    return "DEEP"


class SeniorWaveDetector:
    def __init__(self, cfg: DetectorConfig | None = None):
        self.cfg = cfg or DetectorConfig()

    def detect(self, snapshot: MarketSnapshot, liquidity_rank: int | None, top_n: int) -> WaveState | None:
        daily = snapshot.daily_closed.copy()
        h4 = complete4h(snapshot.hourly_closed)
        if len(daily) < 35 or len(h4) < 40:
            return None

        daily["timestamp"] = pd.to_datetime(daily["timestamp"], utc=True)
        h4["timestamp"] = pd.to_datetime(h4["timestamp"], utc=True)
        daily = daily.sort_values("timestamp").reset_index(drop=True)
        h4 = h4.sort_values("timestamp").reset_index(drop=True)
        daily["atr"] = _atr(daily)
        low_mask = _pivot_mask(daily["low"], self.cfg.daily_pivot_window, "low")
        high_mask = _pivot_mask(daily["high"], self.cfg.daily_pivot_window, "high")
        lows = daily.index[low_mask.fillna(False)].tolist()
        highs = daily.index[high_mask.fillna(False)].tolist()
        if not lows or not highs:
            return None

        now_ts = daily["timestamp"].iloc[-1]
        global_candidates: list[dict] = []
        for li in lows:
            origin = float(daily.at[li, "low"])
            origin_ts = daily.at[li, "timestamp"]
            if (now_ts - origin_ts).days > self.cfg.max_w1_age_days:
                continue
            atr = float(daily.at[li, "atr"]) if not pd.isna(daily.at[li, "atr"]) else 0.0
            for hi in highs:
                if hi <= li:
                    continue
                high_ts = daily.at[hi, "timestamp"]
                # Never evaluate a W1 whose high predates the available COMPLETE4H
                # history. Otherwise a 180-day daily candidate could be judged from only
                # the tail of a shorter 1H/4H history.
                if high_ts < h4["timestamp"].iloc[0]:
                    continue
                days = (high_ts - origin_ts).days
                if days < 1 or days > 120:
                    continue
                high = float(daily.at[hi, "high"])
                # A daily timestamp is the candle OPEN, not the time of its high.
                # Locate the same peak in COMPLETE4H before selecting a later W2.
                peak_day = h4[(h4["timestamp"] >= high_ts)
                              & (h4["timestamp"] < high_ts + pd.Timedelta(days=1))]
                peaks = peak_day[peak_day["high"] == high]
                if peaks.empty:
                    continue
                peak_ts = peaks["timestamp"].iloc[0]
                # A candidate W1 origin cannot be silently broken before W1 high.
                between = daily.loc[li:hi, "low"]
                if float(between.min()) < origin:
                    continue
                length = high - origin
                if length <= 0:
                    continue
                if high / origin - 1 < self.cfg.min_global_impulse_pct:
                    continue
                if atr > 0 and length < self.cfg.min_global_atr_mult * atr:
                    continue
                after = h4[h4["timestamp"] > peak_ts]
                if len(after) < 2:
                    continue
                low_idx = after["low"].idxmin()
                w2_low = float(h4.at[low_idx, "low"])
                w2_ts = h4.at[low_idx, "timestamp"]
                retrace = (high - w2_low) / length
                if w2_low <= origin or not (self.cfg.min_w2_retrace <= retrace <= self.cfg.max_w2_retrace):
                    continue
                global_candidates.append(
                    {
                        "origin": origin,
                        "origin_ts": origin_ts,
                        "high": high,
                        "high_ts": peak_ts,
                        "w2_low": w2_low,
                        "w2_ts": w2_ts,
                        "retrace": retrace,
                    }
                )

        if not global_candidates:
            return None

        # Most recent valid correction first; depth is a secondary preference.
        global_candidates.sort(key=lambda x: (x["w2_ts"], x["retrace"]), reverse=True)
        g = global_candidates[0]

        nested = self._detect_nested(h4, g)
        if nested is not None:
            return self._build_state(
                snapshot=snapshot,
                wave_type="W3-(2)",
                origin=nested["origin"],
                impulse_high=nested["high"],
                working_low=nested["low"],
                strict_origin=g["w2_low"],
                impulse_start_ts=g["w2_ts"],
                impulse_high_ts=nested["high_ts"],
                working_low_ts=nested["low_ts"],
                retrace=nested["retrace"],
                parent_w2_low=g["w2_low"],
                parent_w2_ts=g["w2_ts"],
                w3_1_high=nested["high"],
                w3_1_high_ts=nested["high_ts"],
                h4=h4,
                liquidity_rank=liquidity_rank,
                top_n=top_n,
                is_control=snapshot.symbol in {"XAU", "USOIL"},
            )

        return self._build_state(
            snapshot=snapshot,
            wave_type="W2",
            origin=g["origin"],
            impulse_high=g["high"],
            working_low=g["w2_low"],
            strict_origin=g["origin"],
            impulse_start_ts=g["origin_ts"],
            impulse_high_ts=g["high_ts"],
            working_low_ts=g["w2_ts"],
            retrace=g["retrace"],
            parent_w2_low=None,
            parent_w2_ts=None,
            w3_1_high=None,
            w3_1_high_ts=None,
            h4=h4,
            liquidity_rank=liquidity_rank,
            top_n=top_n,
            is_control=snapshot.symbol in {"XAU", "USOIL"},
        )

    def _detect_nested(self, h4: pd.DataFrame, g: dict) -> dict | None:
        after_w2 = h4[h4["timestamp"] > g["w2_ts"]].copy()
        if len(after_w2) < 8:
            return None
        high_mask = _pivot_mask(after_w2["high"], self.cfg.fourh_pivot_window, "high")
        candidates: list[dict] = []
        for idx in after_w2.index[high_mask.fillna(False)]:
            high = float(h4.at[idx, "high"])
            high_ts = h4.at[idx, "timestamp"]
            length = high - g["w2_low"]
            if length <= 0 or high / g["w2_low"] - 1 < self.cfg.min_nested_impulse_pct:
                continue
            tail = h4[h4["timestamp"] > high_ts]
            if len(tail) < 2:
                continue
            low_idx = tail["low"].idxmin()
            low = float(h4.at[low_idx, "low"])
            low_ts = h4.at[low_idx, "timestamp"]
            retrace = (high - low) / length
            if low <= g["w2_low"]:
                continue
            if self.cfg.min_nested_retrace <= retrace <= self.cfg.max_nested_retrace:
                candidates.append(
                    {
                        "origin": g["w2_low"],
                        "high": high,
                        "high_ts": high_ts,
                        "low": low,
                        "low_ts": low_ts,
                        "retrace": retrace,
                    }
                )
        if not candidates:
            return None
        candidates.sort(key=lambda x: (x["low_ts"], x["retrace"]), reverse=True)
        return candidates[0]

    def _build_state(
        self,
        snapshot: MarketSnapshot,
        wave_type: str,
        origin: float,
        impulse_high: float,
        working_low: float,
        strict_origin: float,
        impulse_start_ts,
        impulse_high_ts,
        working_low_ts,
        retrace: float,
        parent_w2_low: float | None,
        parent_w2_ts,
        w3_1_high: float | None,
        w3_1_high_ts,
        h4: pd.DataFrame,
        liquidity_rank: int | None,
        top_n: int,
        is_control: bool,
    ) -> WaveState | None:
        live = snapshot.live_price or float(h4["close"].iloc[-1])
        # Strict invalidation is intrabar-aware: closed 1H candles inside the current
        # incomplete 4H bucket count for violation detection, but never for re-anchor.
        h1 = snapshot.hourly_closed.copy()
        if not h1.empty:
            h1["timestamp"] = pd.to_datetime(h1["timestamp"], utc=True)
            cut_from = pd.Timestamp(impulse_high_ts)
            if cut_from.tzinfo is None:
                cut_from = cut_from.tz_localize("UTC")
            relevant_h1 = h1[h1["timestamp"] > cut_from]
            if not relevant_h1.empty and float(relevant_h1["low"].min()) < strict_origin:
                return None
        if snapshot.live_low is not None and snapshot.live_low < strict_origin:
            return None
        if working_low <= strict_origin:
            return None

        fibs = _fib_prices(origin, impulse_high)
        last_close = float(h4["close"].iloc[-1])
        status, _ = _fib_status(last_close, fibs, h4["close"].tail(3).tolist())
        growth = (live / working_low - 1) * 100 if working_low else math.nan
        strict_distance = (working_low / strict_origin - 1) * 100 if strict_origin else math.nan
        rating = _rating(retrace, growth, strict_distance, status, liquidity_rank, top_n, wave_type)
        base, deep = _zones(origin, impulse_high, strict_origin)
        impulse_length = impulse_high - origin
        now = datetime.now(timezone.utc).isoformat()
        return WaveState(
            symbol=snapshot.symbol,
            exchange=snapshot.exchange,
            wave_type=wave_type,  # type: ignore[arg-type]
            status=_status_from_state(growth, status),  # type: ignore[arg-type]
            origin=origin,
            impulse_high=impulse_high,
            working_low=working_low,
            strict_origin=strict_origin,
            impulse_start_ts=_iso(impulse_start_ts),
            impulse_high_ts=_iso(impulse_high_ts),
            working_low_ts=_iso(working_low_ts),
            parent_w2_low=parent_w2_low,
            parent_w2_ts=_iso(parent_w2_ts),
            w3_1_high=w3_1_high,
            w3_1_high_ts=_iso(w3_1_high_ts),
            retrace_depth=retrace,
            fibs=fibs,
            fib_status=status,
            targets=_targets(working_low, impulse_length),
            base_zone=base,
            deep_zone=deep,
            current_price=live,
            growth_from_low_pct=growth,
            strict_distance_pct=strict_distance,
            rating=rating,
            liquidity_rank=liquidity_rank,
            last_complete4h_bucket=_iso(h4["timestamp"].iloc[-1]),
            last_complete4h_close=last_close,
            last_event="FOUND",
            is_control=is_control,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def _clear_derived(previous: WaveState, *, fib_status: str) -> None:
        """Remove values derived from an obsolete senior count.

        Anchors are retained for diagnostics/recount context, but no old Fib, zones,
        targets, distance/growth metrics or score may leak into an INVALID/RECOUNT row.
        """
        previous.retrace_depth = None
        previous.fibs = {}
        previous.fib_status = fib_status
        previous.targets = []
        previous.base_zone = None
        previous.deep_zone = None
        previous.growth_from_low_pct = None
        previous.strict_distance_pct = None
        previous.rating = 0.0

    def track(self, previous: WaveState, snapshot: MarketSnapshot, top_n: int) -> WaveState:
        """Update one saved senior structure without searching other symbols."""
        now = datetime.now(timezone.utc).isoformat()
        if previous.status in {"INVALID", "RECOUNT"}:
            diagnostic = (
                "STRICT ORIGIN BROKEN"
                if previous.status == "INVALID"
                else (
                    previous.fib_status
                    if previous.fib_status in {"MISSING STRICT ORIGIN", "MISSING SENIOR ANCHOR", "RECOUNT REQUIRED"}
                    else "RECOUNT REQUIRED"
                )
            )
            self._clear_derived(previous, fib_status=diagnostic)
            previous.current_price = snapshot.live_price or previous.current_price
            previous.updated_at = now
            return previous

        # Controls with no setup may be re-scanned because XAU/USOIL are permanent controls.
        if previous.wave_type == "CONTROL" or previous.status == "NO_SETUP":
            found = self.detect(snapshot, previous.liquidity_rank, top_n)
            if found:
                found.is_control = True
                found.last_event = "CONTROL_SETUP_FOUND"
                return found
            previous.current_price = snapshot.live_price
            previous.updated_at = now
            previous.last_event = "CONTROL_NO_SETUP"
            return previous

        strict = previous.strict_origin
        if strict is None:
            previous.status = "RECOUNT"
            self._clear_derived(previous, fib_status="MISSING STRICT ORIGIN")
            previous.last_event = "MISSING_STRICT_ORIGIN"
            previous.updated_at = now
            return previous

        # Strict origin wick break invalidates immediately, even inside incomplete 4H.
        # Check all CLOSED 1H candles after the last official COMPLETE4H plus the live 1H low.
        hourly = snapshot.hourly_closed.copy()
        intrabar_break = False
        if not hourly.empty:
            hourly["timestamp"] = pd.to_datetime(hourly["timestamp"], utc=True)
            if previous.last_complete4h_bucket:
                last_bucket = pd.Timestamp(previous.last_complete4h_bucket)
                if last_bucket.tzinfo is None:
                    last_bucket = last_bucket.tz_localize("UTC")
                hourly = hourly[hourly["timestamp"] >= last_bucket + pd.Timedelta(hours=4)]
            if not hourly.empty:
                intrabar_break = float(hourly["low"].min()) < strict
        if snapshot.live_low is not None and snapshot.live_low < strict:
            intrabar_break = True
        if intrabar_break:
            previous.status = "INVALID"
            self._clear_derived(previous, fib_status="STRICT ORIGIN BROKEN")
            previous.last_event = "OLD COUNT INVALID — FULL SENIOR RECOUNT REQUIRED"
            previous.current_price = snapshot.live_price
            previous.updated_at = now
            return previous

        h4 = complete4h(snapshot.hourly_closed)
        if h4.empty:
            previous.last_event = "INCOMPLETE — NO COMPLETE4H"
            previous.updated_at = now
            return previous

        last_seen = pd.Timestamp(previous.last_complete4h_bucket) if previous.last_complete4h_bucket else None
        if last_seen is not None:
            last_seen = last_seen.tz_localize("UTC") if last_seen.tzinfo is None else last_seen.tz_convert("UTC")
            if h4["timestamp"].iloc[-1] < last_seen:
                raise DataIntegrityError("tracking COMPLETE4H checkpoint moved backwards")
        new_h4 = h4 if last_seen is None else h4[h4["timestamp"] > last_seen]
        if not new_h4.empty and float(new_h4["low"].min()) < strict:
            previous.status = "INVALID"
            self._clear_derived(previous, fib_status="STRICT ORIGIN BROKEN")
            previous.last_event = "OLD COUNT INVALID — FULL SENIOR RECOUNT REQUIRED"
            previous.current_price = snapshot.live_price
            previous.updated_at = now
            return previous
        reanchored = False
        if not new_h4.empty and previous.working_low is not None:
            idx = new_h4["low"].idxmin()
            candidate_low = float(h4.at[idx, "low"])
            if strict < candidate_low < previous.working_low:
                previous.working_low = candidate_low
                previous.working_low_ts = _iso(h4.at[idx, "timestamp"])
                reanchored = True

        origin = previous.origin
        high = previous.impulse_high
        if origin is None or high is None or previous.working_low is None:
            previous.status = "RECOUNT"
            self._clear_derived(previous, fib_status="MISSING SENIOR ANCHOR")
            previous.last_event = "MISSING SENIOR ANCHOR"
            previous.updated_at = now
            return previous

        length = high - origin
        previous.retrace_depth = (high - previous.working_low) / length if length > 0 else None
        previous.fibs = _fib_prices(origin, high)
        last_close = float(h4["close"].iloc[-1])
        fib_status, _ = _fib_status(last_close, previous.fibs, h4["close"].tail(3).tolist())
        previous.fib_status = fib_status
        previous.last_complete4h_close = last_close
        previous.last_complete4h_bucket = _iso(h4["timestamp"].iloc[-1])
        previous.current_price = snapshot.live_price or last_close
        previous.growth_from_low_pct = (previous.current_price / previous.working_low - 1) * 100
        previous.strict_distance_pct = (previous.working_low / strict - 1) * 100
        previous.targets = _targets(previous.working_low, length)
        previous.base_zone, previous.deep_zone = _zones(origin, high, strict)
        previous.rating = _rating(
            previous.retrace_depth or 0.0,
            previous.growth_from_low_pct,
            previous.strict_distance_pct,
            previous.fib_status,
            previous.liquidity_rank,
            top_n,
            previous.wave_type,
        )
        previous.status = _status_from_state(previous.growth_from_low_pct, previous.fib_status)  # type: ignore[assignment]
        previous.last_event = "RE-ANCHOR COMPLETE4H" if reanchored else "UPDATED"
        previous.updated_at = now
        return previous


def no_setup_state(
    symbol: str,
    exchange: str,
    price: float | None,
    *,
    is_control: bool = False,
    event: str = "NO_SETUP",
) -> WaveState:
    now = datetime.now(timezone.utc).isoformat()
    return WaveState(
        symbol=symbol,
        exchange=exchange,
        wave_type="CONTROL" if is_control else "NONE",
        status="NO_SETUP",
        origin=None,
        impulse_high=None,
        working_low=None,
        strict_origin=None,
        current_price=price,
        rating=None if is_control else 0.0,
        fib_status="NO SENIOR W2/W3-(2)",
        last_event=event,
        is_control=is_control,
        created_at=now,
        updated_at=now,
    )


def data_incomplete_state(
    symbol: str,
    exchange: str,
    price: float | None,
    *,
    is_control: bool = False,
    event: str = "DATA INCOMPLETE",
) -> WaveState:
    now = datetime.now(timezone.utc).isoformat()
    return WaveState(
        symbol=symbol,
        exchange=exchange,
        wave_type="CONTROL" if is_control else "NONE",
        status="DATA_INCOMPLETE",
        origin=None,
        impulse_high=None,
        working_low=None,
        strict_origin=None,
        current_price=price,
        rating=None,
        fib_status="DATA INCOMPLETE",
        last_event=event,
        is_control=is_control,
        created_at=now,
        updated_at=now,
    )


def control_no_setup(symbol: str, exchange: str, price: float | None) -> WaveState:
    return no_setup_state(symbol, exchange, price, is_control=True, event="CONTROL_NO_SETUP")
