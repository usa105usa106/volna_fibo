# v0011

Focused UX + diagnostic release. Elliott/Fib formulas and production Search/Track semantics are not intentionally changed.

- Main Search/Track/manual result is now a high-resolution PNG table with vertical columns.
- Full technical state is attached as `.txt`; Telegram chat no longer gets the long pipe-delimited table unless PNG rendering falls back.
- `.txt` caption shows pure analysis duration, Top-N/scope, error count and best current crypto setups.
- Favorable table cells remain emphasized in PNG.
- XAU/USOIL remain outside TOP-10 crypto.
- `XAUT` and `UKOIL` are excluded from crypto Top-N to prevent commodity duplication.
- `/walk` now analyzes only BTC, ETH, SOL, BNB, XRP, DOGE, ADA, LINK, LTC and BCH.
- `/walk` output is a detailed `.txt` with per-signal and per-asset data for detector polishing.
- Added Pillow + DejaVu runtime support for Cyrillic PNG rendering.
- Bot version → `0011`.
