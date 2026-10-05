from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal


WaveType = Literal["W2", "W3-(2)", "W4", "W5", "CONTROL", "NONE"]
WaveStatus = Literal[
    "FORMING",
    "DEEP",
    "RECOVERING",
    "CONFIRMED",
    "EXTENDED",
    "INVALID",
    "RECOUNT",
    "NO_SETUP",
    "DATA_INCOMPLETE",
    "PHASE_UNCERTAIN",
]


@dataclass(slots=True)
class WaveState:
    symbol: str
    exchange: str
    wave_type: WaveType
    status: WaveStatus
    origin: float | None
    impulse_high: float | None
    working_low: float | None
    strict_origin: float | None
    impulse_start_ts: str | None = None
    impulse_high_ts: str | None = None
    working_low_ts: str | None = None
    parent_w2_low: float | None = None
    parent_w2_ts: str | None = None
    w3_1_high: float | None = None
    w3_1_high_ts: str | None = None
    retrace_depth: float | None = None
    fibs: dict[str, float] = field(default_factory=dict)
    fib_status: str = "—"
    targets: list[float] = field(default_factory=list)
    # Explicit projection anchors used to build T1..T4.  They are stored separately
    # from the generic wave geometry so the target engine cannot silently fall back
    # to a wrong parent impulse when a W3-(2) is being tracked/reloaded.
    target_origin: float | None = None
    target_impulse_high: float | None = None
    target_impulse_length: float | None = None
    target_source: str | None = None
    targets_hit: list[int] = field(default_factory=list)
    detector_version: str = "0023"
    structure_evidence: dict[str, Any] = field(default_factory=dict)
    base_zone: tuple[float, float] | None = None
    deep_zone: tuple[float, float] | None = None
    current_price: float | None = None
    growth_from_low_pct: float | None = None
    strict_distance_pct: float | None = None
    rating: float | None = None
    liquidity_rank: int | None = None
    last_complete4h_bucket: str | None = None
    last_complete4h_close: float | None = None
    last_event: str = ""
    is_control: bool = False
    created_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.base_zone is not None:
            data["base_zone"] = list(self.base_zone)
        if self.deep_zone is not None:
            data["deep_zone"] = list(self.deep_zone)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WaveState":
        data = dict(data)
        # Older stored counts were produced by the defective anchor selector.
        # Preserve the tracked symbols, but require a same-symbol recount.
        data.setdefault("detector_version", "")
        if data.get("base_zone") is not None:
            data["base_zone"] = tuple(data["base_zone"])
        if data.get("deep_zone") is not None:
            data["deep_zone"] = tuple(data["deep_zone"])
        return cls(**data)


@dataclass(slots=True)
class MarketSnapshot:
    symbol: str
    exchange: str
    quote_volume: float
    live_price: float | None
    live_low: float | None
    hourly_closed: Any
    daily_closed: Any
    # Validated daily context across an explicitly documented ticker transition.
    # It is ancestry evidence only; active anchors still come from current H4.
    daily_context: Any = None
    history_evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AppSettings:
    top_n: int = 300
    interval_minutes: int = 60
    exchange: str = "binance_spot"
    mode: str = "idle"  # idle/search/track
    last_run_search: str | None = None
    last_run_track: str | None = None

    @property
    def interval_seconds(self) -> int:
        return self.interval_minutes * 60

    def last_report_for_mode(self) -> datetime | None:
        raw = self.last_run_search if self.mode == "search" else self.last_run_track
        return datetime.fromisoformat(raw) if raw else None
