from __future__ import annotations

import html
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext

from core_models import WaveState
from core_symbols import display_symbol


TABLE_HEADER = (
    "# | Актив | Рейтинг | Цена | Сейчас развивается | Рабочий low | Fib / статус | "
    "Рост от low | База — лимитка | На вынос — лимитка | Цели"
)
CONTROL_ORDER = {"XAU": 0, "USOIL": 1}


def _fmt(v: float | None) -> str:
    """Format market prices in plain decimal notation, never scientific notation.

    We keep about ten significant digits for price-like values at every magnitude.
    This preserves values such as DOGS
    0.00004901 while avoiding strings like 4.901E-05 in Telegram.
    """
    if v is None:
        return "—"
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return "—"
    if not d.is_finite():
        return "—"
    if d == 0:
        return "0"

    # Ten significant digits are enough for displayed market levels while keeping
    # small-token zeros intact. A fixed decimal cap would distort ultra-small prices.
    places = max(0, 9 - d.copy_abs().adjusted())
    with localcontext() as ctx:
        ctx.prec = max(60, d.copy_abs().adjusted() + 2)
        quantum = Decimal(1).scaleb(-places)
        q = d.quantize(quantum, rounding=ROUND_HALF_UP)

    # If a positive non-zero value is so tiny that the display quantization would
    # collapse it to zero, fall back to the exact fixed-point Decimal spelling.
    if q == 0 and d != 0:
        text = format(d, "f")
    else:
        text = format(q, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def split_html(text: str, max_chars: int = 3500) -> list[str]:
    """Split our escaped <b>-only markup, preserving tags/entities and UTF-16 budget."""
    if not 2 <= max_chars <= 4096:
        raise ValueError("max_chars must be between 2 and 4096")
    tokens = re.findall(r"</?b>|&(?:amp|lt|gt|quot|#\d+|#x[0-9a-fA-F]+);|[\s\S]", text)
    chunks, parts = [], []
    used, bold = 0, False
    for fragment in tokens:
        if fragment in {"<b>", "</b>"}:
            bold = fragment == "<b>"
            parts.append(fragment)
            continue
        width = len(html.unescape(fragment).encode("utf-16-le")) // 2
        if used + width > max_chars:
            chunks.append("".join(parts) + ("</b>" if bold else ""))
            parts = ["<b>"] if bold else []
            used = 0
        parts.append(fragment)
        used += width
    if used:
        chunks.append("".join(parts))
    return chunks


def split_plain(text: str, max_chars: int = 3500) -> list[str]:
    return [html.unescape(part) for part in split_html(html.escape(text, quote=False), max_chars)]


def _zone(z: tuple[float, float] | None) -> str:
    if not z:
        return "—"
    return f"{_fmt(z[0])}–{_fmt(z[1])}"


def _targets(state: WaveState) -> str:
    if state.targets:
        return " / ".join(_fmt(x) for x in state.targets[:4])
    if state.status in {"INVALID", "RECOUNT"}:
        return "after recount"
    return "—"


def _e(value: object) -> str:
    return html.escape(str(value), quote=False)


def _b(value: object, condition: bool) -> str:
    text = _e(value)
    return f"<b>{text}</b>" if condition else text


def _near_zone(price: float | None, zone: tuple[float, float] | None) -> bool:
    if price is None or not zone:
        return False
    low, high = sorted(zone)
    # In the zone, or no more than 3% above it: still a fresh senior entry area.
    return low <= price <= high * 1.03


def _row_html(state: WaveState, rank: int | None) -> str:
    invalid = state.status in {"INVALID", "RECOUNT", "NO_SETUP", "DATA_INCOMPLETE"}
    strong = not invalid and state.rating is not None and state.rating >= 8.5
    fresh = (
        not invalid
        and state.growth_from_low_pct is not None
        and 0 <= state.growth_from_low_pct <= 7.0
    )
    deep_valid = (
        not invalid
        and state.retrace_depth is not None
        and 0.618 <= state.retrace_depth <= 0.95
        and (state.strict_distance_pct is None or state.strict_distance_pct >= 1.5)
    )
    fib_good = not invalid and state.status in {"RECOVERING", "CONFIRMED"}
    base_near = not invalid and _near_zone(state.current_price, state.base_zone)
    deep_near = not invalid and _near_zone(state.current_price, state.deep_zone)
    target_good = False
    if not invalid and state.current_price and state.targets:
        target_good = state.targets[0] / state.current_price - 1 >= 0.20

    growth = "—" if state.growth_from_low_pct is None else f"{state.growth_from_low_pct:+.1f}%"
    rating = "—" if state.rating is None else f"{state.rating:.1f}"
    wave = state.wave_type if not invalid else f"{state.status} / {state.wave_type}"

    row = [
        _e(rank if rank is not None else "—"),
        _b(display_symbol(state.symbol), strong),
        _b(rating, strong),
        _b(_fmt(state.current_price), fresh),
        _b(wave, not invalid and state.wave_type in {"W2", "W3-(2)"}),
        _b(_fmt(state.working_low), deep_valid),
        _b(state.fib_status, fib_good),
        _b(growth, fresh),
        _b(_zone(state.base_zone), base_near),
        _b(_zone(state.deep_zone), deep_near),
        _b(_targets(state), target_good),
    ]
    return " | ".join(row)


def _sorted_crypto(states: list[WaveState]) -> list[WaveState]:
    return sorted(
        (state for state in states if not state.is_control),
        key=lambda state: state.rating if state.rating is not None else -1.0,
        reverse=True,
    )


def _sorted_controls(states: list[WaveState]) -> list[WaveState]:
    return sorted(
        (state for state in states if state.is_control),
        key=lambda state: (CONTROL_ORDER.get(display_symbol(state.symbol), 99), display_symbol(state.symbol)),
    )


def table_lines_html(states: list[WaveState], *, ranked: bool = True) -> list[str]:
    """Render one table section. Controls can be rendered with ranked=False (rank shown as —)."""
    ordered = (
        sorted(states, key=lambda s: s.rating if s.rating is not None else -1.0, reverse=True)
        if ranked
        else states
    )
    lines = [f"<b>{_e(TABLE_HEADER)}</b>", _e("—" * 86)]
    for index, state in enumerate(ordered, 1):
        lines.append(_row_html(state, index if ranked else None))
    return lines


def _section_chunks(
    states: list[WaveState],
    section_title: str,
    *,
    ranked: bool,
    max_chars: int,
    report_title: str | None = None,
) -> list[str]:
    if not states:
        return []
    ordered = (
        sorted(states, key=lambda s: s.rating if s.rating is not None else -1.0, reverse=True)
        if ranked
        else states
    )
    heading = f"<b>{_e(section_title)}</b>\n"
    header = f"<b>{_e(TABLE_HEADER)}</b>\n{_e('—' * 86)}\n"
    first_prefix = (f"{_e(report_title)}\n\n" if report_title else "") + heading + header
    continued_prefix = f"{heading}{header}"

    chunks: list[str] = []
    current = first_prefix
    for index, state in enumerate(ordered, 1):
        row = _row_html(state, index if ranked else None) + "\n"
        # A single row is safely below Telegram's message limit. Split only between rows.
        if len(current) + len(row) > max_chars and current != first_prefix:
            chunks.append(current.rstrip())
            current = continued_prefix
        current += row
    chunks.append(current.rstrip())
    return chunks


def telegram_table_messages(
    states: list[WaveState],
    title: str,
    max_chars: int = 3500,
    *,
    crypto_label: str = "CRYPTO",
    controls_label: str = "XAU / USOIL — ДОПОЛНИТЕЛЬНО, ВНЕ РЕЙТИНГА",
) -> list[str]:
    """
    Split crypto and permanent controls deliberately.

    Crypto rows are rating-ranked and numbered. XAU/USOIL are always rendered in a
    separate section with an em dash in the rank column, so they can never consume
    or visually appear inside the TOP-10 crypto ranking.
    """
    crypto = _sorted_crypto(states)
    controls = _sorted_controls(states)

    messages: list[str] = []
    report_title: str | None = title
    if crypto:
        messages.extend(
            _section_chunks(
                crypto,
                crypto_label,
                ranked=True,
                max_chars=max_chars,
                report_title=report_title,
            )
        )
        report_title = None
    if controls:
        messages.extend(
            _section_chunks(
                controls,
                controls_label,
                ranked=False,
                max_chars=max_chars,
                report_title=report_title,
            )
        )
        report_title = None
    if not messages:
        messages.append(f"{_e(title)}\n\nНет данных для таблицы.")
    return [part for message in messages for part in split_html(message, max_chars)]
