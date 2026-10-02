from __future__ import annotations

from core_models import WaveState


SEARCH_TOP_CRYPTO = 10


def select_top_crypto(states: list[WaveState]) -> list[WaveState]:
    """Return the hard TOP-10 crypto setups by displayed Rating, excluding controls."""
    crypto = [state for state in states if not state.is_control]
    crypto.sort(key=lambda state: state.rating if state.rating is not None else -1.0, reverse=True)
    return crypto[:SEARCH_TOP_CRYPTO]
