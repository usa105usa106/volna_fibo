"""Read-only offline replay. No network, Telegram, SQLite or production settings.

python replay_archive.py /path/to/extracted_market_archive --output /path/to/report
Requires the dev/replay dependency pyarrow; the live bot does not need it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from core_models import MarketSnapshot
from core_ranking import select_top_crypto
from core_senior import SeniorWaveDetector, no_setup_state, data_incomplete_state
from data_integrity import IntegrityPolicy, validate_candles
from data_lineage import history_metadata, merge_daily_context, restrict_current_history, transition_for
from services_formatter import render_table_png, technical_report_text


class ArchiveReader:
    def __init__(self, root: Path, predecessor_daily: Path | None = None, hourly_prefix: Path | None = None):
        self.root = root
        manifest = json.loads((root / "manifest.json").read_text())
        self.asof = pd.Timestamp(manifest["generated_at_utc"])
        self.universe = pd.read_parquet(root / "universe.parquet").set_index("binance_base_asset")
        self.frames = {}
        for kind in ("crypto", "commodities"):
            for tf in ("1h", "1d"):
                path = root / f"{kind}_ohlcv_{tf}.parquet"
                self.frames[kind, tf] = pd.read_parquet(path)
        self.volume_order = self.universe.sort_values("binance_quote_volume_24h", ascending=False).index.tolist()
        self.predecessor_daily = pd.read_parquet(predecessor_daily) if predecessor_daily else None
        self.hourly_prefix = pd.read_parquet(hourly_prefix) if hourly_prefix else None

    def snapshot(self, asset: str, cutoff=None) -> MarketSnapshot:
        kind = "commodities" if asset in {"XAU", "USOIL", "XAG"} else "crypto"
        cutoff = pd.Timestamp(cutoff) if cutoff is not None else self.asof
        cutoff = cutoff.tz_localize("UTC") if cutoff.tzinfo is None else cutoff.tz_convert("UTC")
        if cutoff > self.asof:
            raise ValueError("cutoff is later than archive snapshot")
        exchange = "binance_spot" if kind == "crypto" else "mexc_futures"
        converted = {}
        for tf in ("1h", "1d"):
            raw = self.frames[kind, tf]
            raw = raw.loc[raw["asset"] == asset].copy()
            raw["timestamp"] = pd.to_datetime(raw["timestamp_ms"], unit="ms", utc=True)
            step = pd.Timedelta(hours=1) if tf == "1h" else pd.Timedelta(days=1)
            # Validate archive bytes BEFORE cutoff filtering; do not hide malformed
            # duplicate bars or a truncated Binance close_time.
            if "close_time_ms" in raw and not raw.empty:
                expected = raw["timestamp_ms"] + int(step.total_seconds() * 1000) - 1
                if not raw["close_time_ms"].eq(expected).all():
                    raise ValueError(f"{asset} {tf}: invalid close_time_ms")
            if "volume_base" in raw:
                raw["volume"] = raw["volume_base"]
            raw = restrict_current_history(raw, exchange, asset, tf)
            raw = validate_candles(raw, IntegrityPolicy(tf, kind == "commodities"), self.asof)
            if tf == "1h" and self.hourly_prefix is not None:
                prefix = self.hourly_prefix
                if not {"source_exchange", "source_symbol"}.issubset(prefix.columns):
                    raise ValueError("hourly prefix lacks source exchange/symbol")
                remote = asset + ("USDT" if exchange == "binance_spot" else "_USDT")
                prefix = prefix[prefix["source_symbol"].eq(remote)].copy()
                if not prefix.empty:
                    if not prefix["source_exchange"].eq(exchange).all():
                        raise ValueError("hourly prefix exchange mismatch")
                    prefix = validate_candles(prefix, IntegrityPolicy("1h", False, False), self.asof)
                    overlap = prefix.set_index("timestamp").join(raw.set_index("timestamp"), how="inner", lsuffix="_prefix")
                    for col in ("open", "high", "low", "close", "volume"):
                        if not overlap[col + "_prefix"].eq(overlap[col]).all():
                            raise ValueError("hourly prefix conflicts with original archive")
                    prefix = prefix[prefix["timestamp"] < raw["timestamp"].iloc[0]]
                    raw = pd.concat([prefix, raw], ignore_index=True)
            raw = raw[raw["timestamp"] + step <= cutoff]
            converted[tf] = validate_candles(raw, IntegrityPolicy(tf, kind == "commodities"), cutoff)
        is_crypto = kind == "crypto"
        # Historical replays never use the later universe ticker as an entry price.
        price = float(self.universe.loc[asset, "binance_last_price"]) if is_crypto and cutoff == self.asof else float(converted["1h"]["close"].iloc[-1])
        volume = float(self.universe.loc[asset, "binance_quote_volume_24h"]) if is_crypto else 0.0
        context, evidence = None, {}
        rule = transition_for(exchange, asset)
        if rule:
            if self.predecessor_daily is None:
                evidence = history_metadata(rule, "unavailable", detail="offline archive has no predecessor candles")
            else:
                # A supplied offline file must identify both its exchange and
                # ticker. Never silently read another venue's TON as MEXC.
                required = {"source_exchange", "source_symbol"}
                if not required.issubset(self.predecessor_daily.columns):
                    raise ValueError("predecessor file lacks source exchange/symbol")
                prefix = self.predecessor_daily.copy()
                prefix["timestamp"] = pd.to_datetime(prefix["timestamp"], utc=True)
                prefix = prefix[prefix["timestamp"] + pd.Timedelta(days=1) <= cutoff]
                context, evidence = merge_daily_context(converted["1d"], prefix, rule, cutoff)
        return MarketSnapshot(asset, exchange, volume, price, None, converted["1h"], converted["1d"], context, evidence)

    def rank(self, asset: str) -> int | None:
        return self.volume_order.index(asset) + 1 if asset in self.volume_order else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--symbols", default="", help="Comma-separated symbols; default: archive's full universe plus XAU/USOIL")
    parser.add_argument("--predecessor-daily", type=Path, help="Verified same-exchange TON daily parquet; replay never fetches network data")
    parser.add_argument("--hourly-prefix", type=Path, help="Verified same-exchange older H1 parquet with source_exchange/source_symbol")
    args = parser.parse_args()
    reader = ArchiveReader(args.archive, args.predecessor_daily, args.hourly_prefix)
    assets = [a.strip().upper() for a in args.symbols.split(",") if a.strip()] or reader.volume_order + ["XAU", "USOIL"]
    detector = SeniorWaveDetector()
    states = []
    errors = []
    for asset in assets:
        try:
            snap = reader.snapshot(asset)
            state = detector.detect(snap, reader.rank(asset), len(reader.volume_order))
            states.append(state or no_setup_state(asset, snap.exchange, snap.live_price, is_control=asset in {"XAU", "USOIL"}))
        except (ValueError, KeyError) as exc:
            errors.append(f"{asset}: {exc}")
            states.append(data_incomplete_state(asset, "mexc_futures" if asset in {"XAU", "USOIL"} else "binance_spot", None, is_control=asset in {"XAU", "USOIL"}, event=str(exc)))
    args.output.mkdir(parents=True, exist_ok=True)
    provenance = {
        "version": "0024", "snapshot": str(reader.asof),
        "sources": {"crypto": "Binance Spot", "XAU/USOIL": "MEXC Futures"},
        "limitations": ["No incomplete live-candle wick is stored in this archive", "Historical replay uses snapshot liquidity ranks; it is not an out-of-sample profitability test"],
        "file_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in args.archive.glob("*.parquet")},
        "predecessor_sha256": hashlib.sha256(args.predecessor_daily.read_bytes()).hexdigest() if args.predecessor_daily else None,
        "hourly_prefix_sha256": hashlib.sha256(args.hourly_prefix.read_bytes()).hexdigest() if args.hourly_prefix else None,
    }
    (args.output / "states.json").write_text(json.dumps({"provenance": provenance, "states": [s.to_dict() for s in states], "errors": errors}, ensure_ascii=False, indent=2))
    heading = ["SENIOR WAVE BOT v0024 — OFFLINE REPLAY", f"Snapshot: {reader.asof}", "Crypto: Binance Spot; controls: MEXC Futures. Exchange sources are separate."]
    (args.output / "audit.txt").write_text(technical_report_text(states, heading=heading, errors=errors), encoding="utf-8")
    display = states if args.symbols else select_top_crypto([s for s in states if (s.rating or 0) >= 7.5]) + [s for s in states if s.is_control]
    label_time = reader.asof.tz_convert("Europe/Moscow").strftime("%d.%m.%Y %H:%M MSK")
    (args.output / "table.png").write_bytes(render_table_png(display, title="OFFLINE REPLAY · 0024", subtitle=f"Binance Spot crypto / MEXC controls · {label_time}", crypto_label="SENIOR CRYPTO", version="0024"))
    print(json.dumps({"assets": len(states), "setups": sum(s.wave_type in {"W2", "W3-(2)"} and s.status != "DATA_INCOMPLETE" for s in states), "data_errors": len(errors), "insufficient_senior_history": sum(s.status == "DATA_INCOMPLETE" for s in states)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
