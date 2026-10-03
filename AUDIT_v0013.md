# v0013 verification notes

This release is a targeted correction after comparing the production detector against the 2026-10-03 ~01:04 MSK parquet workflow and the BCH discrepancy reported in Telegram.

## Root cause confirmed

The bot correctly retained the BCH parent W2 around 212.9, but `_detect_nested()` required a nested correction of at least 50% of W3-(1). The parquet/manual senior map had W3-(1) around 317.7 and a confirmed nested low around 296.1: about a 20.6% correction. Therefore the child W3-(2) was discarded and the bot kept showing the stale global W2, stale T1 near 320 and very low rating.

## Fixes

1. Nested W3-(2) minimum retrace changed to 20%.
2. W2 accompaniment can promote to the same-parent W3-(2).
3. Entry zones anchor to current working low instead of deep parent Fib.
4. Rating includes T1 convexity and meaningful senior-degree progression while preserving depth/freshness/C4H/liquidity/strict-origin factors.

## Synthetic BCH regression result

Input senior anchors:

- parent W2 = 212.9
- W3-(1) = 317.7
- nested W3-(2) = 296.1
- live ≈ 311.09
- recovery = > .236 · 3/3 C4H

Observed with v0013 core:

- wave = W3-(2)
- nested retrace ≈ 20.61%
- rating ≈ 9.x (premium setup, depending on liquidity rank/current price)
- Base ≈ 296.1 → 301.x
- Sweep stays immediately around 296.x rather than falling back near the parent W2
- targets = 400.9 / 465.6664 / 570.4664 / 740.0328

## Verification executed in this environment

- `python -m compileall -q .` — PASS.
- direct v0013 core regression functions — PASS.
- tiny fixed-decimal formatting smoke check — PASS.
- flat ZIP structure/integrity — checked at packaging time.

Full pytest is not claimed in this environment because runtime dependencies such as `aiosqlite`/`aiogram` are not installed here. The repository contains the new regression tests for execution in Coolify/CI with `requirements-dev.txt` installed.
