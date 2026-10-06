"""Human-readable count audit, shared by Telegram and the attached TXT."""

from __future__ import annotations

from core_models import WaveState


def major_count_text(state: WaveState) -> str:
    from services_formatter import _fmt

    b = state.structure_evidence.get("major_count")
    if not b:
        return ""
    a, lines = b["anchors"], []
    p = lambda key: _fmt(a[key]["price"])
    lines.append(f"MARKET: {state.exchange} / {state.symbol}")
    if state.status == "DATA_INCOMPLETE":
        lines.extend(
            [
                "DATA INCOMPLETE — saved count below is historical, not refreshed",
                state.last_event,
            ]
        )
    if b["base"] == "BTC":
        primary = b.get("primary") or "NONE"
        count = "V2/V3 UNRESOLVED" if b.get("unresolved") else primary + " PRIMARY"
        box = b.get("box", {})
        lines += [
            f"BTC COUNT: {count}",
            f"BTC STATE: {b['state']}",
            f"CURRENT WAVE: {b['current']}",
            f"BOX: {_fmt(box.get('lower'))} – {_fmt(box.get('upper'))}",
            f"BOX STATUS: {box.get('status', 'UNKNOWN')}",
            f"WORKING LOW: {_fmt(b['working_low'])} · {b['working_low_ts']}",
            f"HARD INVALIDATION: V1 wick <{p('i1')} or >{_fmt(box.get('upper'))}; V2 wick <{p('w2')}; V3 wick <{p('origin')}",
            f"PRIMARY: {primary}",
            f"ALT: {' / '.join(b.get('alt', [])) or 'NONE'}",
            f"WHY PRIMARY: {b.get('why', 'V1 remains primary inside structural box')}",
        ]
        for variant in ("V2", "V3"):
            score = b.get("scores", {}).get(variant, {})
            lines += [
                f"{variant} SCORE: {score.get('total', 'unavailable')}/10 — structural score, not probability",
                f"{variant} EVIDENCE: {score.get('subdivision', 'unavailable')}",
                f"{variant} COMPONENTS: {score.get('components', {})}",
            ]
            v = b.get("variants", {}).get(variant, {})
            lines.append(
                f"{variant} {v.get('target_label', 'TARGETS')} (conditional): "
                + " / ".join(_fmt(t) for t in v.get("targets", []))
            )
        for variant, cause in b.get("retired", {}).items():
            lines.append(
                f"{variant}: RETIRED / HARD INVALID — {cause['reason']} @ {cause['at']}"
            )
    else:
        lines += [
            f"ETH BASE: {p('base1')} / {p('origin')} double bottom",
            f"W1: {p('origin')} → {p('w1')}",
            f"W2: {p('w1')} → {p('w2')}",
            f"W3-(1): {p('w2')} → {p('peak')}",
            f"W3-(1) INTERNAL: {p('w2')} → {p('i1')} → {p('i2')} → {p('i3')} → {p('i4')} → {p('peak')}",
            f"W3-(2): {p('peak')} → {_fmt(b['working_low'])}",
            f"CURRENT: {b['current']}",
            f"HARD INVALIDATION: valid wick <{p('w2')}; no automatic restoration",
            "RECOVERY FIB: "
            + "; ".join(f"{k}={_fmt(v)}" for k, v in state.fibs.items()),
            f"C4H RECOVERY: {state.fib_status} @ {_fmt(state.last_complete4h_close)}",
            "W3-(3) TARGETS: "
            + (" / ".join(_fmt(v) for v in state.targets) or "WITHHELD"),
            "REFERENCE CORRECTION: 1849.54 belongs to June 15, before June 26 launch; post-launch W1 high measured on July 13. Internal W4 uses the full correction low.",
        ]
    cross = b.get("cross_asset", {})
    lines += [
        f"ETH/BTC: {_fmt(cross.get('ratio'))} @ {cross.get('timestamp', 'unavailable')}",
        f"ETH/BTC MACRO: {cross.get('status', 'UNAVAILABLE')}; objective 0.07–0.08 (conditional, not guaranteed)",
        f"CROSS-ASSET CONSISTENCY SCORE: {cross.get('score', 0)}; soft evidence only",
        f"RATIO SOURCE: {cross.get('source', 'unavailable')}",
    ]
    return "\n".join(lines)
