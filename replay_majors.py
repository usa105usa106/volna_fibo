"""Offline reconstruction of the dated BTC/ETH hypotheses, NOT a historical signal test.

python replay_majors.py --output audit/v0025/majors-reconstruction
Uses the checked, same-exchange Binance parquet fixtures; no live I/O or SQLite.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from core_major_rules import KNOWN_AT, cross_asset_context
from core_majors import MajorWaveEngine
from core_models import MarketSnapshot
from core_senior import complete4h
from services_formatter import render_table_png, technical_report_text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixtures", type=Path, default=Path(__file__).parent / "fixtures/majors_v0025"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    snapshots = {}
    for base in ("BTC", "ETH"):
        h1 = pd.read_parquet(args.fixtures / f"{base}_1h.parquet")
        d1 = pd.read_parquet(args.fixtures / f"{base}_1d.parquet")
        snapshots[base] = MarketSnapshot(
            base + "USDT",
            "binance_spot",
            0.0,
            float(h1.close.iloc[-1]),
            float(h1.low.iloc[-1]),
            h1,
            d1,
            observed_at=(h1.timestamp.max() + pd.Timedelta(hours=1)).isoformat(),
        )
    cross = cross_asset_context(
        complete4h(snapshots["ETH"].hourly_closed),
        complete4h(snapshots["BTC"].hourly_closed),
        "binance_spot",
    )
    for snap in snapshots.values():
        snap.cross_asset = cross
    engine = MajorWaveEngine()
    states = [engine.evaluate(snap, None, 1, 300) for snap in snapshots.values()]
    failures = [s.last_event for s in states if s.status == "DATA_INCOMPLETE"]
    if failures:
        raise RuntimeError("; ".join(failures))
    args.output.mkdir(parents=True, exist_ok=True)
    provenance = {
        "version": "0025",
        "market": "binance_spot",
        "protocol_known_at": str(KNOWN_AT),
        "snapshot": snapshots["BTC"].observed_at,
        "warning": "Retrospective structural reconstruction, not an out-of-sample prediction or current quote",
        "sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in args.fixtures.glob("*.parquet")
        },
    }
    (args.output / "states.json").write_text(
        json.dumps(
            {"provenance": provenance, "states": [s.to_dict() for s in states]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (args.output / "report.txt").write_text(
        technical_report_text(
            states,
            heading=[
                "v0025 BTC/ETH — RETROSPECTIVE STRUCTURAL RECONSTRUCTION",
                f"Snapshot {provenance['snapshot']}; protocol published {KNOWN_AT}",
                provenance["warning"],
            ],
            errors=[],
        ),
        encoding="utf-8",
    )
    (args.output / "table.png").write_bytes(
        render_table_png(
            states,
            title="BTC / ETH · v0025",
            subtitle=f"Binance Spot · historical reconstruction · {provenance['snapshot']}",
            crypto_label="DATED SCENARIOS · NOT A LIVE REPORT",
            version="0025",
        )
    )
    print(
        json.dumps(
            {
                s.symbol: {
                    "wave": s.wave_type,
                    "low": s.working_low,
                    "fib": s.fib_status,
                    "targets": s.targets,
                }
                for s in states
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
