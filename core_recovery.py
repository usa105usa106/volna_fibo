"""Recovery of a correction, independent of impulse-origin retracement depth.

All callers supply CLOSED COMPLETE4H candles. No persisted confirmation counter
or live price participates in this calculation.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from decimal import Decimal

import pandas as pd

from data_integrity import DataIntegrityError

RATIOS = ("0.236", "0.382", "0.500", "0.618", "0.705", "0.786", "0.886")
SEMANTICS = "recovery-low-to-high-consecutive-c4h-v0025"


def recovery_prices(low: float, high: float) -> dict[str, float]:
    if not all(math.isfinite(v) and v > 0 for v in (low, high)) or low >= high:
        raise DataIntegrityError("recovery: expected 0 < working low < impulse high")
    lo, length = Decimal(str(low)), Decimal(str(high)) - Decimal(str(low))
    return {"R" + r[2:]: float(lo + Decimal(r) * length) for r in RATIOS}


def status_from_closes(
    close: float, fibs: dict[str, float], closes: Iterable[float]
) -> tuple[str, int]:
    recent = list(closes)
    if not recent or any(not math.isfinite(c) or c <= 0 for c in recent):
        raise DataIntegrityError("recovery: missing/invalid COMPLETE4H closes")
    if close != recent[-1]:
        raise DataIntegrityError(
            "recovery: official close must be the latest COMPLETE4H"
        )
    expected = ["R" + r[2:] for r in RATIOS]
    if (
        list(fibs) != expected
        or any(not math.isfinite(v) or v <= 0 for v in fibs.values())
        or any(b <= a for a, b in zip(fibs.values(), list(fibs.values())[1:]))
    ):
        raise DataIntegrityError("recovery: invalid or legacy Fib grid")
    # Strictly above. Equality is a touch, not a reclaim/acceptance.
    held = [key for key, level in fibs.items() if close > level]
    if not held:
        sign = "=" if close == fibs["R236"] else "<"
        return f"{sign}R.236 · 0 C4H", 0
    key = held[-1]
    count = 0
    for value in reversed(recent):
        if value <= fibs[key]:
            break
        count += 1
    return f">R.{key[1:]} · {count} C4H", count


def recovery_status(
    h4: pd.DataFrame, fibs: dict[str, float], low_ts
) -> tuple[str, int]:
    if h4.empty:
        raise DataIntegrityError("recovery: no closed COMPLETE4H")
    stamps = pd.to_datetime(h4["timestamp"], utc=True)
    if (
        stamps.isna().any()
        or stamps.duplicated().any()
        or not stamps.is_monotonic_increasing
    ):
        raise DataIntegrityError("recovery: invalid COMPLETE4H chronology")
    if not stamps.eq(stamps.dt.floor("4h")).all() or (
        "n" in h4 and not h4["n"].eq(4).all()
    ):
        raise DataIntegrityError("recovery: incomplete/misaligned 4H input")
    low_at = None if low_ts is None else pd.Timestamp(low_ts)
    if low_at is not None:
        low_at = (
            low_at.tz_localize("UTC")
            if low_at.tzinfo is None
            else low_at.tz_convert("UTC")
        )
    rows = h4 if low_at is None else h4.loc[stamps >= low_at]
    if rows.empty:
        raise DataIntegrityError("recovery: working low is after latest COMPLETE4H")
    row_stamps = pd.to_datetime(rows["timestamp"], utc=True)
    # Never manufacture persistence through a missing COMPLETE4H bucket. This
    # deliberately also resets the streak across commodity session closures.
    gaps = row_stamps.diff().gt(pd.Timedelta(hours=4))
    cut = next((i for i in range(len(rows) - 1, 0, -1) if gaps.iloc[i]), 0)
    closes = rows["close"].iloc[cut:].astype(float).tolist()
    text, count = status_from_closes(float(h4["close"].iloc[-1]), fibs, closes)
    if (
        low_at is not None
        and low_at < stamps.iloc[0]
        and cut == 0
        and count == len(closes)
    ):
        # The streak could have begun before the downloaded history. An exact
        # integer would be invented; ask the caller to obtain earlier candles.
        raise DataIntegrityError(
            "recovery: history does not cover start of consecutive closes"
        )
    return text, count


def parse_recovery_status(text: str) -> tuple[float | None, int]:
    """Old > .382 · 3/3 strings are intentionally NOT accepted after migration."""
    match = re.fullmatch(
        r">R\.(236|382|500|618|705|786|886) · ([1-9]\d*) C4H", text or ""
    )
    return (float("0." + match[1]), int(match[2])) if match else (None, 0)
