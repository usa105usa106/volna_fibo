from __future__ import annotations

import html
import io
import re
import textwrap
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from core_models import WaveState
from core_symbols import display_symbol


TABLE_HEADER = (
    "# | Актив | Рейтинг | Цена | Сейчас развивается | Рабочий low | Fib / статус | "
    "Рост от low | База | На вынос | Цели"
)
IMAGE_HEADERS = [
    "#",
    "Актив",
    "Рейтинг",
    "Цена",
    "Сейчас",
    "Раб. low",
    "Fib / статус",
    "Рост от low",
    "База",
    "На вынос",
    "Цели",
]
CONTROL_ORDER = {"XAU": 0, "USOIL": 1}

# 2560px keeps eleven true vertical columns readable when the Telegram image is opened.
IMAGE_COLUMN_WIDTHS = [75, 175, 145, 210, 205, 210, 330, 180, 275, 275, 470]
IMAGE_WIDTH = sum(IMAGE_COLUMN_WIDTHS) + 80


# ---------- common numeric formatting ----------

def _fmt(v: float | None) -> str:
    """Format market prices in plain decimal notation, never scientific notation."""
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

    # Preserve about ten significant digits while retaining all leading zeros for tiny tokens.
    places = max(0, 9 - d.copy_abs().adjusted())
    with localcontext() as ctx:
        ctx.prec = max(80, abs(d.copy_abs().adjusted()) + 40)
        quantum = Decimal(1).scaleb(-places)
        q = d.quantize(quantum, rounding=ROUND_HALF_UP)

    if q == 0 and d != 0:
        text = format(d, "f")
    else:
        text = format(q, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


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


def _near_zone(price: float | None, zone: tuple[float, float] | None) -> bool:
    if price is None or not zone:
        return False
    low, high = sorted(zone)
    return low <= price <= high * 1.03


def _inside_zone(price: float | None, zone: tuple[float, float] | None) -> bool:
    if price is None or not zone:
        return False
    low, high = sorted(zone)
    return low <= price <= high


def _confirmed_recovery_ratio(fib_status: str) -> float | None:
    """Return reclaimed Fib ratio only for durable 3/3 COMPLETE4H acceptance."""
    if "3/3 C4H" not in (fib_status or ""):
        return None
    match = re.search(r">\s*\.(\d+)", fib_status)
    if not match:
        return None
    try:
        return float(f"0.{match.group(1)}")
    except ValueError:
        return None


def _row_values_flags(state: WaveState, rank: int | None) -> tuple[list[str], list[bool]]:
    """One source of truth for text and PNG rows.

    v0015 deliberately uses sparse emphasis: bold/green means a genuinely favorable
    property, not merely a valid field. This keeps the image readable at a glance.
    """
    invalid = state.status in {"INVALID", "RECOUNT", "NO_SETUP", "DATA_INCOMPLETE"}
    rating_strong = not invalid and state.rating is not None and state.rating >= 8.5
    very_fresh = (
        not invalid
        and state.growth_from_low_pct is not None
        and 0 <= state.growth_from_low_pct <= 3.0
    )
    recovery_ratio = _confirmed_recovery_ratio(state.fib_status)
    fib_strong = not invalid and recovery_ratio is not None and recovery_ratio <= 0.500
    base_actionable = not invalid and _inside_zone(state.current_price, state.base_zone)
    deep_actionable = not invalid and _inside_zone(state.current_price, state.deep_zone)
    target_strong = False
    if not invalid and state.current_price and state.targets:
        target_strong = state.targets[0] / state.current_price - 1 >= 0.30

    growth = "—" if state.growth_from_low_pct is None else f"{state.growth_from_low_pct:+.1f}%"
    rating = "—" if state.rating is None else f"{state.rating:.1f}"
    wave = state.wave_type if not invalid else f"{state.status} / {state.wave_type}"
    values = [
        str(rank if rank is not None else "—"),
        display_symbol(state.symbol),
        rating,
        _fmt(state.current_price),
        wave,
        _fmt(state.working_low),
        state.fib_status or "—",
        growth,
        _zone(state.base_zone),
        _zone(state.deep_zone),
        _targets(state),
    ]
    flags = [
        False,              # #
        False,              # asset name is never highlighted by itself
        rating_strong,      # rating >= 8.5
        False,              # raw price is information, not a favorable signal by itself
        False,              # W2/W3-(2) label alone is not enough for emphasis
        False,              # working low alone is not enough for emphasis
        fib_strong,         # durable 3/3 C4H reclaim of .500 or stronger (.382/.236)
        very_fresh,         # <= 3% from working low
        base_actionable,    # actually trading inside base zone
        deep_actionable,    # actually trading inside deep/on-sweep zone
        target_strong,      # >= 30% to T1
    ]
    return values, flags


# ---------- legacy/fallback Telegram text ----------

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


def _e(value: object) -> str:
    return html.escape(str(value), quote=False)


def _b(value: object, condition: bool) -> str:
    text = _e(value)
    return f"<b>{text}</b>" if condition else text


def _row_html(state: WaveState, rank: int | None) -> str:
    values, flags = _row_values_flags(state, rank)
    return " | ".join(_b(value, flag) for value, flag in zip(values, flags, strict=True))


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
    ordered = (
        sorted(states, key=lambda s: s.rating if s.rating is not None else -1.0, reverse=True)
        if ranked
        else states
    )
    lines = [f"<b>{_e(TABLE_HEADER)}</b>", _e("—" * 86)]
    for index, state in enumerate(ordered, 1):
        lines.append(_row_html(state, index if ranked else None))
    return lines


def _section_chunks(states: list[WaveState], section_title: str, *, ranked: bool, max_chars: int, report_title: str | None = None) -> list[str]:
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
        if len(current) + len(row) > max_chars and current != first_prefix:
            chunks.append(current.rstrip())
            current = continued_prefix
        current += row
    chunks.append(current.rstrip())
    return chunks


def telegram_table_messages(states: list[WaveState], title: str, max_chars: int = 3500, *, crypto_label: str = "CRYPTO", controls_label: str = "XAU / USOIL — ДОПОЛНИТЕЛЬНО, ВНЕ РЕЙТИНГА") -> list[str]:
    """Text fallback used only if PNG delivery/rendering fails."""
    crypto = _sorted_crypto(states)
    controls = _sorted_controls(states)
    messages: list[str] = []
    report_title: str | None = title
    if crypto:
        messages.extend(_section_chunks(crypto, crypto_label, ranked=True, max_chars=max_chars, report_title=report_title))
        report_title = None
    if controls:
        messages.extend(_section_chunks(controls, controls_label, ranked=False, max_chars=max_chars, report_title=report_title))
        report_title = None
    if not messages:
        messages.append(f"{_e(title)}\n\nНет данных для таблицы.")
    return [part for message in messages for part in split_html(message, max_chars)]


# ---------- high-resolution PNG report ----------

def _font_path(bold: bool = False) -> str:
    candidates = [
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    raise RuntimeError("A Unicode TrueType font is required for PNG table rendering")


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(_font_path(bold), size=size)


def _wrap_cell(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int, *, max_lines: int = 4) -> list[str]:
    text = str(text or "—")
    if text == "—":
        return [text]
    words = text.replace(" / ", " /\n").replace(" · ", " ·\n").splitlines()
    lines: list[str] = []
    for paragraph in words:
        tokens = paragraph.split(" ") if " " in paragraph else [paragraph]
        current = ""
        for token in tokens:
            proposal = token if not current else f"{current} {token}"
            if draw.textlength(proposal, font=font) <= max_width:
                current = proposal
                continue
            if current:
                lines.append(current)
                current = ""
            # Hard-break a long price/target sequence only when it cannot fit by itself.
            if draw.textlength(token, font=font) > max_width:
                chunk = ""
                for ch in token:
                    proposal = chunk + ch
                    if chunk and draw.textlength(proposal, font=font) > max_width:
                        lines.append(chunk)
                        chunk = ch
                    else:
                        chunk = proposal
                current = chunk
            else:
                current = token
        if current:
            lines.append(current)
    if not lines:
        lines = ["—"]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > max_width:
            last = last[:-1]
        lines[-1] = (last or "") + "…"
    return lines


def render_table_png(
    states: list[WaveState],
    *,
    title: str,
    subtitle: str,
    crypto_label: str,
    version: str,
) -> bytes:
    """Render the main report as a normal image table with vertical columns."""
    crypto = _sorted_crypto(states)
    controls = _sorted_controls(states)

    title_font = _font(44, bold=True)
    meta_font = _font(27)
    header_font = _font(25, bold=True)
    cell_font = _font(25)
    cell_bold = _font(25, bold=True)
    section_font = _font(27, bold=True)

    probe = Image.new("RGB", (IMAGE_WIDTH, 200), "white")
    draw = ImageDraw.Draw(probe)
    x_padding = 12
    line_height = 34

    def build_rows(items: list[WaveState], ranked: bool):
        result = []
        for idx, state in enumerate(items, 1):
            values, flags = _row_values_flags(state, idx if ranked else None)
            wraps: list[list[str]] = []
            max_lines = 1
            for value, width, bold in zip(values, IMAGE_COLUMN_WIDTHS, flags, strict=True):
                font = cell_bold if bold else cell_font
                lines = _wrap_cell(draw, value, font, width - x_padding * 2)
                wraps.append(lines)
                max_lines = max(max_lines, len(lines))
            row_height = max(66, max_lines * line_height + 24)
            result.append((state, values, flags, wraps, row_height))
        return result

    crypto_rows = build_rows(crypto, True)
    control_rows = build_rows(controls, False)
    top_margin = 34
    title_height = 122
    header_height = 70
    section_height = 52
    bottom_margin = 35
    total_height = top_margin + title_height
    if crypto_rows:
        total_height += section_height + header_height + sum(row[-1] for row in crypto_rows)
    if control_rows:
        total_height += 24 + section_height + header_height + sum(row[-1] for row in control_rows)
    if not crypto_rows and not control_rows:
        total_height += 160
    total_height += bottom_margin

    image = Image.new("RGB", (IMAGE_WIDTH, total_height), (248, 249, 251))
    draw = ImageDraw.Draw(image)

    # Header panel.
    draw.rounded_rectangle((24, 20, IMAGE_WIDTH - 24, top_margin + title_height - 8), radius=18, fill=(24, 32, 47))
    draw.text((48, 38), title, font=title_font, fill="white")
    draw.text((48, 92), subtitle, font=meta_font, fill=(218, 224, 233))
    version_text = f"v{version}"
    vw = draw.textlength(version_text, font=meta_font)
    draw.text((IMAGE_WIDTH - 50 - vw, 48), version_text, font=meta_font, fill=(218, 224, 233))
    y = top_margin + title_height

    x_positions = [40]
    for width in IMAGE_COLUMN_WIDTHS[:-1]:
        x_positions.append(x_positions[-1] + width)

    def draw_section(label: str, rows, *, controls_section: bool):
        nonlocal y
        draw.text((42, y + 9), label, font=section_font, fill=(31, 41, 55))
        y += section_height
        left = 40
        right = left + sum(IMAGE_COLUMN_WIDTHS)
        draw.rounded_rectangle((left, y, right, y + header_height), radius=9, fill=(55, 65, 81))
        x = left
        for header, width in zip(IMAGE_HEADERS, IMAGE_COLUMN_WIDTHS, strict=True):
            bbox = draw.multiline_textbbox((0, 0), header, font=header_font, spacing=3)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            draw.multiline_text((x + (width - tw) / 2, y + (header_height - th) / 2 - 2), header, font=header_font, fill="white", align="center", spacing=3)
            x += width
        y += header_height

        for row_index, (state, values, flags, wraps, row_height) in enumerate(rows):
            invalid = state.status in {"INVALID", "RECOUNT", "DATA_INCOMPLETE"}
            if invalid:
                base_fill = (255, 239, 239)
            elif controls_section:
                base_fill = (237, 246, 255)
            else:
                base_fill = (255, 255, 255) if row_index % 2 == 0 else (246, 248, 251)
            x = left
            for value, flag, lines, width in zip(values, flags, wraps, IMAGE_COLUMN_WIDTHS, strict=True):
                fill = (231, 247, 236) if flag and not invalid else base_fill
                draw.rectangle((x, y, x + width, y + row_height), fill=fill, outline=(203, 209, 218), width=1)
                font = cell_bold if flag else cell_font
                text = "\n".join(lines)
                bbox = draw.multiline_textbbox((0, 0), text, font=font, spacing=5)
                th = bbox[3] - bbox[1]
                draw.multiline_text((x + x_padding, y + max(8, (row_height - th) / 2 - 2)), text, font=font, fill=(24, 29, 38), spacing=5)
                x += width
            y += row_height

    if crypto_rows:
        draw_section(crypto_label, crypto_rows, controls_section=False)
    if control_rows:
        if crypto_rows:
            y += 24
        draw_section("XAU / USOIL — ДОПОЛНИТЕЛЬНО", control_rows, controls_section=True)

    if not crypto_rows and not control_rows:
        draw.text((50, y + 40), "Нет данных для таблицы.", font=section_font, fill=(31, 41, 55))

    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


# ---------- .txt technical report ----------

def _state_technical_lines(state: WaveState, rank: int | None) -> list[str]:
    values, _ = _row_values_flags(state, rank)
    fibs = ", ".join(f"{key}={_fmt(value)}" for key, value in state.fibs.items()) or "—"
    return [
        " | ".join(values),
        f"  status={state.status}; wave={state.wave_type}; event={state.last_event or '—'}",
        f"  origin={_fmt(state.origin)}; impulse_high={_fmt(state.impulse_high)}; working_low={_fmt(state.working_low)}; strict_origin={_fmt(state.strict_origin)}",
        f"  retrace={'—' if state.retrace_depth is None else f'{state.retrace_depth * 100:.2f}%'}; strict_distance={'—' if state.strict_distance_pct is None else f'{state.strict_distance_pct:.2f}%'}; growth_from_low={'—' if state.growth_from_low_pct is None else f'{state.growth_from_low_pct:+.2f}%'}",
        f"  fibs: {fibs}",
        f"  last_complete4h={state.last_complete4h_bucket or '—'}; close={_fmt(state.last_complete4h_close)}",
        f"  timestamps: impulse_start={state.impulse_start_ts or '—'}; impulse_high={state.impulse_high_ts or '—'}; working_low={state.working_low_ts or '—'}",
    ]


def technical_report_text(
    states: list[WaveState],
    *,
    heading: list[str],
    errors: list[str] | None = None,
    skipped_controls: list[str] | None = None,
) -> str:
    """Full diagnostic report attached as .txt instead of flooding Telegram chat."""
    crypto = _sorted_crypto(states)
    controls = _sorted_controls(states)
    lines = list(heading)
    lines.extend(["", TABLE_HEADER, "=" * 120])
    for index, state in enumerate(crypto, 1):
        lines.extend(_state_technical_lines(state, index))
        lines.append("-")
    if controls:
        lines.extend(["", "XAU / USOIL — ДОПОЛНИТЕЛЬНО", "=" * 120])
        for state in controls:
            lines.extend(_state_technical_lines(state, None))
            lines.append("-")
    if skipped_controls:
        lines.extend(["", "ПРОПУЩЕННЫЕ КОНТРОЛЫ", ", ".join(skipped_controls)])
    if errors:
        lines.extend(["", "ОШИБКИ / НЕПОЛНЫЕ ДАННЫЕ", *errors])
    return "\n".join(lines).rstrip() + "\n"


def safe_report_filename(prefix: str, timestamp: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", prefix.strip().lower()).strip("_") or "report"
    return f"{cleaned}_{timestamp}.txt"
