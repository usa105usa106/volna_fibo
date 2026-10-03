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
from services_formatter import render_table_png, technical_report_text


class ArchiveReader:
    def __init__(self, root: Path):
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

    def snapshot(self, asset: str, cutoff=None) -> MarketSnapshot:
        kind = "commodities" if asset in {"XAU", "USOIL", "XAG"} else "crypto"
        cutoff = pd.Timestamp(cutoff) if cutoff is not None else self.asof
        cutoff = cutoff.tz_localize("UTC") if cutoff.tzinfo is None else cutoff.tz_convert("UTC")
        if cutoff > self.asof:
            raise ValueError("cutoff is later than archive snapshot")
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
            raw = validate_candles(raw, IntegrityPolicy(tf, kind == "commodities"), self.asof)
            raw = raw[raw["timestamp"] + step <= cutoff]
            converted[tf] = validate_candles(raw, IntegrityPolicy(tf, kind == "commodities"), cutoff)
        is_crypto = kind == "crypto"
        # Historical replays never use the later universe ticker as an entry price.
        price = float(self.universe.loc[asset, "binance_last_price"]) if is_crypto and cutoff == self.asof else float(converted["1h"]["close"].iloc[-1])
        volume = float(self.universe.loc[asset, "binance_quote_volume_24h"]) if is_crypto else 0.0
        return MarketSnapshot(asset, "binance_spot" if is_crypto else "mexc_futures", volume, price, None, converted["1h"], converted["1d"])

    def rank(self, asset: str) -> int | None:
        return self.volume_order.index(asset) + 1 if asset in self.volume_order else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--symbols", default="", help="Comma-separated symbols; default: archive's full universe plus XAU/USOIL")
    args = parser.parse_args()
    reader = ArchiveReader(args.archive)
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
        "version": "0019", "snapshot": str(reader.asof),
        "sources": {"crypto": "Binance Spot", "XAU/USOIL": "MEXC Futures"},
        "limitations": ["No incomplete live-candle wick is stored in this archive", "Historical replay uses snapshot liquidity ranks; it is not an out-of-sample profitability test"],
        "file_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in args.archive.glob("*.parquet")},
    }
    (args.output / "states.json").write_text(json.dumps({"provenance": provenance, "states": [s.to_dict() for s in states], "errors": errors}, ensure_ascii=False, indent=2))
    heading = ["SENIOR WAVE BOT v0019 — OFFLINE REPLAY", f"Snapshot: {reader.asof}", "Crypto: Binance Spot; controls: MEXC Futures. Exchange sources are separate."]
    (args.output / "audit.txt").write_text(technical_report_text(states, heading=heading, errors=errors), encoding="utf-8")
    display = states if args.symbols else select_top_crypto([s for s in states if (s.rating or 0) >= 7.5]) + [s for s in states if s.is_control]
    (args.output / "table.png").write_bytes(render_table_png(display, title="OFFLINE REPLAY · 0019", subtitle="Binance Spot crypto / MEXC controls · 03.10.2026 09:25 MSK", crypto_label="SENIOR CRYPTO", version="0019"))
    print(json.dumps({"assets": len(states), "setups": sum(s.wave_type in {"W2", "W3-(2)"} for s in states), "data_errors": len(errors)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
