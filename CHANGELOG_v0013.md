# v0013

## Senior hierarchy / BCH regression

- Lowered the minimum confirmed nested W3-(2) retracement from 50% to 20%. The 50% gate was the direct reason a BCH-like parent W2=212.9 → W3-(1)=317.7 → nested low=296.1 (~20.6%) stayed incorrectly labelled as the old global W2.
- Added a regression fixture for that structure. Expected nested targets are 400.9 / 465.6664 / 570.4664 / 740.0328.
- Accompaniment can now promote a saved W2 to W3-(2) on the same symbol when the child senior structure is confirmed. It still never discovers new symbols in Track mode.

## Execution zones

- Replaced old .786-.950 parent-impulse entry zones with working-low anchored zones.
- Base zone = working low → 23.6% recovery toward active impulse high.
- Sweep zone = narrow buffer immediately below working low, clamped above strict origin.
- This prevents old parent-Fib zones from being shown far below the active nested correction.

## Rating

- Reworked rating as current senior opportunity/asymmetry rather than old-parent depth score.
- Added explicit T1 upside/convexity.
- Added a stronger W3-(2) progression component.
- Kept deeper strict-valid corrections preferable within degree.
- Kept freshness, durable COMPLETE4H recovery, liquidity and strict-origin fragility.
- Late/extended structures receive an explicit penalty.

## Preserved

- 1D + COMPLETE4H senior hierarchy only; no micro Elliott labels.
- Strict-origin wick break invalidates immediately.
- Search remains fresh/no candle cache.
- Track remains symbol-fixed to last Search.
- XAU/USOIL exchange-only behavior unchanged.
- PNG + .txt reporting and sparse emphasis unchanged.
- Detailed fixed-10-major /walk remains isolated from production state.
