# v0011 verification notes

This build is derived from the hardened v0010 flat release and intentionally keeps the senior-wave detector formulas unchanged.

Verified in the build environment:

- Python syntax/compile checks for all source files.
- PNG renderer produces a high-resolution Cyrillic table with 11 vertical columns.
- DOGS-style and ultra-small prices remain fixed decimal, never scientific notation.
- XAUT and UKOIL are excluded while ordinary JUP remains eligible.
- `/walk` source is hard-wired to the exact 10-major diagnostic set.
- report files are created in memory; no new parquet/candle/temp cache is introduced.

The current execution environment does not contain aiogram/aiosqlite, so the complete runtime pytest suite and live Telegram/Binance/MEXC smoke test must still be run in the deployment/runtime dependency environment.
