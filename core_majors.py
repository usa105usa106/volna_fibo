"""BTC/ETH-only engine; frozen reference degree, exchange-local measured prices.

Generic Elliott anchor selection is unchanged. State is JSON serializable;
ScannerService owns durable storage, while /walk supplies a private in-memory book.
"""

from __future__ import annotations

import math
from copy import deepcopy
from itertools import pairwise

import pandas as pd

from core_major_rules import (
    INVALID_TEXT,
    PROTOCOL,
    box_upper,
    clean_five,
    impulse_errors,
    map_anchors,
    projection,
    recovery,
    validate_anchor_chronology,
)
from core_major_state import (
    btc_hard_rules,
    choose_btc,
    eth_hard_rules,
    reanchor,
    retire,
)
from core_models import MarketSnapshot, WaveState
from core_recovery import SEMANTICS, recovery_status
from core_senior import _zones, complete4h, data_incomplete_state
from core_symbols import display_symbol
from data_integrity import DataIntegrityError, IntegrityPolicy, validate_candles


def is_major(symbol: str) -> bool:
    return display_symbol(symbol) in {"BTC", "ETH"}


def _five(a: dict, keys: tuple[str, ...]) -> list[dict]:
    return [a[k] for k in keys]


def _momentum(frame: pd.DataFrame, points: list[dict]) -> float:
    close = frame.close.astype(float)
    macd = (
        close.ewm(span=12, adjust=False).mean()
        - close.ewm(span=26, adjust=False).mean()
    )

    def peak(i):
        mask = frame.timestamp.between(
            pd.Timestamp(points[i - 1]["timestamp"]),
            pd.Timestamp(points[i]["timestamp"]),
        )
        return float(macd[mask].max()) if mask.any() else 0.0

    return 1.0 if peak(3) > 0 and peak(3) > peak(5) else 0.0


def impulse_score(points, proven, frame, daily, cross_bonus=0.0):
    # Components are reproducible scores, not probabilities. D1 support is
    # measured from turning dates over the full supplied history.
    turns = 0
    for p in points[1:-1]:
        day = pd.Timestamp(p["timestamp"]).floor("D")
        around = daily[
            daily.timestamp.between(
                day - pd.Timedelta(days=2), day + pd.Timedelta(days=2)
            )
        ]
        row = around[around.timestamp.eq(day)]
        if not row.empty:
            value = float(row[p["kind"]].iloc[0])
            extreme = float(
                around[p["kind"]].min()
                if p["kind"] == "low"
                else around[p["kind"]].max()
            )
            turns += int(math.isclose(value, extreme, rel_tol=1e-8))
    durations = [
        (pd.Timestamp(y["timestamp"]) - pd.Timestamp(x["timestamp"])).total_seconds()
        for x, y in pairwise(points)
    ]
    proportion = (
        2.0
        if durations and min(durations) > 0 and max(durations) / min(durations) <= 8
        else (1.0 if durations and min(durations) > 0 else 0.0)
    )
    fib = 0.0
    if proven:
        depths = [
            (points[i]["price"] - points[i + 1]["price"])
            / (points[i]["price"] - points[i - 1]["price"])
            for i in (1, 3)
        ]
        fib = 0.5 * sum(0.146 <= x <= 0.886 for x in depths)
    components = {
        "subdivision": 3.0 if proven else 0.0,
        "full_1d": min(2.0, turns * 0.5),
        "proportionality": proportion,
        "fib": fib,
        "momentum": _momentum(frame, points) if proven else 0.0,
        "ethbtc": min(0.5, max(0.0, float(cross_bonus))),
    }
    return {
        "total": round(sum(components.values()), 2),
        "components": components,
        "hard_rules": "PASS" if not points or not impulse_errors(points) else "FAIL",
        "subdivision": "PROVEN"
        if proven
        else "UNPROVEN — conditional ALT, no forced micro pivots",
        "points": points,
    }


def degree_scores(
    a: dict, h4: pd.DataFrame, daily: pd.DataFrame, hourly: pd.DataFrame, cross: dict
) -> dict:
    v3 = _five(a, ("origin", "w1", "w2", "i1", "i2", "peak"))
    v2 = clean_five(h4, a["w2"], a["peak"])
    if v2["status"] != "PROVEN":
        # 1H only supports an independent five. Its vertices never replace senior anchors.
        v2 = clean_five(hourly, a["w2"], a["peak"])
        v2["subdivision_timeframe"] = "1H"
    else:
        v2["subdivision_timeframe"] = "COMPLETE4H"
    result = {}
    for variant, points, proven in (
        ("V2", v2["points"], v2["status"] == "PROVEN"),
        ("V3", v3, not impulse_errors(v3, h4)),
    ):
        frame = (
            h4
            if variant == "V3" or v2.get("subdivision_timeframe") == "COMPLETE4H"
            else hourly
        )
        result[variant] = impulse_score(
            points,
            proven,
            frame,
            daily,
            cross.get("score", 0.0) if variant == "V3" else 0.0,
        )
        if variant == "V2":
            result[variant]["search"] = v2
            if not proven:
                # Missing subdivision evidence is not a measured zero, nor a
                # PASS/FAIL of Elliott rules. Keep the conditional ALT alive.
                result[variant]["total"] = None
                result[variant]["components"] = {
                    key: None for key in result[variant]["components"]
                }
                result[variant]["hard_rules"] = "UNKNOWN"
        elif impulse_errors(points, frame):
            result[variant]["hard_rules"] = "FAIL"
    return result


class MajorWaveEngine:
    def evaluate(
        self,
        snap: MarketSnapshot,
        previous: WaveState | None,
        rank: int | None,
        top_n: int,
    ) -> WaveState:
        try:
            return self._evaluate(snap, previous, rank)
        except DataIntegrityError as exc:
            shown = data_incomplete_state(
                snap.symbol, snap.exchange, None, event=str(exc)
            )
            if previous:
                shown.structure_evidence = deepcopy(previous.structure_evidence)
            shown.last_event = str(exc)
            return shown

    def _evaluate(
        self, snap: MarketSnapshot, previous: WaveState | None, rank: int | None
    ) -> WaveState:
        base = display_symbol(snap.symbol)
        if not is_major(snap.symbol):
            raise ValueError("MajorWaveEngine only supports BTC/ETH")
        if snap.hourly_closed.empty or snap.daily_closed.empty:
            raise DataIntegrityError("major count needs full 1D + COMPLETE4H history")
        cutoff = (
            pd.Timestamp(snap.observed_at)
            if snap.observed_at
            else pd.to_datetime(snap.hourly_closed.timestamp, utc=True).max()
            + pd.Timedelta(hours=1)
        )
        if cutoff.tzinfo is None:
            raise DataIntegrityError("snapshot cutoff must include timezone")
        cutoff = cutoff.tz_convert("UTC")
        h1 = validate_candles(
            snap.hourly_closed,
            IntegrityPolicy("1h", False, False),
            cutoff.to_pydatetime(),
        )
        daily = validate_candles(
            snap.daily_closed,
            IntegrityPolicy("1d", False, False),
            cutoff.to_pydatetime(),
        )
        h4 = complete4h(h1)
        if h4.empty:
            raise DataIntegrityError("no COMPLETE4H buckets")
        live = None
        if snap.live_candle:
            live_frame = validate_candles(
                pd.DataFrame([snap.live_candle]),
                IntegrityPolicy("1h", False, False, False),
                cutoff.to_pydatetime(),
            )
            live = live_frame.iloc[0]
            if pd.Timestamp(live.timestamp) < h1.timestamp.iloc[
                -1
            ] or cutoff - pd.Timestamp(live.timestamp) > pd.Timedelta(hours=2):
                raise DataIntegrityError("stale intrabar candle")
            if live.timestamp not in {
                h1.timestamp.iloc[-1],
                h1.timestamp.iloc[-1] + pd.Timedelta(hours=1),
            }:
                raise DataIntegrityError("gap before intrabar candle")
            if snap.live_price is not None and not math.isclose(
                float(live.close), snap.live_price, rel_tol=1e-10
            ):
                raise DataIntegrityError("live quote disagrees with candle envelope")
        elif snap.live_price is not None and not math.isclose(
            float(h1.close.iloc[-1]), snap.live_price, rel_tol=1e-10
        ):
            raise DataIntegrityError("live quote has no validated candle envelope")
        old = previous.structure_evidence.get("major_count") if previous else None
        if old and (
            old.get("market") != snap.exchange
            or old.get("base") != base
            or old.get("protocol") != PROTOCOL
        ):
            raise DataIntegrityError("count identity / exchange mismatch")
        book = deepcopy(old) if old else None
        if book and cutoff < pd.Timestamp(book["observed_at"]):
            raise DataIntegrityError(
                "out-of-order major snapshot; saved count preserved"
            )
        if book is None:
            a = map_anchors(base, h4, daily, snap.exchange)
            if base == "BTC":
                errors = impulse_errors(
                    _five(a, ("origin", "w1", "w2", "i1", "i2", "peak")), h4
                )
                if errors:
                    raise DataIntegrityError(
                        "BTC reference degree violates " + ", ".join(errors)
                    )
            else:
                if (
                    not a["base1"]["price"]
                    <= a["origin"]["price"]
                    < a["w2"]["price"]
                    < a["w1"]["price"]
                ):
                    raise DataIntegrityError(
                        "ETH double-bottom / senior W2 structure not matched"
                    )
                errors = impulse_errors(
                    _five(a, ("w2", "i1", "i2", "i3", "i4", "peak")), h4
                )
                if errors:
                    raise DataIntegrityError(
                        "ETH W3-(1) five not valid: " + ", ".join(errors)
                    )
            book = {
                "protocol": PROTOCOL,
                "base": base,
                "market": snap.exchange,
                "anchors": a,
                "retired": {},
                "working_low": a["low"]["price"],
                "working_low_ts": a["low"]["timestamp"],
                "v1_low": a["low"]["price"],
                "accepted_at": None,
                "last_bucket": a["low"]["timestamp"],
                "state": "BTC_V1_BOX" if base == "BTC" else "ETH_W32_RECOVERY",
                "streak": 0,
            }
            # Validate historical violations after the impulse peak, before the
            # provisional low too. The W3-(5) length cap only starts AFTER that low.
            tail = h1[
                (
                    h1.timestamp
                    >= pd.Timestamp(a["peak"]["timestamp"]) + pd.Timedelta(hours=4)
                )
                & (
                    h1.timestamp
                    < pd.Timestamp(a["low"]["timestamp"]) + pd.Timedelta(hours=4)
                )
            ]
            if not tail.empty:
                self._hard(
                    book,
                    float(tail.low.min()),
                    a["low"]["price"],
                    tail.timestamp.iloc[-1].isoformat(),
                )
        a = book["anchors"]
        validate_anchor_chronology(base, a)
        for name, anchor in a.items():
            if anchor.get("market") != snap.exchange:
                raise DataIntegrityError("anchor exchange mismatch")
            row = h4[h4.timestamp.eq(pd.Timestamp(anchor["timestamp"]))]
            if row.empty or not math.isclose(
                float(row[anchor["kind"]].iloc[0]), anchor["price"], rel_tol=1e-10
            ):
                raise DataIntegrityError(
                    f"saved {name} anchor differs from current exchange candles"
                )
        if pd.Timestamp(book["working_low_ts"]) <= pd.Timestamp(a["peak"]["timestamp"]):
            raise DataIntegrityError("working low chronology precedes impulse high")
        if h1.timestamp.min() > pd.Timestamp(book["last_bucket"]) + pd.Timedelta(
            hours=4
        ):
            raise DataIntegrityError("history gap since last saved major bucket")
        cross = deepcopy(
            snap.cross_asset
            or {"status": "UNAVAILABLE", "score": 0.0, "objective": [0.07, 0.08]}
        )
        if cross.get("market", snap.exchange) != snap.exchange:
            cross = {
                "status": "UNAVAILABLE",
                "score": 0.0,
                "objective": [0.07, 0.08],
                "reason": "peer market mismatch; discarded",
                "market": snap.exchange,
            }
        # Score fixed impulse evidence at each new observation; no future daily
        # candles participate in a historical step.
        new_buckets = h4[h4.timestamp > pd.Timestamp(book["last_bucket"])]
        for row in new_buckets.itertuples():
            stamp = row.timestamp.isoformat()
            self._hard(book, float(row.low), float(row.high), stamp)
            moved = reanchor(book, float(row.low), stamp)
            if moved and base == "BTC" and "V1" not in book["retired"]:
                # After a lower completed W4 low the cap is lower too. Inspect
                # 1H order: a high BEFORE the new low is not a W5 length breach.
                hours = h1[
                    h1.timestamp.between(
                        row.timestamp, row.timestamp + pd.Timedelta(hours=3)
                    )
                ]
                low_hour = hours.loc[hours.low.idxmin()]
                following = hours[hours.timestamp > low_hour.timestamp]
                after_high = max(
                    float(low_hour.close),
                    float(following.high.max())
                    if not following.empty
                    else float(low_hour.close),
                )
                self._hard(book, float(row.low), after_high, stamp)
            if row.close > a["peak"]["price"] and pd.Timestamp(stamp) >= pd.Timestamp(
                book["working_low_ts"]
            ):
                book["accepted_at"] = book.get("accepted_at") or stamp
            book["last_bucket"] = stamp
        # CLOSED 1H inside an unfinished C4H and the valid current wick can retire,
        # but cannot move senior pivots or confirm a breakout.
        tail = h1[
            h1.timestamp >= pd.Timestamp(book["last_bucket"]) + pd.Timedelta(hours=4)
        ]
        lows, highs = list(tail.low), list(tail.high)
        if live is not None and pd.Timestamp(live.timestamp) >= pd.Timestamp(
            book["last_bucket"]
        ) + pd.Timedelta(hours=4):
            lows.append(float(live.low))
            highs.append(float(live.high))
        if lows:
            self._hard(book, min(lows), max(highs), cutoff.isoformat())
        # Never trust a standalone price/low for invalidation without its OHLC.
        if base == "BTC":
            scores = degree_scores(a, h4, daily, h1, cross)
            for variant in ("V2", "V3"):
                if scores[variant]["hard_rules"] == "FAIL":
                    retire(
                        book,
                        variant,
                        "independent impulse hard-rule violation",
                        cutoff.isoformat(),
                    )
            self._hard(
                book, book["working_low"], book["working_low"], cutoff.isoformat()
            )
            choose_btc(book, scores, book["last_bucket"])
        elif "ETH" not in book["retired"]:
            eth_points = _five(a, ("w2", "i1", "i2", "i3", "i4", "peak"))
            book["score"] = impulse_score(
                eth_points,
                not impulse_errors(eth_points, h4),
                h4,
                daily,
                cross.get("score", 0.0),
            )
            book["state"] = (
                "ETH_W33_CONFIRMED" if book["accepted_at"] else "ETH_W32_RECOVERY"
            )
        book.update(
            observed_at=cutoff.isoformat(),
            cross_asset=cross,
            history={
                "daily_from": daily.timestamp.iloc[0].isoformat(),
                "daily_to": daily.timestamp.iloc[-1].isoformat(),
                "daily_rows": len(daily),
                "hourly_from": h1.timestamp.iloc[0].isoformat(),
                "hierarchy": "FULL 1D -> COMPLETE4H -> 1H -> 15m (execution only)",
            },
        )
        return self._render(snap, book, h4, rank)

    @staticmethod
    def _hard(book: dict, low: float, high: float, at: str):
        (
            btc_hard_rules(book, low, high, at)
            if book["base"] == "BTC"
            else eth_hard_rules(book, low, at)
        )

    def _render(
        self, snap: MarketSnapshot, book: dict, h4: pd.DataFrame, rank: int | None
    ) -> WaveState:
        a, base = book["anchors"], book["base"]
        primary = book.get("primary")
        is_btc_v1 = base == "BTC" and primary == "V1"
        origin_key = (
            "i2"
            if is_btc_v1
            else ("origin" if base == "BTC" and primary == "V3" else "w2")
        )
        origin, high, low = (
            a[origin_key]["price"],
            a["peak"]["price"],
            book["working_low"],
        )
        if is_btc_v1:
            current, wave, source = (
                "W3-(5) ACTIVE / DEVELOPING",
                "W3-(5)",
                "W3-(5) structural limit; not an extension target",
            )
        elif base == "BTC" and primary == "V3":
            current = (
                "senior W3 candidate"
                if not book["accepted_at"]
                else "senior W3 developing"
            )
            wave, source = "W2", "SENIOR W3 TARGETS (V3)"
        else:
            current = (
                "W3-(3) CONFIRMED"
                if book["accepted_at"]
                else "W3-(2) recovery / W3-(3) CANDIDATE"
            )
            wave, source = (
                ("W3-(3)" if book["accepted_at"] else "W3-(2)"),
                "W3-(3) TARGETS",
            )
        invalid = book["state"] in {"BTC_FULL_RECOUNT", "ETH_FULL_RECOUNT"}
        if invalid:
            current = INVALID_TEXT
        book["current"] = current
        fibs = recovery(low, high) if not invalid and low < high else {}
        last_close = float(h4.close.iloc[-1])
        fib_status, _ = (
            recovery_status(h4, fibs, book["working_low_ts"]) if fibs else ("—", 0)
        )
        targets = [] if invalid or is_btc_v1 else projection(low, origin, high)
        if base == "BTC":
            book["box"] = {
                "lower": a["i1"]["price"],
                "upper": box_upper(a, book["v1_low"]),
                "status": "RETIRED" if "V1" in book["retired"] else "ACTIVE",
            }
            book["variants"] = {}
            for v, k in (("V2", "w2"), ("V3", "origin")):
                book["variants"][v] = {
                    "hard_status": "INVALID"
                    if v in book["retired"]
                    else book["scores"][v]["hard_rules"],
                    "subdivision": book["scores"][v]["subdivision"],
                    "strict_origin": a[k]["price"],
                    "targets": []
                    if v in book["retired"]
                    else projection(low, a[k]["price"], high),
                    "target_label": "W3-(3)" if v == "V2" else "SENIOR W3",
                }
            strict = a["i1"]["price"] if is_btc_v1 else origin
        else:
            strict = origin
            book["reference_corrections"] = [
                "1849.54 (June 15) predates June 26 launch; post-launch C4H W1 high is measured near July 13",
                "internal W4 low is measured from full correction, not 2386.19 reference",
            ]
            book["subdivision"] = _five(a, ("w2", "i1", "i2", "i3", "i4", "peak"))
        price = (
            snap.live_price if snap.live_price is not None else float(h4.close.iloc[-1])
        )
        if not math.isfinite(price) or price <= 0:
            raise DataIntegrityError("non-finite/non-positive current price")
        status = (
            "RECOUNT"
            if invalid
            else ("CONFIRMED" if book["accepted_at"] else "RECOVERING")
        )
        rating = None
        if not invalid:
            # Auditable structural components; never a probability or success rate.
            rating = (
                book["score"]["total"]
                if base == "ETH"
                else book["scores"]["V3" if is_btc_v1 else primary]["total"]
            )
        state = WaveState(
            symbol=snap.symbol,
            exchange=snap.exchange,
            wave_type=wave,
            status=status,
            origin=origin,
            impulse_high=high,
            working_low=low,
            strict_origin=strict,
            impulse_start_ts=a[origin_key]["timestamp"],
            impulse_high_ts=a["peak"]["timestamp"],
            working_low_ts=book["working_low_ts"],
            parent_w2_low=a["w2"]["price"],
            parent_w2_ts=a["w2"]["timestamp"],
            w3_1_high=high if source == "W3-(3) TARGETS" else None,
            retrace_depth=(high - low) / (high - origin),
            fibs={} if invalid else fibs,
            fib_status=INVALID_TEXT if invalid else fib_status,
            targets=[] if invalid else targets,
            target_origin=None if invalid or is_btc_v1 else origin,
            target_impulse_high=None if invalid or is_btc_v1 else high,
            target_impulse_length=None if invalid or is_btc_v1 else high - origin,
            target_source=source,
            current_price=price,
            growth_from_low_pct=(price / low - 1) * 100,
            strict_distance_pct=(price / strict - 1) * 100,
            rating=rating,
            liquidity_rank=rank,
            detector_version="0026",
            structure_evidence={"major_count": book, "fib_semantics": SEMANTICS},
            last_complete4h_bucket=book["last_bucket"],
            last_complete4h_close=last_close,
            last_event=INVALID_TEXT if invalid else book["state"],
            updated_at=book["observed_at"],
        )
        if not invalid and low > strict:
            state.base_zone, state.deep_zone = _zones(low, high, strict)
            if is_btc_v1:
                # A W4 retest uses the same execution grid, but its sweep must
                # stay inside the V1 box. Near the overlap boundary the generic
                # origin buffer can exceed low: never display it above low.
                sweep_floor = max(strict, low - 0.05 * (high - low))
                if strict * 1.0005 < low:
                    sweep_floor = max(sweep_floor, strict * 1.0005)
                state.deep_zone = (sweep_floor, low)
        elif not invalid and is_btc_v1 and low == strict:
            # Equality is technically valid, but no sweep below low is legal.
            state.base_zone = (low, fibs["R236"])
        return state
