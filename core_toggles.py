from __future__ import annotations

TOP_LABELS = {100: "Top-100", 200: "Top-200", 300: "Top-300"}
INTERVAL_LABELS = {30: "30 мин", 60: "1 час", 240: "4 часа", 720: "12 часов"}
EXCHANGE_LABELS = {"binance_spot": "Binance Spot", "mexc_futures": "MEXC Futures"}

TOP_CYCLE = {100: 200, 200: 300, 300: 100}
INTERVAL_CYCLE = {30: 60, 60: 240, 240: 720, 720: 30}
EXCHANGE_CYCLE = {"binance_spot": "mexc_futures", "mexc_futures": "binance_spot"}


def next_top_n(current: int) -> int:
    return TOP_CYCLE.get(current, 100)


def next_interval_minutes(current: int) -> int:
    return INTERVAL_CYCLE.get(current, 30)


def next_exchange(current: str) -> str:
    return EXCHANGE_CYCLE.get(current, "binance_spot")
