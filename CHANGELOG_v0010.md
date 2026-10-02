# v0010 — focused hardening

v0010 is intentionally small. It is based on the independently audited/fixed v0009 archive and does **not** change the Elliott/Fibonacci methodology, rating formula, target formula, scheduler semantics, Telegram controls, XAU/USOIL source logic, or Search/Track workflow.

## Fixed

- Replaced suffix-only leveraged-token filtering (`endswith("UP"/"DOWN"/...)`) with an explicit leveraged-product set. A normal ticker such as `JUP` now remains eligible for Top-N, while known products such as `BTCUP` / `ETHDOWN` are still excluded.
- Centralized Top-N exclusions so Binance Spot and MEXC Futures use the same stable/control/leveraged rules.
- Expanded explicit stable/pegged exclusions with `AEUR`, `XUSD`, `USDG`, `USDQ`, `USDF`, and `AUSD`.
- INVALID/RECOUNT tracking now clears all obsolete count-derived values: retracement depth, Fib map, targets, entry zones, growth-from-low, strict-distance and rating. The row keeps only a fresh diagnostic status such as `STRICT ORIGIN BROKEN`, `MISSING STRICT ORIGIN`, `MISSING SENIOR ANCHOR`, or `RECOUNT REQUIRED`.
- Production package versions are pinned to the versions used by the successful v0009 Python 3.12 audit environment. Docker installs a fully pinned runtime lock file.
- Bot version changed everywhere in current runtime/docs to `0010`.

## Added regression coverage

- `JUP` must not be rejected merely because its name ends in `UP`.
- real leveraged products remain excluded;
- `AEUR` / `XUSD` do not consume Top-N slots;
- Binance and MEXC apply the same exclusion semantics;
- terminal INVALID/RECOUNT states cannot leak stale derived Fib/rating/zone data;
- direct runtime dependency pins and Docker lock usage are asserted;
- version `0010` is asserted.

## Explicitly unchanged

- XAU/USOIL code and integrity behavior;
- global W2 / senior W3-(2) detector thresholds;
- COMPLETE4H rules;
- strict-origin semantics;
- senior target calculations;
- Search reset/commit lifecycle;
- `/walk` logic;
- cyclic Telegram toggles and Reset behavior.
