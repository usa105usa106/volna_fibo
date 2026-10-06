from __future__ import annotations

from core_models import WaveState
from core_recovery import parse_recovery_status
from core_symbols import display_symbol, excluded_from_crypto_top
import math


SEARCH_TOP_CRYPTO = 10


def ranking_key(state: WaveState) -> tuple:
    rating = state.rating if state.rating is not None and math.isfinite(state.rating) else -1.0
    growth = state.growth_from_low_pct
    growth = growth if growth is not None and math.isfinite(growth) else math.inf
    _, holds = parse_recovery_status(state.fib_status)
    return (-round(rating, 1), growth, -holds, state.liquidity_rank or math.inf, state.symbol)


def select_top_crypto(states: list[WaveState]) -> list[WaveState]:
    """Return the hard TOP-10 crypto setups by displayed Rating, excluding controls."""
    crypto = [state for state in states if not state.is_control
              and state.wave_type not in {"W4", "W5", "W3-(5)", "W3-(3)"} and state.status != "PHASE_UNCERTAIN"
              and not excluded_from_crypto_top(display_symbol(state.symbol))]
    crypto.sort(key=ranking_key)
    return crypto[:SEARCH_TOP_CRYPTO]
