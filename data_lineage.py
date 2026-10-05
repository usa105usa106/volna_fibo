"""Documented ticker ancestry with an explicit separate spot-context fallback.

The old unrelated GRAM token on MEXC must NOT be joined to Toncoin. Only the
documented TON -> GRAM transition below is supported. Older daily candles are
context; no synthetic hourly candles or active low/targets are manufactured.
"""
from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass

import httpx
import pandas as pd

from data_integrity import DataIntegrityError, IntegrityPolicy, validate_candles


@dataclass(frozen=True)
class TickerTransition:
    exchange: str
    current: str
    predecessor: str
    current_start: str
    predecessor_end: str
    sources: tuple[str, ...]


TRANSITIONS = {
    "mexc_spot": TickerTransition(
        "mexc_spot", "GRAMUSDT", "TONUSDT",
        "2026-06-15T12:00:00Z", "2026-06-15T10:00:00Z",
        ("https://www.mexc.com/announcements/article/mexc-to-rename-toncoin-ton-to-gram-gram-17827791536125",),
    ),
    "binance_spot": TickerTransition(
        "binance_spot", "GRAMUSDT", "TONUSDT",
        "2026-07-02T08:00:00Z", "2026-06-30T03:00:00Z",
        ("https://www.binance.com/en/support/announcement/detail/fe307fba935b44698fde4db01e84a7eb",),
    ),
    "mexc_futures": TickerTransition(
        "mexc_futures", "GRAM_USDT", "TON_USDT",
        "2026-06-15T14:45:00Z", "2026-06-23T08:00:00Z",
        ("https://www.mexc.com/announcements/article/mexc-to-list-gramusdt-gramusdc-gramusd1-futures-on-jun-15-2026-14-17827791536266",
         "https://www.mexc.com/announcements/article/delisting-of-tonusdt-tonusdc-and-tonusd1-usdt-m-perpetual-futures-pairs-jun-23-2026-08-17827791536290"),
    ),
}
PRICE_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


def transition_for(exchange: str, symbol: str) -> TickerTransition | None:
    rule = TRANSITIONS.get(exchange)
    return rule if rule and symbol in {rule.current, "GRAM"} else None


def restrict_current_history(df: pd.DataFrame, exchange: str, symbol: str, timeframe: str) -> pd.DataFrame:
    """Exclude a reused ticker's unrelated past and a partial first hourly bar."""
    rule = transition_for(exchange, symbol)
    if rule is None or df.empty:
        return df
    start = pd.Timestamp(rule.current_start)
    boundary = start.ceil("h") if timeframe == "1h" else start.floor("D")
    return df.loc[pd.to_datetime(df["timestamp"], utc=True) >= boundary].copy()


def history_metadata(rule: TickerTransition, status: str, **details) -> dict:
    return {"status": status, "exchange": rule.exchange, "current": rule.current,
            "predecessor": rule.predecessor, "conversion_ratio": 1,
            "current_start": rule.current_start, "predecessor_end": rule.predecessor_end,
            "sources": list(rule.sources), **details}


def merge_daily_context(current: pd.DataFrame, prefix: pd.DataFrame, rule: TickerTransition, now) -> tuple[pd.DataFrame, dict]:
    """Join verified predecessor context without weakening normal gap validation.

    Each segment is validated independently. Only the officially scheduled
    Binance transition hiatus is permitted between them; no OHLC is filled.
    Overlapping MEXC contracts use the NEW contract from its listing day onward.
    """
    current = validate_candles(current, IntegrityPolicy("1d", False, False), now)
    prefix = validate_candles(prefix, IntegrityPolicy("1d", False, False), now)
    if not current["timestamp"].eq(current["timestamp"].dt.floor("D")).all():
        raise DataIntegrityError("current daily context is not UTC-day aligned")
    if not prefix["timestamp"].eq(prefix["timestamp"].dt.floor("D")).all():
        raise DataIntegrityError("predecessor daily context is not UTC-day aligned")
    seam = current["timestamp"].iloc[0]
    expected_seam = pd.Timestamp(rule.current_start).floor("D")
    if seam != expected_seam:
        raise DataIntegrityError("current history does not reach the documented ticker transition")
    for col, expected in (("source_exchange", rule.exchange), ("source_symbol", rule.predecessor)):
        if col in prefix and not prefix[col].eq(expected).all():
            raise DataIntegrityError(f"predecessor {col} mismatch")
    # Reject wrong-period predecessor bars, rather than silently repairing an
    # offline file passed as 'TON' or importing overlapping contracts twice.
    prefix = prefix[prefix["timestamp"] < seam].copy()
    if prefix.empty:
        raise DataIntegrityError("empty predecessor prefix")
    # The final TON session ended intraday. Its truncated daily bar is NOT a
    # complete 1D candle; omit that day instead of pretending it traded 24 hours.
    expected_last = min(seam - pd.Timedelta(days=1),
                        pd.Timestamp(rule.predecessor_end).floor("D") - pd.Timedelta(days=1))
    if prefix["timestamp"].iloc[-1] != expected_last:
        raise DataIntegrityError("predecessor history is truncated before the transition")
    if (prefix["timestamp"] >= pd.Timestamp(rule.predecessor_end)).any():
        raise DataIntegrityError("predecessor data extends past delisting")
    old = prefix[PRICE_COLUMNS].assign(source_symbol=rule.predecessor, source_exchange=rule.exchange)
    new = current[PRICE_COLUMNS].assign(source_symbol=rule.current, source_exchange=rule.exchange)
    context = pd.concat([old, new], ignore_index=True)
    if context["timestamp"].duplicated().any():
        raise DataIntegrityError("duplicate timestamps across ticker transition")
    return context, history_metadata(rule, "restored", prefix_rows=len(prefix),
        prefix_from=prefix["timestamp"].iloc[0].isoformat(), prefix_through=prefix["timestamp"].iloc[-1].isoformat(),
        missing_calendar_days=int((seam - expected_last) / pd.Timedelta(days=1)) - 1)


def decode_binance_daily_zip(blob: bytes, checksum: str, filename: str, *, terminal_time=None) -> pd.DataFrame:
    """Read one bounded official monthly archive, including 2025+ microseconds."""
    parts = checksum.strip().split()
    if not parts or len(blob) > 2_000_000 or hashlib.sha256(blob).hexdigest() != parts[0]:
        raise DataIntegrityError("Binance predecessor archive checksum/size mismatch")
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            entries = archive.infolist()
            if len(entries) != 1 or entries[0].filename != filename.removesuffix(".zip") + ".csv":
                raise DataIntegrityError("unexpected Binance predecessor ZIP member")
            if entries[0].file_size > 2_000_000:
                raise DataIntegrityError("oversized Binance predecessor CSV")
            content = archive.read(entries[0])
    except (zipfile.BadZipFile, RuntimeError) as exc:
        raise DataIntegrityError("invalid Binance predecessor ZIP") from exc
    raw = pd.read_csv(io.BytesIO(content), header=None)
    if raw.shape[1] != 12 or not 1 <= len(raw) <= 31:
        raise DataIntegrityError("unexpected Binance predecessor CSV shape")
    raw.columns = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "trades", "taker_base", "taker_quote", "ignore"]
    opens = pd.to_numeric(raw["open_time"], errors="raise")
    microseconds = opens >= 100_000_000_000_000
    if microseconds.any() and not microseconds.all():
        raise DataIntegrityError("mixed predecessor timestamp units")
    unit = "us" if microseconds.all() else "ms"
    step = 86_400_000_000 if unit == "us" else 86_400_000
    closes = pd.to_numeric(raw["close_time"], errors="coerce")
    complete = closes.eq(opens + step - 1)
    terminal = pd.Series(False, index=raw.index)
    if terminal_time is not None:
        end = pd.Timestamp(terminal_time)
        terminal_open = int(end.floor("D").timestamp()) * (1_000_000 if unit == "us" else 1000)
        terminal_end = int(end.timestamp()) * (1_000_000 if unit == "us" else 1000)
        # Binance's delisting bar uses millisecond precision even inside the
        # microsecond archive. Permit ONLY that documented terminal close, then
        # discard the incomplete calendar day from structural daily context.
        terminal = opens.eq(terminal_open) & closes.isin([terminal_end - 1, terminal_end - (1000 if unit == "us" else 1)])
    if not (complete | terminal).all():
        raise DataIntegrityError("invalid predecessor close_time")
    raw["timestamp"] = pd.to_datetime(opens, unit=unit, utc=True)
    month = re.fullmatch(r"[A-Z0-9]+-1d-(\d{4}-\d{2})\.zip", filename)
    if month is None or not raw["timestamp"].dt.strftime("%Y-%m").eq(month[1]).all():
        raise DataIntegrityError("predecessor archive month mismatch")
    return raw.loc[complete & ~terminal, PRICE_COLUMNS].copy()


async def binance_predecessor_daily(client, rule: TickerTransition, start, end) -> tuple[pd.DataFrame, list[dict]]:
    """Public official archive is used because the old API symbol is delisted."""
    first = pd.Timestamp(start).floor("D")
    last = min(pd.Timestamp(end), pd.Timestamp(rule.predecessor_end))
    months = pd.period_range(first.tz_localize(None).to_period("M"), (last - pd.Timedelta(nanoseconds=1)).tz_localize(None).to_period("M"), freq="M")
    if len(months) > 24:
        raise DataIntegrityError("predecessor history request exceeds 24-month limit")
    frames, files = [], []
    for month in months:
        filename = f"{rule.predecessor}-1d-{month}.zip"
        url = f"https://data.binance.vision/data/spot/monthly/klines/{rule.predecessor}/1d/{filename}"
        response = await client._get(url)
        response.raise_for_status()
        checksum_response = await client._get(url + ".CHECKSUM")
        checksum_response.raise_for_status()
        blob = response.content
        frames.append(decode_binance_daily_zip(blob, checksum_response.text, filename, terminal_time=rule.predecessor_end))
        files.append({"url": url, "sha256": hashlib.sha256(blob).hexdigest()})
    if not frames:
        raise DataIntegrityError("no predecessor archive months")
    full = pd.concat(frames, ignore_index=True)
    prefix = full[(full["timestamp"] >= first) & (full["timestamp"] < pd.Timestamp(end))]
    return prefix.copy(), files


def validate_spot_context(context: pd.DataFrame, futures: pd.DataFrame, spot_rule: TickerTransition, now) -> dict:
    """Check identity/overlap without copying spot prices into futures candles.

    A 5% maximum daily-close basis is a data compatibility guard, not a price
    conversion. No normalization/rescaling of either market is performed.
    """
    if not {"source_exchange", "source_symbol"}.issubset(context.columns):
        raise DataIntegrityError("spot context lacks provenance")
    if not context["source_exchange"].eq(spot_rule.exchange).all():
        raise DataIntegrityError("spot context exchange mismatch")
    expected = context["timestamp"].map(lambda ts: spot_rule.predecessor if ts < pd.Timestamp(spot_rule.current_start).floor("D") else spot_rule.current)
    if not context["source_symbol"].eq(expected).all():
        raise DataIntegrityError("spot context ticker mismatch")
    futures = validate_candles(futures, IntegrityPolicy("1d", False, False), now)
    overlap = futures[["timestamp", "close"]].merge(context[["timestamp", "close"]], on="timestamp", suffixes=("_future", "_spot"))
    # Listing days are partial and differ between markets.
    overlap = overlap[overlap.timestamp > pd.Timestamp(spot_rule.current_start).floor("D")]
    if len(overlap) < 14:
        raise DataIntegrityError("spot context needs 14 overlapping daily candles")
    basis = (overlap.close_future / overlap.close_spot - 1).abs()
    if basis.max() > .05:
        raise DataIntegrityError("spot/futures close basis exceeds 5% compatibility limit")
    if overlap.timestamp.max() != futures.timestamp.max():
        raise DataIntegrityError("spot context is truncated before the futures snapshot")
    return {"overlap_days": len(overlap), "max_abs_close_basis": float(basis.max()), "basis_limit": .05}


async def load_daily_context(client, symbol: str, current: pd.DataFrame, start, now, *, spot_clients=()) -> tuple[pd.DataFrame | None, dict]:
    rule = transition_for(client.name, symbol)
    if rule is None:
        return None, {}
    if pd.Timestamp(start) >= pd.Timestamp(rule.current_start).floor("D"):
        return None, history_metadata(rule, "not_requested", detail="requested lookback starts after the ticker transition")
    try:
        if current.empty:
            raise DataIntegrityError("empty current ticker history")
        seam = current["timestamp"].iloc[0]
        if client.name == "binance_spot":
            prefix, files = await binance_predecessor_daily(client, rule, start, seam)
        else:
            prefix, _, _ = await client.candles(rule.predecessor, "1d", pd.Timestamp(start).floor("D").to_pydatetime(), seam.to_pydatetime())
            files = []
        context, evidence = merge_daily_context(current, prefix, rule, now)
        if files:
            evidence["archives"] = files
        return context, evidence
    except (httpx.HTTPError, ValueError, KeyError, RuntimeError, OSError) as exc:
        # Cancellation is BaseException and propagates through all fallback calls.
        detail = f"{type(exc).__name__}: {exc}"[:240]
    attempts = [{"exchange": client.name, "detail": detail}]
    if client.name == "mexc_futures":
        for spot in spot_clients:
            spot_rule = TRANSITIONS.get(spot.name)
            if spot_rule is None or spot.name not in {"mexc_spot", "binance_spot"}:
                continue
            try:
                spot_current, _, _ = await spot.candles(spot_rule.current, "1d", pd.Timestamp(start).to_pydatetime(), now)
                context, info = await load_daily_context(spot, spot_rule.current, spot_current, start, now)
                if context is None:
                    raise DataIntegrityError(info.get("detail", "spot predecessor unavailable"))
                basis = validate_spot_context(context, current, spot_rule, now)
                return context, history_metadata(rule, "restored_spot", spot_exchange=spot.name,
                    spot_history=info, compatibility=basis, attempts=attempts,
                    usage="Separate ancestry context; active anchors and targets remain futures-only")
            except (httpx.HTTPError, ValueError, KeyError, RuntimeError, OSError) as exc:
                attempts.append({"exchange": spot.name, "detail": f"{type(exc).__name__}: {exc}"[:240]})
    return None, history_metadata(rule, "unavailable", detail=detail, attempts=attempts)
