# Senior-wave methodology used by v0011

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

Deep strict-valid corrections are preferred, but depth never overrides strict-origin validity. INVALID/RECOUNT states drop obsolete Fib/targets/zones.

## Fresh Search

Search has no candle/parquet cache. Each run downloads the configured full exchange history and reconstructs the senior map from scratch. Only the successfully delivered Search is atomically installed as the new accompaniment set.

Top-N excludes stable/pegged assets, exact leveraged products, XAU/USOIL controls, and the duplicate commodity symbols XAUT/UKOIL.

## XAU / USOIL

XAU and USOIL are permanent separate controls outside the crypto TOP-10 and come only from the currently selected exchange. Absence/unavailability skips that control without breaking crypto discovery.

## Reporting

The trading methodology is unchanged by v0011. Only presentation changes:

- main table → PNG with true vertical columns;
- full diagnostic state → attached UTF-8 `.txt`;
- caption → analysis duration, scope/errors and best current crypto setups.

## /walk methodology

`/walk` is isolated from Search/Accompaniment state. It is a no-look-ahead diagnostic over a fixed liquid set:

`BTC, ETH, SOL, BNB, XRP, DOGE, ADA, LINK, LTC, BCH`.

For each configured historical checkpoint the detector sees only candles that had closed by that checkpoint. If a qualifying W2/W3-(2) exists, later 1H candles within the configured horizon are used only to classify the outcome:

- T1 first;
- strict invalidation first;
- unresolved;
- T1 and strict invalidation in the same 1H candle (ordering ambiguous).

The report records MFE/MAE, rating bucket, wave type and the exact state values needed to inspect detector errors. `/walk` writes no candle cache and changes no production session/timer.
