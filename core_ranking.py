from __future__ import annotations

from core_models import WaveState
import math
import re


SEARCH_TOP_CRYPTO = 10


def ranking_key(state: WaveState) -> tuple:
    rating = state.rating if state.rating is not None and math.isfinite(state.rating) else -1.0
    growth = state.growth_from_low_pct
    growth = growth if growth is not None and math.isfinite(growth) else math.inf
    match = re.search(r"(\d+)/\d+ C4H", state.fib_status or "")
    holds = int(match.group(1)) if match else 0
    return (-round(rating, 1), growth, -holds, state.liquidity_rank or math.inf, state.symbol)


def select_top_crypto(states: list[WaveState]) -> list[WaveState]:
    """Return the hard TOP-10 crypto setups by displayed Rating, excluding controls."""
    crypto = [state for state in states if not state.is_control
              and state.wave_type != "W4" and state.status != "PHASE_UNCERTAIN"]
    crypto.sort(key=ranking_key)
    return crypto[:SEARCH_TOP_CRYPTO]
