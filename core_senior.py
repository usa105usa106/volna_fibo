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
    # Relative-degree floor is deliberately soft.  v0015 used 0.30 and that
    # rejected the user-validated GRAM 1.286 -> 1.740 W3-(1).  Micro pullbacks
    # are filtered primarily by the active-record-high lifecycle below: once a
    # candidate high is accepted above on COMPLETE4H it is finished/stale and
    # cannot remain the current W3-(2) anchor.
    min_nested_parent_impulse_ratio: float = 0.15
    min_nested_retrace: float = 0.20
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


def _zones(working_low: float, high: float, strict_origin: float) -> tuple[tuple[float, float], tuple[float, float]]:
    """Execution zones around the *active* senior correction low.

    The old implementation projected .786-.950 of the entire parent impulse.  Once
    W2/W3-(2) had already printed and price started recovering this pushed entry
    zones absurdly far below the live senior low (the BCH failure was the clearest
    example).  The working table used in the parquet workflow instead treats the
    confirmed working low as the execution anchor and the first 23.6% recovery of
    the correction as the normal retest area.

    Base: working low -> 23.6% recovery toward the impulse high.
    Sweep: a narrow 5% correction-amplitude buffer below the working low, never
    through strict origin.
    """
    correction = max(0.0, high - working_low)
    base_upper = working_low + 0.236 * correction
    base = tuple(sorted((working_low, base_upper)))

    # A sweep zone is deliberately narrow.  It is execution context only and must
    # not recreate the old deep-parent-Fib zones near strict origin.
    sweep_buffer = 0.05 * correction
    deep_low = max(strict_origin * 1.0005, working_low - sweep_buffer)
    deep = tuple(sorted((deep_low, working_low)))
    return base, deep


def _targets(working_low: float, impulse_length: float) -> list[float]:
    return [working_low + m * impulse_length for m in TARGET_MULTIPLIERS]


def _target_projection_anchors(
    wave_type: str,
    *,
    origin: float | None,
    impulse_high: float | None,
    parent_w2_low: float | None = None,
    w3_1_high: float | None = None,
) -> tuple[float, float, str] | None:
    """Return the only anchors allowed to drive the senior target projection.

    v0017 intentionally separates target geometry from the generic wave fields.
    This prevents a W3-(2) from accidentally using an old/global W1 impulse just
    because ``origin`` / ``impulse_high`` happened to contain those values.

    W2      -> previous W1: origin -> impulse_high
    W3-(2)  -> nested W3-(1): parent_w2_low -> w3_1_high

    A malformed W3-(2) is not allowed to silently fall back to global anchors.
    Returning ``None`` forces a recount instead of publishing bogus targets.
    """
    if wave_type == "W3-(2)":
        lo = parent_w2_low
        hi = w3_1_high
        source = "W3-(1): parent W2 → W3-(1) high"
    elif wave_type == "W2":
        lo = origin
        hi = impulse_high
        source = "W1: origin → W1 high"
    else:
        return None

    if lo is None or hi is None:
        return None
    lo = float(lo)
    hi = float(hi)
    if not math.isfinite(lo) or not math.isfinite(hi) or hi <= lo:
        return None
    return lo, hi, source


def _project_targets(
    wave_type: str,
    *,
    working_low: float | None,
    origin: float | None,
    impulse_high: float | None,
    parent_w2_low: float | None = None,
    w3_1_high: float | None = None,
) -> tuple[list[float], float, float, float, str] | None:
    """Build T1/T2/T3/T4 from the exact manual-parquet projection anchors.

    The multipliers are fixed at 1.000 / 1.618 / 2.618 / 4.236.  Only the active
    correction low may move during tracking; the projection impulse itself is
    immutable for that wave count.
    """
    if working_low is None or not math.isfinite(float(working_low)):
        return None
    anchors = _target_projection_anchors(
        wave_type,
        origin=origin,
        impulse_high=impulse_high,
        parent_w2_low=parent_w2_low,
        w3_1_high=w3_1_high,
    )
    if anchors is None:
        return None
    anchor_origin, anchor_high, source = anchors
    length = anchor_high - anchor_origin
    targets = _targets(float(working_low), length)
    if len(targets) != 4 or any(
        (not math.isfinite(t)) or t <= float(working_low) for t in targets
    ):
        return None
    if any(b <= a for a, b in zip(targets, targets[1:])):
        return None
    return targets, anchor_origin, anchor_high, length, source


def _rating(
    retrace: float,
    growth_pct: float,
    strict_distance_pct: float,
    fib_status: str,
    liquidity_rank: int | None,
    top_n: int,
    wave_type: str,
    t1_upside_pct: float | None = None,
) -> float:
    """Opportunity score for an active senior setup.

    This deliberately scores *current asymmetry*, not coin quality.  The previous
    score over-weighted deep parent retracement and ignored projected upside, which
    made a mature global W2 outrank a fresh nested W3-(2).  That is the opposite of
    the parquet workflow where a newly formed, strict-valid W3-(2) close to its low
    with large T1 room is usually the premium setup.
    """
    score = 2.0  # valid senior structure

    # Degree/progression.  A valid nested W3-(2) is closer to the main W3 expansion
    # and gets a meaningful, but not overriding, premium.
    if wave_type == "W3-(2)":
        score += 1.2
        if 0.20 <= retrace < 0.382:
            # Shallow nested W3-(2) is valid, but do not automatically score it as
            # premium.  previous release over-rated these and produced many 9.x false positives.
            score += 0.5
        elif retrace < 0.618:
            score += 1.1
        elif retrace <= 0.886:
            score += 1.3
        elif retrace <= 0.97:
            score += 1.0
        else:
            score += 0.5
    else:
        score += 0.8
        if 0.618 <= retrace <= 0.886:
            score += 1.4
        elif 0.886 < retrace <= 0.97:
            score += 1.2
        elif 0.50 <= retrace < 0.618:
            score += 1.0
        else:
            score += 0.5

    # Freshness to the confirmed working low.
    if growth_pct <= 3:
        score += 1.8
    elif growth_pct <= 6:
        score += 1.6
    elif growth_pct <= 10:
        score += 1.1
    elif growth_pct <= 15:
        score += 0.6
    elif growth_pct <= 20:
        score += 0.2

    # Recovery quality on COMPLETE4H.  The level matters, but persistence matters
    # too: 3/3 C4H is stronger than a one-bucket reclaim.
    recovery = 0.0
    for marker, value in (("> .236", 1.6), ("> .382", 1.45), ("> .500", 1.2),
                          ("> .5", 1.2), ("> .618", 0.9), ("> .705", 0.65),
                          ("> .786", 0.55), ("> .886", 0.35), ("> .950", 0.2)):
        if marker in fib_status:
            recovery = value
            break
    if "3/3 C4H" in fib_status:
        recovery += 0.15
    elif "1/3 C4H" in fib_status:
        recovery -= 0.15
    score += max(0.0, recovery)

    # Convexity: room to the first senior target is a core part of opportunity.
    if t1_upside_pct is not None:
        if t1_upside_pct >= 25:
            score += 1.5
        elif t1_upside_pct >= 18:
            score += 1.3
        elif t1_upside_pct >= 12:
            score += 1.0
        elif t1_upside_pct >= 8:
            score += 0.7
        elif t1_upside_pct >= 4:
            score += 0.3
        elif t1_upside_pct <= 0:
            score -= 1.0

    # Liquidity/execution quality remains a modest tie-breaker, never an override.
    if liquidity_rank is None:
        # Permanent controls (XAU/USOIL) do not have a crypto-cap rank but are highly
        # liquid execution instruments.  Treat missing crypto rank as neutral/robust,
        # not as a penalty.
        score += 0.8
    else:
        frac = (liquidity_rank - 1) / max(top_n, 1)
        score += max(0.25, 0.6 - frac * 0.35)

    # A low sitting directly on strict origin is fragile even if everything else is
    # attractive.
    if strict_distance_pct < 0.75:
        score -= 1.2
    elif strict_distance_pct < 1.5:
        score -= 0.6
    elif strict_distance_pct < 3.0:
        score -= 0.2

    # Late structures remain in accompaniment but are poor fresh entries.
    if growth_pct > 20:
        score -= min(2.0, 0.5 + (growth_pct - 20) / 20)

    # Calibration from the parquet workflow: a crypto W3-(2) does not deserve 9.x
    # merely because it is nested and close to its low. Durable recovery is mandatory.
    # This is intentionally a cap (not a bonus): it preserves convexity information
    # while stopping weak-recovery structures from outranking cleaner setups.
    if wave_type == "W3-(2)" and liquidity_rank is not None:
        if "1/3 C4H" in fib_status:
            score = min(score, 8.5)
        elif "2/3 C4H" in fib_status:
            score = min(score, 8.8)
        elif "3/3 C4H" in fib_status:
            if any(marker in fib_status for marker in ("> .618", "> .705", "> .786", "> .886", "> .950")):
                score = min(score, 8.4)
            elif "> .500" in fib_status or "> .5" in fib_status:
                score = min(score, 8.9)

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

    def _global_candidates(self, daily: pd.DataFrame, h4: pd.DataFrame) -> list[dict]:
        """Enumerate senior W1->W2 parents without letting later W3 price action re-anchor W2.

        A W2 is allowed to re-anchor only until the market has *accepted* back above the
        W1 high on COMPLETE4H.  After that close the parent W2 is locked.  The previous
        detector kept taking the minimum low from the whole future tail, so a later W3/W4
        pullback could be silently relabelled as the old W2 and then all targets moved.
        """
        daily = daily.copy()
        daily["atr"] = _atr(daily)
        low_mask = _pivot_mask(daily["low"], self.cfg.daily_pivot_window, "low")
        high_mask = _pivot_mask(daily["high"], self.cfg.daily_pivot_window, "high")
        lows = daily.index[low_mask.fillna(False)].tolist()
        highs = daily.index[high_mask.fillna(False)].tolist()
        if not lows or not highs:
            return []

        now_ts = daily["timestamp"].iloc[-1]
        result: list[dict] = []
        for li in lows:
            origin = float(daily.at[li, "low"])
            origin_ts = daily.at[li, "timestamp"]
            if (now_ts - origin_ts).days > self.cfg.max_w1_age_days:
                continue
            atr = float(daily.at[li, "atr"]) if not pd.isna(daily.at[li, "atr"]) else 0.0
            for hi in highs:
                if hi <= li:
                    continue
                high_day_ts = daily.at[hi, "timestamp"]
                if high_day_ts < h4["timestamp"].iloc[0]:
                    continue
                days = (high_day_ts - origin_ts).days
                if days < 1 or days > 120:
                    continue
                high = float(daily.at[hi, "high"])
                peak_day = h4[
                    (h4["timestamp"] >= high_day_ts)
                    & (h4["timestamp"] < high_day_ts + pd.Timedelta(days=1))
                ]
                peaks = peak_day[peak_day["high"] == high]
                if peaks.empty:
                    continue
                peak_ts = peaks["timestamp"].iloc[0]
                if float(daily.loc[li:hi, "low"].min()) < origin:
                    continue
                length = high - origin
                if length <= 0:
                    continue
                impulse_pct = high / origin - 1.0
                if impulse_pct < self.cfg.min_global_impulse_pct:
                    continue
                if atr > 0 and length < self.cfg.min_global_atr_mult * atr:
                    continue

                after = h4[h4["timestamp"] > peak_ts]
                if len(after) < 2:
                    continue
                accepted = after[after["close"] > high]
                if accepted.empty:
                    correction = after
                    breakout_ts = None
                    w2_locked = False
                else:
                    breakout_ts = accepted["timestamp"].iloc[0]
                    correction = after[after["timestamp"] < breakout_ts]
                    w2_locked = True
                if len(correction) < 2:
                    continue
                low_idx = correction["low"].idxmin()
                w2_low = float(h4.at[low_idx, "low"])
                w2_ts = h4.at[low_idx, "timestamp"]
                retrace = (high - w2_low) / length
                if w2_low <= origin or not (self.cfg.min_w2_retrace <= retrace <= self.cfg.max_w2_retrace):
                    continue
                result.append(
                    {
                        "origin": origin,
                        "origin_ts": origin_ts,
                        "high": high,
                        "high_ts": peak_ts,
                        "w2_low": w2_low,
                        "w2_ts": w2_ts,
                        "retrace": retrace,
                        "w2_locked": w2_locked,
                        "breakout_ts": breakout_ts,
                        "impulse_pct": impulse_pct,
                    }
                )
        return result

    @staticmethod
    def _same_level(a: float | None, b: float | None, *, rel: float = 0.015) -> bool:
        if a is None or b is None:
            return False
        scale = max(abs(float(a)), abs(float(b)), 1e-12)
        return abs(float(a) - float(b)) / scale <= rel

    def _nested_impulse_is_senior(self, high: float, g: dict) -> bool:
        """Reject micro 4H W3-(1) candidates inside an otherwise senior parent.

        The percentage floor alone was far too permissive for volatile alts: a +10%
        local leg could become "W3-(1)" even when the parent W1 was several times
        larger.  The parquet workflow compares degrees, so we also require the nested
        impulse to be meaningful versus the parent W1 amplitude.
        """
        parent_low = float(g["w2_low"])
        nested_len = float(high) - parent_low
        if nested_len <= 0 or nested_len / parent_low < self.cfg.min_nested_impulse_pct:
            return False
        parent_origin = g.get("origin")
        parent_high = g.get("high")
        if parent_origin is None or parent_high is None:
            return True
        parent_len = float(parent_high) - float(parent_origin)
        if parent_len <= 0:
            return True
        return nested_len >= self.cfg.min_nested_parent_impulse_ratio * parent_len

    def _lineage_nested(self, h4: pd.DataFrame, parent: dict, globals_: list[dict]) -> dict | None:
        """Recover W3-(2) lineage when a nested W3-(1) also looks like a new global W1.

        The important v0017 rule is *active structure*, not "first structure ever".
        A child high whose correction has already been followed by COMPLETE4H
        acceptance back above that high is historical/internal and must not own the
        current W3-(2) label.  Among still-active children we prefer the latest
        record high.
        """
        parent_low = float(parent["w2_low"])
        parent_ts = pd.Timestamp(parent["w2_ts"])
        if parent_ts.tzinfo is None:
            parent_ts = parent_ts.tz_localize("UTC")
        else:
            parent_ts = parent_ts.tz_convert("UTC")

        eligible: list[dict] = []
        for child in globals_:
            if child is parent:
                continue
            if not self._same_level(child.get("origin"), parent_low):
                continue
            high_ts = pd.Timestamp(child["high_ts"])
            low_ts = pd.Timestamp(child["w2_ts"])
            if high_ts.tzinfo is None:
                high_ts = high_ts.tz_localize("UTC")
            else:
                high_ts = high_ts.tz_convert("UTC")
            if low_ts.tzinfo is None:
                low_ts = low_ts.tz_localize("UTC")
            else:
                low_ts = low_ts.tz_convert("UTC")
            if high_ts <= parent_ts or low_ts <= high_ts:
                continue
            high = float(child["high"])
            if high <= parent_low:
                continue
            if not self._nested_impulse_is_senior(high, parent):
                continue

            # The W3-(1) must be a record high of the move from parent W2.  A lower
            # bounce inside an already-active correction is not a new senior W3-(1).
            history_to_high = h4[(h4["timestamp"] > parent_ts) & (h4["timestamp"] <= high_ts)]
            if history_to_high.empty or high + 1e-12 < float(history_to_high["high"].max()):
                continue

            tail = h4[h4["timestamp"] > high_ts]
            if len(tail) < 2:
                continue
            # Once the high is accepted above, that correction has completed.  It
            # cannot remain the current senior W3-(2) anchor (the v0015 BCH bug).
            if not tail[tail["close"] > high].empty:
                continue
            if float(tail["low"].min()) <= parent_low:
                continue
            low_idx = tail["low"].idxmin()
            low = float(h4.at[low_idx, "low"] )
            low_ts_active = h4.at[low_idx, "timestamp"]
            retrace = (high - low) / (high - parent_low)
            if not (self.cfg.min_nested_retrace <= retrace <= self.cfg.max_nested_retrace):
                continue
            eligible.append({
                "origin": parent_low,
                "high": high,
                "high_ts": high_ts,
                "low": low,
                "low_ts": low_ts_active,
                "retrace": retrace,
                "source": "GLOBAL_LINEAGE_ACTIVE",
            })

        if not eligible:
            return None
        eligible.sort(key=lambda c: pd.Timestamp(c["high_ts"]), reverse=True)
        return eligible[0]

    def detect(self, snapshot: MarketSnapshot, liquidity_rank: int | None, top_n: int) -> WaveState | None:
        daily = snapshot.daily_closed.copy()
        h4 = complete4h(snapshot.hourly_closed)
        if len(daily) < 35 or len(h4) < 40:
            return None

        daily["timestamp"] = pd.to_datetime(daily["timestamp"], utc=True)
        h4["timestamp"] = pd.to_datetime(h4["timestamp"], utc=True)
        daily = daily.sort_values("timestamp").reset_index(drop=True)
        h4 = h4.sort_values("timestamp").reset_index(drop=True)
        global_candidates = self._global_candidates(daily, h4)
        if not global_candidates:
            return None

        # A valid nested W3-(2) has hierarchy priority over a newer local geometry
        # that merely looks like another global W2. Within the same degree, prefer the
        # most recent senior parent; parent amplitude is only a final tie-breaker.
        ranked_states: list[tuple[tuple[float, float, float], WaveState]] = []
        for g in global_candidates:
            # Direct COMPLETE4H hierarchy is authoritative.  Global-lineage is a
            # fallback for cases where the same nested leg also appears as a daily
            # W1/W2 candidate (GRAM-like geometry).
            nested = self._detect_nested(h4, g)
            if nested is None:
                nested = self._lineage_nested(h4, g, global_candidates)
            if nested is not None:
                state = self._build_state(
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
                if state is not None:
                    ranked_states.append(
                        ((3.0, pd.Timestamp(g["w2_ts"]).timestamp(), float(g.get("impulse_pct", 0.0))), state)
                    )
                continue

            # Once W1 high has accepted above, the old global W2 is no longer the
            # developing wave. Keep it out of fresh Search unless a nested W3-(2) can
            # be identified. This prevents late W3/W4 price action from becoming W2.
            if g.get("w2_locked", False):
                continue

            state = self._build_state(
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
            if state is not None:
                ranked_states.append(
                    ((2.0, pd.Timestamp(g["w2_ts"]).timestamp(), float(g.get("impulse_pct", 0.0))), state)
                )

        if not ranked_states:
            return None
        ranked_states.sort(key=lambda item: item[0], reverse=True)
        return ranked_states[0][1]

    def _detect_nested(self, h4: pd.DataFrame, g: dict) -> dict | None:
        """Return the *current active* senior W3-(1) -> W3-(2) pair.

        v0015 froze the first qualifying post-W2 pivot forever.  That made an old
        BCH micro leg (~212.9 -> ~266 -> ~243) survive even after price had long
        accepted above ~266, while the actually active senior leg
        212.9 -> 317.7 -> 296.1 was ignored.

        v0017 uses four invariants:
        1) W3-(1) high must be a running/record high since parent W2;
        2) it must be senior enough in absolute and parent-degree terms;
        3) its pullback low must remain above parent W2 and satisfy nested retrace;
        4) there must be NO later COMPLETE4H close above that high.  Acceptance
           above means that candidate correction is complete/stale, so we continue
           looking for the next (later) record-high correction.

        Scanning newest -> oldest gives the same "current senior pullback" behavior
        used in the parquet reviews without letting lower-high bounces create labels.
        """
        parent_low = float(g["w2_low"])
        parent_ts = pd.Timestamp(g["w2_ts"])
        if parent_ts.tzinfo is None:
            parent_ts = parent_ts.tz_localize("UTC")
        else:
            parent_ts = parent_ts.tz_convert("UTC")

        after_w2 = h4[h4["timestamp"] > parent_ts].copy()
        if len(after_w2) < 8:
            return None

        high_mask = _pivot_mask(after_w2["high"], self.cfg.fourh_pivot_window, "high")
        pivots = list(after_w2.index[high_mask.fillna(False)])

        # A very recent active high can lack enough right-hand bars for the centered
        # pivot mask.  Admit the latest running maximum when at least two COMPLETE4H
        # candles already exist after it; the retrace test below still has to pass.
        running_max_idx = after_w2["high"].idxmax()
        if running_max_idx not in pivots:
            loc = after_w2.index.get_loc(running_max_idx)
            if len(after_w2) - loc - 1 >= 2:
                pivots.append(running_max_idx)

        candidates: list[dict] = []
        running_high = -math.inf
        for idx in after_w2.index:
            high = float(h4.at[idx, "high"])
            if high > running_high:
                running_high = high
            if idx not in pivots:
                continue
            # Lower highs inside a correction are not a new senior W3-(1).
            if high + max(1e-12, abs(running_high) * 1e-9) < running_high:
                continue
            if not self._nested_impulse_is_senior(high, g):
                continue
            candidates.append({"idx": idx, "high": high, "high_ts": h4.at[idx, "timestamp"]})

        if not candidates:
            return None

        for candidate in reversed(candidates):
            high = float(candidate["high"])
            high_ts = pd.Timestamp(candidate["high_ts"])
            if high_ts.tzinfo is None:
                high_ts = high_ts.tz_localize("UTC")
            else:
                high_ts = high_ts.tz_convert("UTC")
            tail = h4[h4["timestamp"] > high_ts]
            if len(tail) < 2:
                continue

            # If COMPLETE4H has already accepted above this W3-(1) high, its W3-(2)
            # is no longer the current correction.  Skip stale early subwaves.
            if not tail[tail["close"] > high].empty:
                continue
            if float(tail["low"].min()) <= parent_low:
                continue

            low_idx = tail["low"].idxmin()
            working_low = float(h4.at[low_idx, "low"])
            working_low_ts = h4.at[low_idx, "timestamp"]
            length = high - parent_low
            if length <= 0:
                continue
            retrace = (high - working_low) / length
            if not (self.cfg.min_nested_retrace <= retrace <= self.cfg.max_nested_retrace):
                continue
            return {
                "origin": parent_low,
                "high": high,
                "high_ts": high_ts,
                "low": working_low,
                "low_ts": working_low_ts,
                "retrace": retrace,
                "source": "H4_ACTIVE_RECORD_HIGH",
            }
        return None

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
        projection = _project_targets(
            wave_type,
            working_low=working_low,
            origin=origin,
            impulse_high=impulse_high,
            parent_w2_low=parent_w2_low,
            w3_1_high=w3_1_high,
        )
        if projection is None:
            # Publishing a senior setup with ambiguous projection anchors is worse
            # than returning no setup: targets are part of the trading decision.
            return None
        targets, target_origin, target_high, target_length, target_source = projection
        t1_upside = ((targets[0] / live) - 1) * 100 if targets and live > 0 else None
        rating = _rating(
            retrace, growth, strict_distance, status, liquidity_rank, top_n, wave_type, t1_upside
        )
        base, deep = _zones(working_low, impulse_high, strict_origin)
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
            targets=targets,
            target_origin=target_origin,
            target_impulse_high=target_high,
            target_impulse_length=target_length,
            target_source=target_source,
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
        previous.target_origin = None
        previous.target_impulse_high = None
        previous.target_impulse_length = None
        previous.target_source = None
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

        # A saved global W2 is allowed to progress into a nested W3-(2) on the *same*
        # symbol.  Accompaniment must not be frozen at the degree found on day one.
        # We still do not discover any new symbols here; this is only a same-parent
        # hierarchy upgrade after COMPLETE4H confirms W3-(1) and its correction.
        if previous.wave_type == "W2" and previous.working_low is not None:
            promoted = self.detect(snapshot, previous.liquidity_rank, top_n)
            if (
                promoted is not None
                and promoted.wave_type == "W3-(2)"
                and promoted.parent_w2_low is not None
                and math.isclose(
                    promoted.parent_w2_low, previous.working_low, rel_tol=0.003, abs_tol=1e-12
                )
            ):
                promoted.is_control = previous.is_control
                promoted.created_at = previous.created_at or promoted.created_at
                promoted.last_event = "PROMOTED W2 → W3-(2)"
                return promoted

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
        # Once a correction's projection high has been accepted above on COMPLETE4H,
        # that particular W2/W3-(2) is structurally complete.  Keep its historical
        # low/targets stable in accompaniment; do not re-anchor it to an unrelated
        # later pullback. Fresh Search will independently find the next active setup.
        structure_locked = False
        if previous.impulse_high is not None and previous.wave_type in {"W2", "W3-(2)"}:
            lock_from_raw = previous.working_low_ts or previous.impulse_high_ts
            if lock_from_raw:
                lock_from = pd.Timestamp(lock_from_raw)
                if lock_from.tzinfo is None:
                    lock_from = lock_from.tz_localize("UTC")
                else:
                    lock_from = lock_from.tz_convert("UTC")
                structure_locked = bool(
                    not h4[(h4["timestamp"] > lock_from) & (h4["close"] > float(previous.impulse_high))].empty
                )

        reanchored = False
        if not structure_locked and not new_h4.empty and previous.working_low is not None:
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
        projection = _project_targets(
            previous.wave_type,
            working_low=previous.working_low,
            origin=previous.origin,
            impulse_high=previous.impulse_high,
            parent_w2_low=previous.parent_w2_low,
            w3_1_high=previous.w3_1_high,
        )
        if projection is None:
            previous.status = "RECOUNT"
            self._clear_derived(previous, fib_status="TARGET ANCHORS INVALID — RECOUNT")
            previous.last_event = "TARGET ANCHORS INVALID — FULL SENIOR RECOUNT REQUIRED"
            previous.current_price = snapshot.live_price
            previous.updated_at = now
            return previous
        (
            previous.targets,
            previous.target_origin,
            previous.target_impulse_high,
            previous.target_impulse_length,
            previous.target_source,
        ) = projection
        previous.base_zone, previous.deep_zone = _zones(previous.working_low, high, strict)
        t1_upside = (
            (previous.targets[0] / previous.current_price - 1) * 100
            if previous.targets and previous.current_price and previous.current_price > 0
            else None
        )
        previous.rating = _rating(
            previous.retrace_depth or 0.0,
            previous.growth_from_low_pct,
            previous.strict_distance_pct,
            previous.fib_status,
            previous.liquidity_rank,
            top_n,
            previous.wave_type,
            t1_upside,
        )
        previous.status = _status_from_state(previous.growth_from_low_pct, previous.fib_status)  # type: ignore[assignment]
        if reanchored:
            previous.last_event = "RE-ANCHOR COMPLETE4H"
        elif structure_locked:
            previous.last_event = f"{previous.wave_type} LOCKED — PROJECTION HIGH ACCEPTED"
        else:
            previous.last_event = "UPDATED"
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
