from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import pairwise
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
    # A senior W3-(1) may be volatile, but an isolated MEXC wick must not become
    # the projection anchor.  Manual parquet reviews use the structural swing,
    # not a single unconfirmed futures spike.
    max_isolated_upper_wick_pct: float = 0.055
    structural_high_peer_tolerance: float = 0.025
    structural_high_neighbor_bars: int = 2
    # Two adjacent 4H bars are still micro noise for our senior map.  Requiring
    # several COMPLETE4H bars before the high avoids the old BCH 266 -> 243
    # internal subwave stealing the W3-(1) label.
    min_nested_impulse_bars: int = 4
    max_w1_age_days: int = 180


def complete4h(hourly_closed: pd.DataFrame) -> pd.DataFrame:
    """Build COMPLETE4H only from the exact UTC hourly slots 00/01/02/03, 04/05/06/07, etc."""
    columns = ["timestamp", "open", "high", "low", "close", "volume", "n"]
    if hourly_closed.empty:
        return pd.DataFrame(columns=columns)
    df = hourly_closed.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    if df["timestamp"].duplicated().any():
        raise DataIntegrityError("1h: duplicate timestamp in COMPLETE4H input")
    df = df.sort_values("timestamp")

    # Four rows in one floor('4h') bucket are not enough by themselves: malformed or
    # offset timestamps (e.g. 00:30/01:30/02:30/03:30) must never become a senior C4H.
    aligned = (
        df["timestamp"].dt.minute.eq(0)
        & df["timestamp"].dt.second.eq(0)
        & df["timestamp"].dt.microsecond.eq(0)
        & df["timestamp"].dt.nanosecond.eq(0)
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
            suffix = f" · {holds}/3 C4H" if recent else ""
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


def _utc(value) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _origin_bucket(h4: pd.DataFrame, day, price: float):
    """Resolve a daily origin to the actual COMPLETE4H candle, never a nearby price."""
    day = _utc(day).floor("D")
    matches = h4[(h4["timestamp"] >= day) & (h4["timestamp"] < day + pd.Timedelta(days=1))]
    matches = matches[matches["low"].map(lambda x: math.isclose(float(x), price, rel_tol=1e-9, abs_tol=0.0))]
    return matches["timestamp"].iloc[0] if not matches.empty else None


def _recovery(h4: pd.DataFrame, fibs: dict[str, float], low_ts) -> tuple[str, int]:
    # A new working low resets the confirmation window. Earlier closes belong to
    # the old correction and must not manufacture 3/3 acceptance for a fresh low.
    rows = h4 if low_ts is None else h4[h4["timestamp"] >= _utc(low_ts)]
    return _fib_status(float(h4["close"].iloc[-1]), fibs, rows["close"].tail(3))


def _reached_targets(snapshot: MarketSnapshot, low_ts, targets: list[float]) -> list[int]:
    # Exclude the anchor bucket: OHLC cannot tell whether its high preceded its low.
    after = snapshot.hourly_closed
    if low_ts is None or after.empty:
        return []
    after = after[pd.to_datetime(after["timestamp"], utc=True) >= _utc(low_ts) + pd.Timedelta(hours=4)]
    high = float(after["high"].max()) if not after.empty else -math.inf
    if snapshot.live_price is not None:
        high = max(high, snapshot.live_price)
    return [i for i, target in enumerate(targets, 1) if high >= target]


def _projection_acceptance(h4: pd.DataFrame, high: float, low_ts) -> str | None:
    """First closing acceptance: the low of that same candle precedes its close."""
    if low_ts is None or h4.empty:
        return None
    accepted = h4[(h4["timestamp"] >= _utc(low_ts)) & (h4["close"] > high)]
    return _iso(accepted["timestamp"].iloc[0]) if not accepted.empty else None


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
    if not math.isfinite(lo) or not math.isfinite(hi) or lo <= 0 or hi <= lo:
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
    if not anchor_origin < float(working_low) < anchor_high:
        return None
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
            if "> .500" in fib_status or "> .5" in fib_status:
                score = min(score, 8.9)
        # Weak-level cap applies at EVERY persistence count. Otherwise a stronger
        # third close reduced the score from 8.8 to 8.4 (non-monotone recovery).
        if any(marker in fib_status for marker in ("> .618", "> .705", "> .786", "> .886", "> .950")):
            score = min(score, 8.4)

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
                days = (high_day_ts - origin_ts).days
                if days < 1 or days > 120:
                    continue
                high = float(daily.at[hi, "high"])
                # The endpoint of a rising impulse cannot be a lower high after
                # an already higher extreme inside that same impulse.
                if float(daily.loc[li:hi, "high"].max()) > high:
                    continue
                peak_day = h4[
                    (h4["timestamp"] >= high_day_ts)
                    & (h4["timestamp"] < high_day_ts + pd.Timedelta(days=1))
                ]
                peaks = peak_day[peak_day["high"] == high]
                if peaks.empty:
                    # Older daily peaks remain context for a W2 whose exact low IS
                    # visible on COMPLETE4H. Never synthesize four-hour candles.
                    if high_day_ts >= h4["timestamp"].iloc[0].floor("D") + pd.Timedelta(days=1):
                        continue
                    peak_ts = high_day_ts
                    context_only = True
                else:
                    peak_ts = peaks["timestamp"].iloc[0]
                    context_only = False
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

                # A W1 that already produced a qualifying W2 cannot be extended
                # retrospectively across that correction to a later W3 high.
                consumed = False
                for earlier in highs:
                    if not li < earlier < hi:
                        continue
                    prior_high = float(daily.at[earlier, "high"])
                    prior_len = prior_high - origin
                    between = daily.loc[earlier + 1:hi - 1]
                    if between.empty or prior_len <= 0 or prior_high / origin - 1 < self.cfg.min_global_impulse_pct:
                        continue
                    if atr > 0 and prior_len < self.cfg.min_global_atr_mult * atr:
                        continue
                    depth = (prior_high - float(between["low"].min())) / prior_len
                    if self.cfg.min_w2_retrace <= depth <= self.cfg.max_w2_retrace:
                        consumed = True
                        break
                if consumed:
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
                    # The breakout candle's LOW occurs before its closing acceptance.
                    correction = after[after["timestamp"] <= breakout_ts]
                    w2_locked = True
                if len(correction) < 2:
                    continue
                low_idx = correction["low"].idxmin()
                w2_low = float(h4.at[low_idx, "low"])
                w2_ts = h4.at[low_idx, "timestamp"]
                if context_only:
                    # Daily candles prove that the missing prefix contained neither
                    # a lower correction low nor an earlier closing breakout. Exact
                    # timing of the CURRENT W2 still comes solely from COMPLETE4H.
                    prefix = daily[(daily["timestamp"] >= high_day_ts)
                                   & (daily["timestamp"] < h4["timestamp"].iloc[0].floor("D") + pd.Timedelta(days=1))]
                    if prefix.empty or float(prefix["low"].min()) <= w2_low:
                        continue
                    # Daily close below the level does NOT exclude a hidden 4H
                    # closing breakout earlier that day. Daily HIGH must be <= it.
                    if (prefix["high"] > high).any():
                        continue
                retrace = (high - w2_low) / length
                if w2_low <= origin or not (self.cfg.min_w2_retrace <= retrace <= self.cfg.max_w2_retrace):
                    continue
                result.append(
                    {
                        "origin": origin,
                        "origin_ts": _origin_bucket(h4, origin_ts, origin) or origin_ts,
                        "high": high,
                        "high_ts": peak_ts,
                        "w2_low": w2_low,
                        "w2_ts": w2_ts,
                        "retrace": retrace,
                        "w2_locked": w2_locked,
                        "breakout_ts": breakout_ts,
                        "impulse_pct": impulse_pct,
                        "high_context_only": context_only,
                    }
                )
        return result

    @staticmethod
    def _same_level(a: float | None, b: float | None, *, rel: float = 1e-9) -> bool:
        if a is None or b is None:
            return False
        scale = max(abs(float(a)), abs(float(b)))
        if scale == 0:
            return a == b
        return abs(float(a) - float(b)) / scale <= rel

    def _structural_high_ok(self, h4: pd.DataFrame, idx: int) -> bool:
        """Return True when a pivot high is a structural swing, not one isolated wick.

        The v0017 target math itself was correct, but MEXC raw highs could replace
        the manually validated W3-(1) anchor with a later isolated futures spike.
        That inflated BCH/USOIL projection length even though the displayed wave
        label and working low looked plausible.

        A high is accepted when its upper wick is modest OR another nearby COMPLETE4H
        candle trades close to the same level.  This keeps real blow-off clusters but
        rejects one-candle anomalies.
        """
        try:
            pos = h4.index.get_loc(idx)
        except KeyError:
            return False
        row = h4.loc[idx]
        high = float(row["high"])
        body_top = max(float(row["open"]), float(row["close"]))
        if not math.isfinite(high) or high <= 0 or body_top <= 0:
            return False
        wick_pct = max(0.0, high / body_top - 1.0)
        if wick_pct <= self.cfg.max_isolated_upper_wick_pct:
            return True

        n = self.cfg.structural_high_neighbor_bars
        lo = max(0, pos - n)
        hi = min(len(h4), pos + n + 1)
        peers = h4.iloc[lo:hi].drop(index=idx, errors="ignore")
        if peers.empty:
            return False
        peer_high = float(peers["high"].max())
        return peer_high >= high * (1.0 - self.cfg.structural_high_peer_tolerance)

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
        """Promote a daily child W1/W2 geometry to senior W3-(1)/W3-(2).

        A child that already passed the full global-candidate filters is senior by
        construction, so v0018 no longer re-rejects it with the coarse nested
        amplitude ratio.  This is the GRAM failure from v0015-v0017.

        Crucially, the child's *own* origin/high/W2 low are preserved as projection
        anchors.  We do not rescan the whole future tail and accidentally replace
        them with an older parent or later MEXC wick.
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
            origin_ts = child.get("origin_ts")
            if origin_ts is None or _origin_bucket(h4, origin_ts, parent_low) != parent_ts:
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

            origin = float(child["origin"])
            high = float(child["high"])
            low = float(child["w2_low"])
            if high <= origin or low <= origin or low >= high:
                continue
            if not self._nested_impulse_is_senior(high, parent):
                continue

            # Full daily child geometry has already passed global amplitude/ATR
            # filters.  It is therefore a stronger degree proof than the old coarse
            # nested-parent ratio that rejected GRAM.
            retrace = (high - low) / (high - origin)
            if not (self.cfg.min_nested_retrace <= retrace <= self.cfg.max_nested_retrace):
                continue

            # Reject a child whose high is just one isolated MEXC wick.  Map the
            # timestamp back to COMPLETE4H when it is available.
            matches = h4.index[h4["timestamp"].eq(high_ts)].tolist()
            if matches and not self._structural_high_ok(h4, matches[0]):
                continue

            eligible.append({
                "origin": origin,
                "origin_ts": parent_ts,
                "high": high,
                "high_ts": high_ts,
                "low": low,
                "low_ts": low_ts,
                "retrace": retrace,
                "source": "GLOBAL_LINEAGE_ACTIVE",
            })

        if not eligible:
            return None
        # Same lifecycle as the primary path: later children cannot rewrite a
        # projection already confirmed for the same parent.
        eligible.sort(key=lambda c: pd.Timestamp(c["high_ts"]))
        return eligible[0]

    def _origin_is_prior_w2_h4(self, h4: pd.DataFrame, child: dict) -> bool:
        """Prove that a global-looking child origin is itself an older senior W2.

        This is intentionally an ancestry check, not a second full detector.  It is
        used only to decide whether a current daily W1/W2 geometry should be read as
        W3-(1)/W3-(2), which is the GRAM-like failure from v0015-v0017.
        """
        origin = float(child.get("origin", 0.0))
        raw_ts = child.get("origin_ts")
        if raw_ts is None or not math.isfinite(origin) or origin <= 0:
            return False
        origin_ts = _origin_bucket(h4, raw_ts, origin)
        if origin_ts is None:
            return False

        pre = h4[(h4["timestamp"] < origin_ts) & (h4["timestamp"] >= origin_ts - pd.Timedelta(days=120))].copy()
        if len(pre) < 8:
            return False

        # Prefer real pivot highs but always include the dominant pre-origin high as
        # a fallback; sparse/monotonic senior legs do not always create a centered
        # pivot in synthetic or newly listed data.
        high_mask = _pivot_mask(pre["high"], self.cfg.fourh_pivot_window, "high")
        highs = list(pre.index[high_mask.fillna(False)])
        dominant = pre["high"].idxmax()
        if dominant not in highs:
            highs.append(dominant)

        for hi in sorted(highs, reverse=True):
            high = float(h4.at[hi, "high"])
            high_ts = pd.Timestamp(h4.at[hi, "timestamp"])
            if high <= origin or (origin_ts - high_ts).days > 60:
                continue

            post = h4[(h4["timestamp"] > high_ts) & (h4["timestamp"] <= origin_ts)]
            if post.empty:
                continue
            post_low = float(post["low"].min())
            # The child origin must actually be the correction low, not merely some
            # later arbitrary daily low in the same broad range.
            if not self._same_level(post_low, origin):
                continue

            before = h4[(h4["timestamp"] < high_ts) & (h4["timestamp"] >= high_ts - pd.Timedelta(days=120))]
            if before.empty:
                continue
            prior_low = float(before["low"].min())
            if prior_low <= 0 or prior_low >= origin or high <= prior_low:
                continue
            impulse = high - prior_low
            if high / prior_low - 1.0 < self.cfg.min_global_impulse_pct:
                continue
            retrace = (high - origin) / impulse
            if self.cfg.min_w2_retrace <= retrace <= self.cfg.max_w2_retrace:
                return True
        return False

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

        # Current corrections precede historical, already accepted corrections.
        # Hierarchy priority applies WITHIN the same lifecycle, so an old nested
        # leg cannot hide a current global W2 merely by being named W3-(2).
        ranked_states: list[tuple[tuple[bool, float, float, float], WaveState]] = []
        for g in global_candidates:
            daily_parent = None
            # Direct COMPLETE4H hierarchy is authoritative.  Global-lineage is a
            # fallback for cases where the same nested leg also appears as a daily
            # W1/W2 candidate (GRAM-like geometry).
            nested = self._detect_nested(h4, g, daily=daily)
            if nested is None:
                nested = self._lineage_nested(h4, g, global_candidates)

            # A current global-looking W1/W2 can itself be the nested W3-(1)/W3-(2)
            # when its origin is proven to be an older senior W2.  This is the live
            # GRAM case that v0015-v0017 kept mislabelling as plain W2.
            if nested is None and not g.get("w2_locked", False):
                daily_parent = self._daily_parent_evidence(snapshot, h4, g)
            if nested is None and not g.get("w2_locked", False) and (
                self._origin_is_prior_w2_h4(h4, g) or daily_parent is not None
            ):
                high_matches = h4.index[h4["timestamp"].eq(pd.Timestamp(g["high_ts"]))].tolist()
                if not high_matches or self._structural_high_ok(h4, high_matches[0]):
                    child_len = float(g["high"]) - float(g["origin"])
                    if child_len > 0:
                        child_retrace = (float(g["high"]) - float(g["w2_low"])) / child_len
                        if self.cfg.min_nested_retrace <= child_retrace <= self.cfg.max_nested_retrace:
                            nested = {
                                "origin": float(g["origin"]),
                                "origin_ts": _origin_bucket(h4, g["origin_ts"], float(g["origin"])),
                                "high": float(g["high"]),
                                "high_ts": g["high_ts"],
                                "low": float(g["w2_low"]),
                                "low_ts": g["w2_ts"],
                                "retrace": child_retrace,
                                "source": "D1_PREDECESSOR_PARENT_W2" if daily_parent else "H4_INFERRED_PARENT_W2",
                            }
            if nested is not None:
                state = self._build_state(
                    snapshot=snapshot,
                    wave_type="W3-(2)",
                    origin=nested["origin"],
                    impulse_high=nested["high"],
                    working_low=nested["low"],
                    strict_origin=nested["origin"],
                    impulse_start_ts=nested.get("origin_ts", g["w2_ts"]),
                    impulse_high_ts=nested["high_ts"],
                    working_low_ts=nested["low_ts"],
                    retrace=nested["retrace"],
                    parent_w2_low=nested["origin"],
                    parent_w2_ts=nested.get("origin_ts", g["w2_ts"]),
                    w3_1_high=nested["high"],
                    w3_1_high_ts=nested["high_ts"],
                    h4=h4,
                    liquidity_rank=liquidity_rank,
                    top_n=top_n,
                    is_control=snapshot.symbol in {"XAU", "USOIL"},
                )
                if state is not None:
                    state.structure_evidence.update({
                        "route": nested["source"],
                        "daily_origin": g["origin"], "daily_origin_ts": _iso(g["origin_ts"]),
                        "daily_high": g["high"], "daily_high_ts": _iso(g["high_ts"]),
                        "daily_high_context_only": g.get("high_context_only", False),
                        "parent_low": g["w2_low"], "parent_low_ts": _iso(g["w2_ts"]),
                    })
                    if daily_parent:
                        state.structure_evidence["daily_parent"] = daily_parent
                    ranked_states.append(
                        ((not state.structure_evidence.get("projection_accepted_at"), 3.0,
                          pd.Timestamp(g["w2_ts"]).timestamp(), float(g.get("impulse_pct", 0.0))), state)
                    )
                # Also evaluate this parent's current W2. The nested candidate
                # can be a historical child of an earlier, already completed leg.

            # Once W1 high has accepted above, the old global W2 is no longer the
            # developing wave. Keep it out of fresh Search unless a nested W3-(2) can
            # be identified. This prevents late W3/W4 price action from becoming W2.
            if g.get("w2_locked", False):
                continue

            if g.get("high_context_only", False):
                # A daily-context parent can prove ancestry, but cannot itself be
                # published as a fresh C4H-anchored W2 with invented peak timing.
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
                state.structure_evidence.update({"route": "DAILY_W1_COMPLETE4H_W2", "daily_high_context_only": False})
                ranked_states.append(
                    ((not state.structure_evidence.get("projection_accepted_at"), 2.0,
                      pd.Timestamp(g["w2_ts"]).timestamp(), float(g.get("impulse_pct", 0.0))), state)
                )

        if not ranked_states:
            return None
        ranked_states.sort(key=lambda item: item[0], reverse=True)
        selected = ranked_states[0][1]
        selected = self._prefer_senior_parent(snapshot, h4, daily, selected, liquidity_rank, top_n)
        selected = self._guard_mature_impulse(h4, daily, selected, global_candidates)
        if selected.wave_type == "W4":
            # The parent-W1 overlap boundary is stricter than the former W3
            # origin. Apply it to incomplete H1/live wicks on the first detection.
            selected = self._track_mature_phase(selected, snapshot)
        if snapshot.history_evidence:
            selected.structure_evidence["history"] = dict(snapshot.history_evidence)
            selected.structure_evidence["ancestry_incomplete"] = snapshot.history_evidence.get("status") == "unavailable"
        return selected

    def _daily_parent_evidence(self, snapshot: MarketSnapshot, h4: pd.DataFrame, child: dict) -> dict | None:
        """Use verified predecessor D1 for ancestry, never fabricate older H4 bars.

        An old parent may precede the fresh-entry age limit. The ACTIVE child's
        three anchors must still be present in COMPLETE4H. The predecessor frame
        is supplied only by the validated same-exchange ticker-history adapter.
        """
        context = snapshot.daily_context
        if context is None or context.empty or snapshot.history_evidence.get("status") != "restored":
            return None
        origin = float(child["origin"])
        origin_ts = _origin_bucket(h4, child["origin_ts"], origin)
        if origin_ts is None:
            return None
        d1 = context.sort_values("timestamp").reset_index(drop=True).copy()
        d1["atr"] = _atr(d1)
        pre = d1[d1["timestamp"] < origin_ts.floor("D")]
        highs = pre.index[_pivot_mask(pre["high"], self.cfg.daily_pivot_window, "high").fillna(False)]
        for hi in reversed(highs.tolist()):
            high, high_ts = float(d1.at[hi, "high"]), d1.at[hi, "timestamp"]
            if high <= origin:
                continue
            correction = d1[(d1["timestamp"] > high_ts) & (d1["timestamp"] <= origin_ts.floor("D"))]
            if correction.empty or not self._same_level(float(correction["low"].min()), origin):
                continue
            # Even a hidden 4H closing breakout would require the day's HIGH to
            # exceed the old peak. Do not infer an unbroken correction otherwise.
            if (correction["high"] > high).any():
                continue
            before = d1[(d1["timestamp"] < high_ts) & (d1["timestamp"] >= high_ts - pd.Timedelta(days=120))]
            if before.empty:
                continue
            li = before["low"].idxmin()
            root, root_ts = float(d1.at[li, "low"]), d1.at[li, "timestamp"]
            length = high - root
            if not 0 < root < origin or high / root - 1 < self.cfg.min_global_impulse_pct:
                continue
            atr = float(d1.at[li, "atr"])
            if math.isfinite(atr) and length < self.cfg.min_global_atr_mult * atr:
                continue
            if float(d1.loc[li:hi, "high"].max()) > high:
                continue
            retrace = (high - origin) / length
            if self.cfg.min_w2_retrace <= retrace <= self.cfg.max_w2_retrace:
                return {"origin": root, "origin_day": _iso(root_ts), "high": high,
                        "high_day": _iso(high_ts), "w2_low": origin, "w2_ts": _iso(origin_ts),
                        "retrace": retrace, "timeframe": "1D_CONTEXT_ONLY"}
        return None

    def _prefer_senior_parent(self, snapshot: MarketSnapshot, h4: pd.DataFrame,
                              daily: pd.DataFrame, state: WaveState,
                              liquidity_rank: int | None, top_n: int) -> WaveState:
        """Keep the outer intact D1 parent; an inner H4 turn cannot lower degree.

        Fresh selection may have lost an older W2 at the left H1 boundary. Prove
        the outer W1/W2 in closed daily context, then require the actual child
        origin on COMPLETE4H. Never invent its timestamp from a daily low. The
        current selected high/low remain unchanged; accepted projections stay
        locked. All candidates use the same rules, without symbol constants.
        """
        if state.structure_evidence.get("projection_accepted_at") or state.targets_hit:
            return state
        if state.origin is None or state.impulse_high is None or state.working_low is None:
            return state
        d = daily.sort_values("timestamp").reset_index(drop=True).copy()
        d["atr"] = _atr(d)
        lows = d.index[_pivot_mask(d["low"], self.cfg.daily_pivot_window, "low").fillna(False)]
        highs = d.index[_pivot_mask(d["high"], self.cfg.daily_pivot_window, "high").fillna(False)]
        end = _utc(state.impulse_high_ts)
        candidates = []
        for li in lows:
            root, root_day = float(d.at[li, "low"]), d.at[li, "timestamp"]
            if root >= state.origin or (d["timestamp"].iloc[-1] - root_day).days > self.cfg.max_w1_age_days:
                continue
            for hi in highs:
                peak, peak_day = float(d.at[hi, "high"]), d.at[hi, "timestamp"]
                if hi <= li or peak_day >= end.floor("D") or (peak_day - root_day).days > 120:
                    continue
                length = peak - root
                if peak / root - 1 < self.cfg.min_global_impulse_pct:
                    continue
                atr = float(d.at[li, "atr"])
                if math.isfinite(atr) and length < self.cfg.min_global_atr_mult * atr:
                    continue
                if float(d.loc[li:hi, "low"].min()) < root or float(d.loc[li:hi, "high"].max()) > peak:
                    continue
                after = d[(d["timestamp"] > peak_day) & (d["timestamp"] < end.floor("D"))]
                accepted = after[after["close"] > peak]
                if accepted.empty:
                    continue
                correction = after[after["timestamp"] <= accepted["timestamp"].iloc[0]]
                ci = correction["low"].idxmin()
                child_origin, child_day = float(d.at[ci, "low"]), d.at[ci, "timestamp"]
                before_child = d[(d["timestamp"] > peak_day) & (d["timestamp"] < child_day)]
                if (before_child["high"] > peak).any():
                    # The parent impulse continued to a higher wick before W2;
                    # its earlier daily pivot cannot stand in for the endpoint.
                    continue
                if not root < child_origin < state.origin or child_origin >= state.working_low:
                    continue
                retrace = (peak - child_origin) / length
                if not self.cfg.min_w2_retrace <= retrace <= self.cfg.max_w2_retrace:
                    continue
                # The prospective older W2 must remain strictly intact through
                # today, and the current projection high must be its record high.
                child_days = d[d["timestamp"] >= child_day]
                if float(child_days["low"].min()) < child_origin:
                    continue
                to_high = child_days[child_days["timestamp"] <= end.floor("D")]
                if float(to_high["high"].max()) > state.impulse_high:
                    continue
                parent = {"origin": root, "high": peak, "w2_low": child_origin}
                if not self._nested_impulse_is_senior(state.impulse_high, parent):
                    continue
                child_retrace = (state.impulse_high - state.working_low) / (state.impulse_high - child_origin)
                if not self.cfg.min_nested_retrace <= child_retrace <= self.cfg.max_nested_retrace:
                    continue
                origin_ts = _origin_bucket(h4, child_day, child_origin)
                # If parent high is covered, verify W2 against the exact first
                # COMPLETE4H acceptance, including that candle's low.
                parent_peaks = h4[(h4["timestamp"] >= peak_day) & (h4["timestamp"] < peak_day + pd.Timedelta(days=1)) & h4["high"].eq(peak)]
                if not parent_peaks.empty:
                    parent_high_ts = parent_peaks["timestamp"].iloc[0]
                    tail = h4[(h4["timestamp"] > parent_high_ts) & (h4["timestamp"] < end)]
                    closes = tail[tail["close"] > peak]
                    if closes.empty:
                        continue
                    parent_correction = tail[tail["timestamp"] <= closes["timestamp"].iloc[0]]
                    if not self._same_level(float(parent_correction["low"].min()), child_origin):
                        continue
                else:
                    # A daily HIGH above the peak before the W2 day could hide
                    # an earlier 4H acceptance. Do not infer through that ambiguity.
                    prefix = d[(d["timestamp"] > peak_day) & (d["timestamp"] < child_day)]
                    if (prefix["high"] > peak).any():
                        continue
                    parent_high_ts = None
                evidence = {"origin": root, "origin_day": _iso(root_day), "high": peak,
                            "high_day": _iso(peak_day), "high_ts": _iso(parent_high_ts),
                            "w2_low": child_origin, "w2_day": _iso(child_day),
                            "w2_ts": _iso(origin_ts), "retrace": retrace}
                # Earlier intact parent defines the senior degree. Smaller
                # internal 212/239-type corrections remain diagnostic substructure.
                candidates.append((root_day, peak_day, child_day, evidence, child_retrace))
        if not candidates:
            return state
        candidates.sort(key=lambda x: (x[0], x[1], x[2]))
        _, _, _, parent, child_retrace = candidates[0]
        if parent["w2_ts"] is None:
            state.structure_evidence["senior_parent"] = parent
            state.structure_evidence["senior_history_incomplete"] = True
            state.status = "DATA_INCOMPLETE"
            self._clear_derived(state, fib_status="OLDER SENIOR W2 NEEDS COMPLETE4H HISTORY")
            state.last_event = "SENIOR DEGREE UNRESOLVED — EXTEND 1H HISTORY"
            return state
        replacement = self._build_state(
            snapshot=snapshot, wave_type="W3-(2)", origin=parent["w2_low"],
            impulse_high=state.impulse_high, working_low=state.working_low,
            strict_origin=parent["w2_low"], impulse_start_ts=parent["w2_ts"],
            impulse_high_ts=state.impulse_high_ts, working_low_ts=state.working_low_ts,
            retrace=child_retrace, parent_w2_low=parent["w2_low"], parent_w2_ts=parent["w2_ts"],
            w3_1_high=state.impulse_high, w3_1_high_ts=state.impulse_high_ts,
            h4=h4, liquidity_rank=liquidity_rank, top_n=top_n, is_control=state.is_control,
        )
        if replacement is None:
            return state
        replacement.structure_evidence.update({"route": "OUTER_D1_PARENT_COMPLETE4H_CHILD",
            "senior_parent": parent, "inner_candidate": {"origin": state.origin,
            "origin_ts": state.impulse_start_ts, "wave_type": state.wave_type}})
        return replacement

    def _guard_mature_impulse(self, h4: pd.DataFrame, daily: pd.DataFrame,
                             state: WaveState, parents: list[dict]) -> WaveState:
        """A five-leg advance after senior W2 is not proven to be only W3-(1).

        Reuse the existing D1/COMPLETE4H swing and amplitude rules. The first two
        corrections are frozen at their first closing breakouts. Their chronology,
        non-overlap and third-wave length must support an impulse before this guard
        suppresses fresh W2/W3-(2) projections. An extension can have the same shape:
        report W4 as a CANDIDATE, never a unique/confirmed Elliott count or W5 targets.
        """
        if state.status in {"DATA_INCOMPLETE", "INVALID", "RECOUNT"}:
            return state
        if state.wave_type != "W3-(2)" or not state.impulse_start_ts or not state.impulse_high_ts:
            return state
        parent = state.structure_evidence.get("senior_parent") or state.structure_evidence.get("daily_parent")
        if not parent:
            parent = next((g for g in parents
                           if self._same_level(g["w2_low"], state.origin)
                           and _utc(g["w2_ts"]) == _utc(state.impulse_start_ts)
                           and g["origin"] < state.origin), None)
        if not parent or parent.get("w2_ts") is None:
            return state
        evidence = self._mature_impulse_evidence(h4, daily, state, parent)
        if evidence is None:
            return state
        state.wave_type = "W4"
        state.status = "PHASE_UNCERTAIN"
        state.structure_evidence["mature_impulse"] = evidence
        state.structure_evidence.pop("projection_accepted_at", None)
        # The former W3-(1) high is the end of a potentially complete W3, not an
        # anchor for another W3 extension. Its internal names must not survive.
        state.w3_1_high = state.w3_1_high_ts = None
        state.strict_origin = float(parent["high"])
        self._clear_derived(state, fib_status="СТАДИЯ ТРЕБУЕТ ПРОВЕРКИ")
        state.last_event = "POSSIBLE W4 AFTER FIVE SENIOR LEGS — W2/W3-(2) TARGETS WITHHELD"
        return state

    def _mature_impulse_evidence(self, h4: pd.DataFrame, daily: pd.DataFrame,
                               state: WaveState, parent: dict) -> dict | None:
        start, end = _utc(state.impulse_start_ts), _utc(state.impulse_high_ts)
        if not (self._same_level(parent["w2_low"], state.origin)
                and _utc(parent["w2_ts"]) == start):
            return None
        # Parent ancestry alone cannot supply missing intraday subdivision.
        span = h4[(h4["timestamp"] >= start) & (h4["timestamp"] <= end)]
        if span.empty or not span["timestamp"].eq(start).any():
            return None
        first = self._detect_nested(span, parent, daily=daily)
        if first is None or _utc(first["low_ts"]) >= end:
            return None
        # Wave (3) must exceed (1); lower highs within (2) are not new impulse legs.
        next_parent = {"origin": first["origin"], "high": first["high"],
                       "w2_low": first["low"], "w2_ts": first["low_ts"],
                       "phase_previous_high": first["high"]}
        third = self._detect_nested(span, next_parent, daily=daily)
        if third is None:
            return None
        points = [(state.origin, start), (first["high"], first["high_ts"]),
                  (first["low"], first["low_ts"]), (third["high"], third["high_ts"]),
                  (third["low"], third["low_ts"]), (state.impulse_high, end)]
        if not all(_utc(a[1]) < _utc(b[1]) for a, b in pairwise(points)):
            return None
        p0, p1, p2, p3, p4, p5 = (float(p[0]) for p in points)
        if not (p0 < p2 < p1 < p4 < p3 < p5):
            return None  # (2) intact, (4) no overlap, no truncated (5) inference
        lengths = [p1 - p0, p3 - p2, p5 - p4]
        if lengths[1] < min(lengths[0], lengths[2]):
            return None  # wave (3) cannot be the shortest motive leg
        # Apply the same senior-degree floor to (5) and demand structural support
        # on closed D1. Merely counting five noisy H4 turns cannot advance degree.
        fifth_parent = {"origin": p2, "high": p3, "w2_low": p4}
        if not self._nested_impulse_is_senior(p5, fifth_parent):
            return None
        if len(span[span["timestamp"] > _utc(third["low_ts"])]) < self.cfg.min_nested_impulse_bars:
            return None
        daily_peaks = daily[_pivot_mask(daily["high"], self.cfg.daily_pivot_window, "high").fillna(False)]
        if not any(_utc(row.timestamp).floor("D") == end.floor("D")
                   and self._same_level(row.high, p5) for row in daily_peaks.itertuples()):
            return None
        tail = h4[h4["timestamp"] > end]
        if tail.empty or state.working_low_ts is None:
            return None
        # Only an active post-(5) correction above the OUTER W1 can be a W4.
        # New record highs or overlap require a different count, not this label.
        if (tail["high"] > p5).any() or float(tail["low"].min()) <= float(parent["high"]):
            return None
        return {"phase": "W4_CANDIDATE", "certainty": "AMBIGUOUS_DEGREE",
                "parent": {k: (_iso(v) if isinstance(v, (pd.Timestamp, datetime)) else v)
                           for k, v in parent.items()},
                "subwaves": [{"point": i, "price": float(price), "timestamp": _iso(ts)}
                             for i, (price, ts) in enumerate(points)],
                "motive_lengths": lengths, "outer_w1_high": float(parent["high"]),
                "reason": "Five senior legs after W2; W3 extension and completed W3 cannot be distinguished uniquely"}

    def _detect_nested(self, h4: pd.DataFrame, g: dict, daily: pd.DataFrame | None = None) -> dict | None:
        """Return the senior W3-(1) -> W3-(2) map with frozen impulse anchors.

        The decisive v0018 change is lifecycle ordering.  W3-(1) is the *first
        senior structural impulse* after parent W2 that produces a qualifying
        W3-(2) correction.  Once that correction exists, later W3 continuation
        highs must NOT rewrite W3-(1) and inflate T1/T2/T3/T4.

        This is exactly what v0017 still got wrong on live MEXC: BCH/USOIL kept the
        right working-low area but a later high replaced the projection high.
        Isolated futures wicks are filtered as an additional guard.
        """
        parent_low = float(g["w2_low"])
        parent_ts = pd.Timestamp(g["w2_ts"])
        if parent_ts.tzinfo is None:
            parent_ts = parent_ts.tz_localize("UTC")
        else:
            parent_ts = parent_ts.tz_convert("UTC")

        after_w2 = h4[h4["timestamp"] > parent_ts].copy()
        if len(after_w2) < max(8, self.cfg.min_nested_impulse_bars + 2):
            return None

        high_mask = _pivot_mask(after_w2["high"], self.cfg.fourh_pivot_window, "high")
        pivots = list(after_w2.index[high_mask.fillna(False)])

        # A recent high may not yet have enough right-hand bars for centered pivot.
        running_max_idx = after_w2["high"].idxmax()
        if running_max_idx not in pivots:
            loc = after_w2.index.get_loc(running_max_idx)
            if len(after_w2) - loc - 1 >= 2:
                pivots.append(running_max_idx)

        candidates: list[dict] = []
        running_high = -math.inf
        first_idx = after_w2.index[0]
        first_pos = h4.index.get_loc(first_idx)
        for idx in after_w2.index:
            high = float(h4.at[idx, "high"])
            if high > running_high:
                running_high = high
            if idx not in pivots:
                continue
            if high <= g.get("phase_previous_high", -math.inf):
                continue
            # A lower high inside an existing correction cannot start senior W3-(1).
            if high + max(1e-12, abs(running_high) * 1e-9) < running_high:
                continue
            bars_from_parent = h4.index.get_loc(idx) - first_pos + 1
            if bars_from_parent < self.cfg.min_nested_impulse_bars:
                continue
            if not self._nested_impulse_is_senior(high, g):
                continue
            if not self._structural_high_ok(h4, idx):
                continue
            candidates.append({"idx": idx, "high": high, "high_ts": h4.at[idx, "timestamp"]})

        if not candidates:
            return None

        if daily is not None and not daily.empty:
            # A local 4H pivot is insufficient evidence of senior degree. Use the
            # same closed daily context/pivot window as the parent W1 detector.
            mask = _pivot_mask(daily["high"], self.cfg.daily_pivot_window, "high")
            senior_peaks = daily.loc[mask.fillna(False), ["timestamp", "high"]]
            candidates = [c for c in candidates if any(
                _utc(row.timestamp).floor("D") == _utc(c["high_ts"]).floor("D")
                and self._same_level(float(row.high), c["high"])
                for row in senior_peaks.itertuples()
            )]

        # IMPORTANT: chronological order.  The first senior impulse that actually
        # produces a valid correction owns the W3-(1) anchor.  Later highs belong to
        # the continuation and cannot retarget the already established W3-(2).
        for candidate in candidates:
            high = float(candidate["high"])
            high_ts = pd.Timestamp(candidate["high_ts"])
            if high_ts.tzinfo is None:
                high_ts = high_ts.tz_localize("UTC")
            else:
                high_ts = high_ts.tz_convert("UTC")
            tail = h4[h4["timestamp"] > high_ts]
            if len(tail) < 2:
                continue

            accepted = tail[tail["close"] > high]
            correction = tail if accepted.empty else tail[tail["timestamp"] <= accepted["timestamp"].iloc[0]]
            if len(correction) < 2:
                continue
            if float(correction["low"].min()) <= parent_low:
                continue

            low_idx = correction["low"].idxmin()
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
                "origin_ts": parent_ts,
                "high": high,
                "high_ts": high_ts,
                "low": working_low,
                "low_ts": working_low_ts,
                "retrace": retrace,
                "source": "H4_FROZEN_FIRST_SENIOR_W3_1",
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
            cut_from = _utc(impulse_start_ts)
            if cut_from.tzinfo is None:
                cut_from = cut_from.tz_localize("UTC")
            relevant_h1 = h1[h1["timestamp"] >= cut_from]
            if not relevant_h1.empty and float(relevant_h1["low"].min()) < strict_origin:
                return None
        if snapshot.live_low is not None and snapshot.live_low < strict_origin:
            return None
        if working_low <= strict_origin:
            return None

        fibs = _fib_prices(origin, impulse_high)
        last_close = float(h4["close"].iloc[-1])
        status, _ = _recovery(h4, fibs, working_low_ts)
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
        # Fib, strict invalidation and targets must describe the SAME impulse.
        if not (self._same_level(origin, target_origin) and self._same_level(strict_origin, target_origin)
                and self._same_level(impulse_high, target_high)):
            return None
        targets_hit = _reached_targets(snapshot, working_low_ts, targets)
        accepted_at = _projection_acceptance(h4, impulse_high, working_low_ts)
        t1_upside = 0.0 if 1 in targets_hit else (((targets[0] / live) - 1) * 100 if targets and live > 0 else None)
        rating = _rating(
            retrace, growth, strict_distance, status, liquidity_rank, top_n, wave_type, t1_upside
        )
        base, deep = _zones(working_low, impulse_high, strict_origin)
        now = datetime.now(timezone.utc).isoformat()
        return WaveState(
            symbol=snapshot.symbol,
            exchange=snapshot.exchange,
            wave_type=wave_type,  # type: ignore[arg-type]
            status="EXTENDED" if targets_hit or accepted_at else _status_from_state(growth, status),  # type: ignore[arg-type]
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
            targets_hit=targets_hit,
            structure_evidence={"projection_accepted_at": accepted_at},
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
        previous.targets_hit = []
        previous.target_origin = None
        previous.target_impulse_high = None
        previous.target_impulse_length = None
        previous.target_source = None
        previous.base_zone = None
        previous.deep_zone = None
        previous.growth_from_low_pct = None
        previous.strict_distance_pct = None
        previous.rating = 0.0

    def _track_mature_phase(self, previous: WaveState, snapshot: MarketSnapshot) -> WaveState:
        """Retain observed degree when a rolling history window loses its origin."""
        evidence = previous.structure_evidence["mature_impulse"]
        h4 = complete4h(snapshot.hourly_closed)
        seen = _utc(previous.last_complete4h_bucket) if previous.last_complete4h_bucket else None
        if not h4.empty and seen is not None and h4["timestamp"].iloc[-1] < seen:
            raise DataIntegrityError("tracking COMPLETE4H checkpoint moved backwards")
        h1 = snapshot.hourly_closed.copy()
        h1["timestamp"] = pd.to_datetime(h1["timestamp"], utc=True)
        cut = seen + pd.Timedelta(hours=4) if seen is not None else _utc(previous.impulse_high_ts) + pd.Timedelta(hours=4)
        fresh_h1 = h1[h1["timestamp"] >= cut]
        boundary = float(evidence["outer_w1_high"])
        overlap = (not fresh_h1.empty and float(fresh_h1["low"].min()) <= boundary)
        overlap = overlap or (snapshot.live_low is not None and snapshot.live_low <= boundary)
        previous.current_price = snapshot.live_price or previous.current_price
        previous.updated_at = datetime.now(timezone.utc).isoformat()
        previous.detector_version = "0022"
        self._clear_derived(previous, fib_status="СТАДИЯ ТРЕБУЕТ ПРОВЕРКИ")
        if overlap:
            previous.status = "RECOUNT"
            previous.fib_status = "W4 OVERLAPS W1 — RECOUNT"
            previous.last_event = "W4 CANDIDATE INVALIDATED — FULL SENIOR RECOUNT REQUIRED"
            return previous
        previous.status = "PHASE_UNCERTAIN"
        if h4.empty:
            previous.last_event = "INCOMPLETE — RETAINED MATURE PHASE"
            return previous
        fresh = h4[h4["timestamp"] >= cut]
        accepted_at = evidence.get("next_high_accepted_at")
        accepted = fresh[fresh["close"] > previous.impulse_high]
        if accepted_at is None and not accepted.empty:
            accepted_at = _iso(accepted["timestamp"].iloc[0])
            evidence["next_high_accepted_at"] = accepted_at
            evidence["phase"] = "AFTER_W4_CANDIDATE"
        eligible = fresh if accepted_at is None else fresh[fresh["timestamp"] <= _utc(accepted_at)]
        if not eligible.empty:
            idx = eligible["low"].idxmin()
            if float(eligible.at[idx, "low"]) < previous.working_low:
                previous.working_low = float(eligible.at[idx, "low"])
                previous.working_low_ts = _iso(eligible.at[idx, "timestamp"])
        previous.last_complete4h_bucket = _iso(h4["timestamp"].iloc[-1])
        previous.last_complete4h_close = float(h4["close"].iloc[-1])
        previous.last_event = "MATURE PHASE RETAINED — NO W2/W3-(2) PROJECTION"
        return previous

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

        if previous.wave_type == "W4" and previous.structure_evidence.get("mature_impulse"):
            return self._track_mature_phase(previous, snapshot)

        # Controls with no setup may be re-scanned because XAU/USOIL are permanent controls.
        if previous.wave_type == "CONTROL" or previous.status == "NO_SETUP":
            found = self.detect(snapshot, previous.liquidity_rank, top_n)
            if found:
                found.is_control = previous.is_control
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

        ancestry_restored = (
            previous.structure_evidence.get("ancestry_incomplete")
            and snapshot.history_evidence.get("status") == "restored"
        )
        senior_history_restored = previous.structure_evidence.get("senior_history_incomplete", False)
        if previous.detector_version != "0022" or ancestry_restored or senior_history_restored:
            rebuilt = self.detect(snapshot, previous.liquidity_rank, top_n)
            if rebuilt is not None:
                rebuilt.is_control = previous.is_control
                rebuilt.created_at = previous.created_at or rebuilt.created_at
                rebuilt.last_event = (
                    "HISTORY RESTORED — SAME-SYMBOL ANCESTRY RECOUNT" if ancestry_restored
                    else "V0022 SAME-SYMBOL RECOUNT — LEGACY ANCHORS REPLACED"
                )
                return rebuilt
            previous.status = "RECOUNT"
            self._clear_derived(previous, fib_status="V0022 RECOUNT REQUIRED")
            previous.last_event = "V0022 — LEGACY ANCHORS NOT CONFIRMED"
            previous.updated_at = now
            return previous
        new_h4 = h4 if last_seen is None else h4[h4["timestamp"] > last_seen]
        if not new_h4.empty and float(new_h4["low"].min()) < strict:
            previous.status = "INVALID"
            self._clear_derived(previous, fib_status="STRICT ORIGIN BROKEN")
            previous.last_event = "OLD COUNT INVALID — FULL SENIOR RECOUNT REQUIRED"
            previous.current_price = snapshot.live_price
            previous.updated_at = now
            return previous
        if not new_h4.empty:
            phase = self.detect(snapshot, previous.liquidity_rank, top_n)
            if phase is not None and phase.wave_type == "W4":
                parent = phase.structure_evidence["mature_impulse"]["parent"]
                same_cycle = (
                    previous.wave_type == "W3-(2)"
                    and self._same_level(previous.origin, phase.origin)
                    and previous.impulse_start_ts == phase.impulse_start_ts
                ) or (
                    previous.wave_type == "W2"
                    and self._same_level(previous.origin, parent["origin"])
                    and self._same_level(previous.impulse_high, parent["high"])
                    and self._same_level(previous.working_low, phase.origin)
                    and previous.working_low_ts == phase.impulse_start_ts
                )
                if same_cycle:
                    phase.is_control = previous.is_control
                    phase.created_at = previous.created_at or phase.created_at
                    phase.last_event = "SAME CYCLE ADVANCED — POSSIBLE W4; OLD TARGETS WITHHELD"
                    return phase
        # Once a correction's projection high has been accepted above on COMPLETE4H,
        # that particular W2/W3-(2) is structurally complete.  Keep its historical
        # low/targets stable in accompaniment; do not re-anchor it to an unrelated
        # later pullback. Fresh Search will independently find the next active setup.
        structure_locked = False
        eligible_h4 = new_h4
        if previous.impulse_high is not None and previous.wave_type in {"W2", "W3-(2)"}:
            lock_from_raw = previous.working_low_ts or previous.impulse_high_ts
            if lock_from_raw:
                accepted_at = previous.structure_evidence.get("projection_accepted_at")
                observed_at = _projection_acceptance(h4, float(previous.impulse_high), lock_from_raw)
                if observed_at and (not accepted_at or _utc(observed_at) < _utc(accepted_at)):
                    accepted_at = observed_at
                if accepted_at:
                    structure_locked = True
                    previous.structure_evidence["projection_accepted_at"] = accepted_at
                    eligible_h4 = new_h4[new_h4["timestamp"] <= _utc(accepted_at)]

        reanchored = False
        if not eligible_h4.empty and previous.working_low is not None:
            idx = eligible_h4["low"].idxmin()
            candidate_low = float(h4.at[idx, "low"])
            if strict < candidate_low < previous.working_low:
                previous.working_low = candidate_low
                previous.working_low_ts = _iso(h4.at[idx, "timestamp"])
                reanchored = True

        # Promote only AFTER all earlier lows in this batch have been applied.
        # Identity includes time and price; revisiting the same price is not lineage.
        if previous.wave_type == "W2" and previous.working_low is not None:
            promoted = self.detect(snapshot, previous.liquidity_rank, top_n)
            if (
                promoted is not None and promoted.wave_type == "W3-(2)"
                and self._same_level(promoted.parent_w2_low, previous.working_low)
                and promoted.parent_w2_ts is not None and previous.working_low_ts is not None
                and _utc(promoted.parent_w2_ts) == _utc(previous.working_low_ts)
            ):
                promoted.is_control = previous.is_control
                promoted.created_at = previous.created_at or promoted.created_at
                promoted.last_event = "PROMOTED W2 → W3-(2)"
                return promoted

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
        fib_status, _ = _recovery(h4, previous.fibs, previous.working_low_ts)
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
        if projection is None or not (
            self._same_level(origin, projection[1]) and self._same_level(strict, projection[1])
            and self._same_level(high, projection[2])
        ):
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
        hits = _reached_targets(snapshot, previous.working_low_ts, previous.targets)
        previous.targets_hit = sorted(set(hits) | (set() if reanchored else set(previous.targets_hit)))
        previous.base_zone, previous.deep_zone = _zones(previous.working_low, high, strict)
        t1_upside = 0.0 if 1 in previous.targets_hit else (
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
        previous.status = "EXTENDED" if previous.targets_hit or structure_locked else _status_from_state(previous.growth_from_low_pct, previous.fib_status)  # type: ignore[assignment]
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
