# Senior-wave methodology used by v0013

## Structural hierarchy

- 1D identifies senior impulse/origin context.
- COMPLETE4H is built only from exactly four closed 1H candles.
- New senior anchors/re-anchors come only from COMPLETE4H.
- Intrabar 1H data may immediately invalidate a count if strict origin is wicked through.
- No 1m/5m/15m/1H move creates a new global Elliott label.

## Setups

The production detector searches only:

- global W2 after a senior W1;
- nested W3-(2) after W2 → W3-(1).

Deep strict-valid corrections are preferred, but depth never overrides strict-origin validity. A nested W3-(2) is allowed from a COMPLETE4H-confirmed correction of 20%+ of W3-(1); requiring 50%+ was too restrictive and kept active W3 structures mislabeled as their old global W2. INVALID/RECOUNT states drop obsolete Fib/targets/zones.

For an active setup, execution zones are anchored to the confirmed working low:

- `База`: working low through the first `.236` recovery of the correction back toward the impulse high;
- `На вынос`: only a narrow sweep buffer immediately below working low, never a recycled deep parent-Fib zone.

Senior targets remain projections from the current working low using the established impulse multipliers `1.0 / 1.618 / 2.618 / 4.236`.

Rating is an opportunity score. It combines degree/progression, retrace quality, distance from working low, COMPLETE4H recovery durability, T1 convexity, liquidity and strict-origin fragility. A fresh W3-(2) with large T1 room must outrank a stale global W2 whose price has already moved far from its low.

## Fresh Search

Search has no candle/parquet cache. Each run downloads the configured full exchange history and reconstructs the senior map from scratch. Only the successfully delivered Search is atomically installed as the new accompaniment set.

Top-N excludes stable/pegged assets, exact leveraged products, XAU/USOIL controls, and the duplicate commodity symbols XAUT/UKOIL.

## XAU / USOIL

XAU and USOIL are permanent separate controls outside the crypto TOP-10 and come only from the currently selected exchange. Absence/unavailability skips that control without breaking crypto discovery.

## Reporting

v0013 keeps the senior-only doctrine but corrects the production hierarchy/rating/execution-zone implementation described above. Reporting remains:

- main table → PNG with true vertical columns;
- full diagnostic state → attached UTF-8 `.txt`;
- caption → analysis duration, scope/errors and best current crypto setups.

## /walk methodology

`/walk` is isolated from Search/Accompaniment state. It is a no-look-ahead diagnostic over a fixed liquid set:

`BTC, ETH, SOL, BNB, XRP, DOGE, ADA, LINK, LTC, BCH`.

The diagnostic walks sequentially through **every completed UTC COMPLETE4H** in the configured historical window (`WALK_HISTORY_DAYS`, default 180). At each checkpoint the detector sees only candles that had already closed by that moment.

A senior structure is identified by its senior anchors, not by every re-observation of the same living pullback. Therefore:

- the same W2 or W3-(2) is counted once, at the first qualifying fresh observation;
- later observations of that same structure are recorded as duplicates but do not improve hit-rate statistics;
- a candidate already at/above T1, or already `EXTENDED`, is excluded from fresh-signal statistics;
- future 1H candles are used only after signal creation to classify T1-first / strict-invalid-first / unresolved / ambiguous;
- MFE/MAE to first resolution are kept separate from full-horizon 30d MFE/MAE;
- the report preserves anchors, Fib levels, zones, targets, liquidity rank, timing and exclusion reasons for manual detector polishing.

`/walk` writes no candle cache and changes no production session/timer.
