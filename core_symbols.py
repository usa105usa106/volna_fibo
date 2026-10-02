from __future__ import annotations

import re


# Crypto-universe exclusions. Keep these centralized so Binance/MEXC apply
# exactly the same rules. Stable assets must never consume a Top-N slot.
STABLE_BASES = frozenset({
    "USDT", "USDC", "FDUSD", "TUSD", "DAI", "USDE", "USDD", "PYUSD",
    "FRAX", "LUSD", "GUSD", "BUSD", "USDP", "USD1", "EURC", "EURI",
    "EURT", "USDS", "USD0", "RLUSD", "BFUSD", "AEUR", "XUSD", "USDG",
    "USDQ", "USDF", "AUSD",
})

CONTROL_BASES = frozenset({"XAU", "USOIL"})

# Exact leveraged-token bases only. Never infer leverage from a suffix alone:
# JUP is a normal token and must not be rejected merely because it ends in "UP".
_LEVERAGED_UNDERLYINGS = frozenset({
    "BTC", "ETH", "BNB", "ADA", "BCH", "DOT", "EOS", "FIL", "LINK",
    "LTC", "SXP", "TRX", "UNI", "XLM", "XRP", "XTZ", "YFI", "AAVE",
    "SUSHI", "1INCH",
})
LEVERAGED_BASES = frozenset(
    f"{underlying}{suffix}"
    for underlying in _LEVERAGED_UNDERLYINGS
    for suffix in ("UP", "DOWN", "BULL", "BEAR")
)


def excluded_from_crypto_top(base: str) -> bool:
    """Return True only for explicit non-crypto-top instruments."""
    return base in STABLE_BASES or base in CONTROL_BASES or base in LEVERAGED_BASES


def normalize_symbol(exchange: str, raw: str) -> str:
    token = raw.strip().upper().replace(" ", "")
    if token in {"XAU", "USOIL"}:
        return token
    token = token.replace("/", "").replace("-", "").replace("_", "")
    if token.endswith("USDT"):
        base = token[:-4]
    else:
        base = token
    if not base or not re.fullmatch(r"[A-Z0-9]{1,24}", base):
        raise ValueError(f"Некорректный тикер: {raw}")
    return f"{base}USDT" if exchange == "binance_spot" else f"{base}_USDT"


def display_symbol(symbol: str) -> str:
    if symbol in {"XAU", "USOIL"}:
        return symbol
    if symbol.endswith("_USDT"):
        return symbol[:-5]
    if symbol.endswith("USDT"):
        return symbol[:-4]
    return symbol
