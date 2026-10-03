# v0014 verification notes

## User-reported failures addressed

1. BCH targets drifted from the validated senior map (`400.90 / 465.6664 / 570.4664 / 740.0328`) to `450+` because later higher highs were being reinterpreted as new W3-(1) anchors.
2. GRAM could be shown as global W2 while the validated parquet map was already nested W3-(2).
3. `/walk` exposed the systemic root cause: one parent W2 could manufacture many different W3-(2) signals as W3 printed successive higher highs.
4. Rating 9.x was too easy to obtain for shallow nested corrections and did not reliably rank better than 8.x in walk-forward output.

## Structural corrections

- Global W2 re-anchor window ends on first COMPLETE4H close above W1 high.
- W3-(1) extends through higher highs until the first qualifying senior pullback.
- First qualifying W3-(2) locks the W3-(1) anchor.
- After COMPLETE4H acceptance above that W3-(1), the W3-(2) is consumed; same parent cannot emit another W3-(2).
- Fresh detector evaluates all senior parents and prioritizes current active W3-(2) over child/local W2 counts.
- Tracking refuses to re-anchor consumed corrections.
- `/walk` identity for W3-(2) is parent-based: one parent = one W3-(2).

## Reference regressions

PASS:

- BCH reference targets from `212.90 -> 317.70 -> 296.10`:
  `400.90 / 465.6664 / 570.4664 / 740.0328`.
- GRAM reference targets from `1.286 -> 1.740 -> 1.460`:
  `1.914 / 2.194572 / 2.648572 / 3.383144`.
- BCH reference rating with `.382 · 2/3 C4H`, rank 52 and ~4.7% from low: `8.8`.
- Higher highs before a qualifying correction extend W3-(1), not create a new nested wave.
- Later higher highs after a completed W3-(2) do not create a second W3-(2) from the same parent.
- Consumed global W2 is not re-anchored by a later lower W3/W4 pullback.
- Existing v0014 BCH hierarchy regressions remain PASS.
- PNG/tiny-decimal/commodity-duplicate/fixed-10-walk feature regressions remain PASS.

## Static checks

- `python -m compileall -q .` — PASS.
- 34 zero-fixture legacy/core tests from `test_senior.py` — PASS after correcting pre-existing test-source parenthesis/self-scan mistakes.
- v0014 lifecycle/hierarchy/features direct regression set — PASS.

## Environment limitation

Full `pytest` cannot be executed in this build container because runtime/dev dependencies `aiosqlite` and `aiogram` are not installed and outbound pip/DNS is blocked. This is an environment limitation, not reported as a fictitious full-suite pass. Docker/Coolify installs the pinned dependencies from `requirements.lock.txt`.
