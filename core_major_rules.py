"""Auditable, dated BTC/ETH reference structures. No executable rules for other assets."""

from __future__ import annotations

from decimal import Decimal
from itertools import pairwise

import pandas as pd

from core_recovery import recovery_prices
from core_senior import _atr, _pivot_mask
from data_integrity import DataIntegrityError

PROTOCOL = "majors-0025-20261005"
# These scenarios were supplied at this time. /walk cannot pretend they were known earlier.
KNOWN_AT = pd.Timestamp("2026-10-05T21:07:00Z")
HISTORY_START = pd.Timestamp("2026-06-04T00:00:00Z")
FULL_DAILY_START = pd.Timestamp("2010-01-01T00:00:00Z")
MULTIPLIERS = ("1", "1.618", "2.618", "4.236")
INVALID_TEXT = "OLD COUNT INVALID — FULL SENIOR RECOUNT REQUIRED."
# Price references identify the reviewed structure only. Production anchors are
# resolved in a time window on the SELECTED market and stored with provenance.
BTC_REFERENCE = (
    ("origin", "low", "2026-07-01T00:00Z", 57800.19),
    ("w1", "high", "2026-07-21T12:00Z", 66956.15),
    ("w2", "low", "2026-08-01T16:00Z", 62275.0),
    ("i1", "high", "2026-09-03T20:00Z", 82300.0),
    ("i2", "low", "2026-09-15T16:00Z", 74967.97),
    ("peak", "high", "2026-09-21T20:00Z", 87395.67),
    ("low", "low", "2026-09-28T12:00Z", 82563.0),
)
ETH_REFERENCE = (
    ("base1", "low", "2026-06-06T04:00Z", 1505.68),
    ("origin", "low", "2026-06-26T00:00Z", 1512.0),
    # 1849.54 belongs to June 15, BEFORE launch; chronological high is July 13.
    ("w1", "high", "2026-07-13T00:00Z", 1846.0),
    ("w2", "low", "2026-07-13T16:00Z", 1750.2),
    ("i2", "low", "2026-08-01T16:00Z", 1822.06),
    ("i3", "high", "2026-08-27T08:00Z", 2566.53),
    ("i4", "low", "2026-09-02T08:00Z", 2356.41),
    ("peak", "high", "2026-09-21T20:00Z", 2807.34),
    ("low", "low", "2026-09-24T08:00Z", 2600.15),
)


def projection(low: float, origin: float, high: float) -> list[float]:
    lo, length = Decimal(str(low)), Decimal(str(high)) - Decimal(str(origin))
    return [float(lo + Decimal(m) * length) for m in MULTIPLIERS]


def recovery(low: float, high: float) -> dict[str, float]:
    return recovery_prices(low, high)


def box_upper(a: dict, low: float) -> float:
    return float(
        Decimal(str(low))
        + Decimal(str(a["peak"]["price"]))
        - Decimal(str(a["i2"]["price"]))
    )


def point(row, kind: str, exchange: str) -> dict:
    return {
        "price": float(row[kind]),
        "timestamp": pd.Timestamp(row["timestamp"]).isoformat(),
        "kind": kind,
        "market": exchange,
        "timeframe": "COMPLETE4H",
    }


def validate_anchor_chronology(base: str, anchors: dict) -> None:
    """Check every reviewed AND derived vertex before any Fib/target arithmetic."""
    keys = (
        ("origin", "w1", "w2", "i1", "i2", "peak", "low")
        if base == "BTC"
        else ("base1", "origin", "w1", "w2", "i1", "i2", "i3", "i4", "peak", "low")
    )
    try:
        stamps = [pd.Timestamp(anchors[key]["timestamp"]) for key in keys]
        if any(pd.isna(t) or t.tzinfo is None for t in stamps) or any(
            x >= y for x, y in pairwise(stamps)
        ):
            raise ValueError("timestamps must strictly increase")
    except (KeyError, TypeError, ValueError) as exc:
        raise DataIntegrityError(
            f"{base}: invalid anchor chronology; no Fib/targets"
        ) from exc


def map_anchors(
    base: str, h4: pd.DataFrame, daily: pd.DataFrame, exchange: str
) -> dict:
    refs = BTC_REFERENCE if base == "BTC" else ETH_REFERENCE
    anchors = {}
    for name, kind, stamp, reference in refs:
        ts = pd.Timestamp(stamp)
        # Do not silently accept a partial reference window at a historical cutoff.
        left, right = ts - pd.Timedelta(hours=8), ts + pd.Timedelta(hours=8)
        if h4.timestamp.min() > left or h4.timestamp.max() < right:
            raise DataIntegrityError(
                f"{base}: missing COMPLETE4H anchor window {name} {ts.isoformat()}"
            )
        window = h4[h4.timestamp.between(left, right)]
        row = window.loc[
            window[kind].idxmin() if kind == "low" else window[kind].idxmax()
        ]
        measured = float(row[kind])
        if abs(measured / reference - 1) > 0.035:
            raise DataIntegrityError(
                f"{base}: reference structure not matched on {exchange}: {name}"
            )
        # FULL 1D supplies context, never prices for senior pivot replacement.
        day = daily[daily.timestamp.eq(pd.Timestamp(row.timestamp).floor("D"))]
        if day.empty or not (
            float(day.low.iloc[0]) <= measured <= float(day.high.iloc[0])
        ):
            raise DataIntegrityError(f"{base}: D1/COMPLETE4H disagreement at {name}")
        anchors[name] = point(row, kind, exchange)
        anchors[name]["binance_reference"] = reference
    stamps = [pd.Timestamp(anchors[x[0]]["timestamp"]) for x in refs]
    if any(x >= y for x, y in pairwise(stamps)):
        raise DataIntegrityError(
            f"{base}: unordered/ambiguous senior anchor timestamps"
        )
    if base == "ETH":
        start, end = (
            pd.Timestamp(anchors["w2"]["timestamp"]),
            pd.Timestamp(anchors["i2"]["timestamp"]),
        )
        window = h4[(h4.timestamp > start) & (h4.timestamp < end)]
        peaks = window[_pivot_mask(h4.high, 3, "high").loc[window.index].fillna(False)]
        if peaks.empty:
            raise DataIntegrityError("ETH: internal wave-(1) high is not derived")
        anchors["i1"] = point(peaks.loc[peaks.high.idxmax()], "high", exchange)
    validate_anchor_chronology(base, anchors)
    return anchors


def impulse_errors(points: list[dict], frame: pd.DataFrame | None = None) -> list[str]:
    """Normal, non-truncated impulse; equal origin/overlap boundaries stay valid."""
    if len(points) != 6:
        return ["FIVE_PIVOTS_NOT_ESTABLISHED"]
    p = [x["price"] for x in points]
    t = [pd.Timestamp(x["timestamp"]) for x in points]
    errors = []
    if any(x >= y for x, y in pairwise(t)):
        errors.append("CHRONOLOGY")
    if not (p[1] > p[0] and p[3] > p[1] and p[5] > p[3]):
        errors.append("MOTIVE_HIGHS")
    if p[2] < p[0] or p[2] >= p[1]:
        errors.append("W2_ORIGIN")
    if p[4] < p[1] or p[4] >= p[3]:
        errors.append("W4_OVERLAP")
    lengths = [p[1] - p[0], p[3] - p[2], p[5] - p[4]]
    if lengths[1] < min(lengths[0], lengths[2]):
        errors.append("W3_SHORTEST")
    if frame is not None:
        for i in (0, 2, 4):
            span = frame[(frame.timestamp > t[i]) & (frame.timestamp <= t[i + 1])]
            if not span.empty and (
                float(span.low.min()) < p[i] or float(span.high.max()) > p[i + 1]
            ):
                errors.append("MOTIVE_SEGMENT_EXTREMES")
        for i, boundary, rule in (
            (1, p[0], "W2_ORIGIN_WICK"),
            (3, p[1], "W4_OVERLAP_WICK"),
        ):
            span = frame[(frame.timestamp > t[i]) & (frame.timestamp <= t[i + 1])]
            if not span.empty and float(span.low.min()) < boundary:
                errors.append(rule)
            if not span.empty and (
                float(span.low.min()) < p[i + 1] or float(span.high.max()) > p[i]
            ):
                errors.append("CORRECTION_SEGMENT_EXTREMES")
    return errors


def clean_five(frame: pd.DataFrame, start: dict, end: dict) -> dict:
    """Use existing centered structural pivot masks at multiple meaningful scales.

    Retain alternating swings using an ATR/displacement floor. Never manufacture
    extra vertices to reach six. An unproved subdivision is UNKNOWN, not invalid.
    """
    span = frame[
        frame.timestamp.between(
            pd.Timestamp(start["timestamp"]), pd.Timestamp(end["timestamp"])
        )
    ]
    if len(span) < 20:
        return {
            "status": "UNPROVEN",
            "reason": "insufficient subdivision history",
            "points": [],
        }
    length = end["price"] - start["price"]
    atr = float(_atr(span).dropna().median())
    timeframe = (
        "1H"
        if span.timestamp.diff().dropna().min() == pd.Timedelta(hours=1)
        else "COMPLETE4H"
    )
    peaks = _pivot_mask(span.high, 3, "high").fillna(False)
    troughs = _pivot_mask(span.low, 3, "low").fillna(False)
    best = None
    for fraction in (0.06, 0.08, 0.10, 0.12, 0.16, 0.20):
        floor = max(length * fraction, atr * 1.5)
        turns = [dict(start)]
        for idx, row in span.iterrows():
            if pd.Timestamp(row.timestamp) <= pd.Timestamp(
                start["timestamp"]
            ) or pd.Timestamp(row.timestamp) >= pd.Timestamp(end["timestamp"]):
                continue
            kinds = [
                k for k, mask in (("high", peaks), ("low", troughs)) if mask.loc[idx]
            ]
            if len(kinds) != 1:
                continue  # OHLC cannot order both pivots in a single candle.
            kind = kinds[0]
            p = point(row, kind, start["market"])
            p["timeframe"] = timeframe
            last = turns[-1]
            if last["kind"] == kind:
                if len(turns) > 1 and (
                    (kind == "high" and p["price"] > last["price"])
                    or (kind == "low" and p["price"] < last["price"])
                ):
                    turns[-1] = p
            elif abs(p["price"] - last["price"]) >= floor:
                turns.append(p)
        if turns[-1]["kind"] == "high":
            turns[-1] = dict(end)
        else:
            turns.append(dict(end))
        if len(turns) != 6 or impulse_errors(turns, span):
            continue
        durations = [
            (
                pd.Timestamp(y["timestamp"]) - pd.Timestamp(x["timestamp"])
            ).total_seconds()
            / 3600
            for x, y in pairwise(turns)
        ]
        if min(durations) < 12 or max(durations) / min(durations) > 20:
            continue
        depths = [
            (turns[i]["price"] - turns[i + 1]["price"])
            / (turns[i]["price"] - turns[i - 1]["price"])
            for i in (1, 3)
        ]
        if not all(0.08 <= d <= 0.95 for d in depths):
            continue
        candidate = {
            "status": "PROVEN",
            "points": turns,
            "floor": floor,
            "duration_hours": durations,
            "retracements": depths,
        }
        if best is None or floor > best["floor"]:
            best = candidate
    return best or {
        "status": "UNPROVEN",
        "reason": "no clean five without micro pivots",
        "points": [],
    }


def cross_asset_context(
    eth_h4: pd.DataFrame, btc_h4: pd.DataFrame, exchange: str
) -> dict:
    # Divide synchronous closes only. Division of OHLC extrema would invent candles.
    joined = eth_h4[["timestamp", "close"]].merge(
        btc_h4[["timestamp", "close"]], on="timestamp", suffixes=("_eth", "_btc")
    )
    if len(joined) < 12:
        return {
            "status": "UNAVAILABLE",
            "score": 0.0,
            "market": exchange,
            "objective": [0.07, 0.08],
        }
    ratio = joined.close_eth / joined.close_btc
    recent, before = ratio.iloc[-6:], ratio.iloc[-12:-6]
    strengthening = bool(recent.max() > before.max() and recent.min() > before.min())
    value = float(ratio.iloc[-1])
    failed = value < 0.01766
    return {
        "status": "THESIS_FAILED"
        if failed
        else ("STRENGTHENING" if strengthening else "NOT_CONFIRMED"),
        "ratio": value,
        "timestamp": joined.timestamp.iloc[-1].isoformat(),
        "source": "same-market synchronous COMPLETE4H closes; derived ratio, not ETHBTC OHLC",
        "market": exchange,
        "score": 0.5 if strengthening and not failed else 0.0,
        "objective": [0.07, 0.08],
        "objective_is_conditional": True,
        "progress": ">0.04327"
        if value > 0.04327
        else (">0.0333" if value > 0.0333 else "below 0.0333"),
    }
