from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd


class DataIntegrityError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class IntegrityPolicy:
    timeframe: str
    is_control: bool = False
    check_freshness: bool = True
    closed_only: bool = True


def validate_candles(df: pd.DataFrame, policy: IntegrityPolicy, now: datetime | None = None) -> pd.DataFrame:
    """Validate an exchange candle series before senior-wave analysis.

    Crypto markets are expected to be continuous. Exchange commodity contracts may
    legitimately pause around weekends/holidays, so larger calendar gaps are tolerated
    for controls while duplicates, malformed OHLC and timestamp misalignment are not.
    """
    if df.empty:
        raise DataIntegrityError(f"{policy.timeframe}: empty candle history")
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = required.difference(df.columns)
    if missing:
        raise DataIntegrityError(f"{policy.timeframe}: missing columns {sorted(missing)}")

    out = df.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True, errors="coerce")
    if out["timestamp"].isna().any():
        raise DataIntegrityError(f"{policy.timeframe}: invalid timestamp")
    if out["timestamp"].duplicated().any():
        raise DataIntegrityError(f"{policy.timeframe}: duplicate timestamp")
    out = out.sort_values("timestamp").reset_index(drop=True)

    for col in ("open", "high", "low", "close", "volume"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    if out[["open", "high", "low", "close"]].isna().any().any():
        raise DataIntegrityError(f"{policy.timeframe}: NaN OHLC")
    if not np.isfinite(out[["open", "high", "low", "close"]].to_numpy(dtype=float)).all():
        raise DataIntegrityError(f"{policy.timeframe}: non-finite OHLC")
    if (out[["open", "high", "low", "close"]] <= 0).any().any():
        raise DataIntegrityError(f"{policy.timeframe}: non-positive OHLC")
    if not np.isfinite(out["volume"].to_numpy(dtype=float)).all() or (out["volume"] < 0).any():
        raise DataIntegrityError(f"{policy.timeframe}: invalid volume")

    max_oc = out[["open", "close"]].max(axis=1)
    min_oc = out[["open", "close"]].min(axis=1)
    if (out["high"] < max_oc).any() or (out["low"] > min_oc).any() or (out["high"] < out["low"]).any():
        raise DataIntegrityError(f"{policy.timeframe}: impossible OHLC relationship")

    if policy.timeframe == "1h":
        aligned = (
            out["timestamp"].dt.minute.eq(0)
            & out["timestamp"].dt.second.eq(0)
            & out["timestamp"].dt.microsecond.eq(0)
            & out["timestamp"].dt.nanosecond.eq(0)
        )
        if not aligned.all():
            raise DataIntegrityError("1h: timestamps are not UTC-hour aligned")
        expected = pd.Timedelta(hours=1)
        max_gap = pd.Timedelta(hours=72 if policy.is_control else 1)
        freshness = pd.Timedelta(hours=8 if policy.is_control else 3)
    elif policy.timeframe == "1d":
        expected = pd.Timedelta(days=1)
        max_gap = pd.Timedelta(days=4 if policy.is_control else 1)
        freshness = pd.Timedelta(days=4 if policy.is_control else 2)
    else:
        raise DataIntegrityError(f"unsupported timeframe: {policy.timeframe}")

    diffs = out["timestamp"].diff().dropna()
    if (diffs % expected != pd.Timedelta(0)).any():
        raise DataIntegrityError(f"{policy.timeframe}: inconsistent candle grid")
    if (diffs < expected).any():
        raise DataIntegrityError(f"{policy.timeframe}: overlapping/out-of-order candles")
    if (diffs > max_gap).any():
        biggest = diffs.max()
        raise DataIntegrityError(f"{policy.timeframe}: internal candle gap {biggest}")

    current = pd.Timestamp(now or datetime.now(timezone.utc))
    current = current.tz_localize("UTC") if current.tzinfo is None else current.tz_convert("UTC")
    if (out["timestamp"] > current).any():
        raise DataIntegrityError(f"{policy.timeframe}: future candle")
    if policy.closed_only and ((out["timestamp"] + expected) > current).any():
        raise DataIntegrityError(f"{policy.timeframe}: candle is not closed at snapshot cutoff")
    if policy.check_freshness:
        last = out["timestamp"].iloc[-1]
        if current - last > freshness:
            raise DataIntegrityError(f"{policy.timeframe}: stale history, last candle {last.isoformat()}")

    return out
