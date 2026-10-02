from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from core_models import AppSettings
from core_toggles import EXCHANGE_LABELS, INTERVAL_LABELS, TOP_LABELS


def _mode_label(active: bool, text: str) -> str:
    return f"● {text}" if active else text


def keyboard(s: AppSettings) -> ReplyKeyboardMarkup:
    """Compact ReplyKeyboard with cyclic setting toggles.

    Search/Tracking show a dot only when active. The setting buttons themselves show
    the currently selected value. Reset is an immediate hard return to defaults/IDLE.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=_mode_label(s.mode == "search", "Поиск W2/W3-(2)")),
                KeyboardButton(text=_mode_label(s.mode == "track", "Сопровождение")),
            ],
            [
                KeyboardButton(text=TOP_LABELS.get(s.top_n, "Top-100")),
                KeyboardButton(text=INTERVAL_LABELS.get(s.interval_minutes, "1 час")),
            ],
            [KeyboardButton(text=EXCHANGE_LABELS.get(s.exchange, "Binance Spot"))],
            [KeyboardButton(text="Сброс"), KeyboardButton(text="Пинг")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выбери действие",
    )


def clean_button(text: str) -> str:
    """Normalize current and legacy button markers."""
    text = text.strip()
    for prefix in ("● ", "✅ "):
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return text
