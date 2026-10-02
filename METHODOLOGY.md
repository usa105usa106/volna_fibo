# Senior-wave methodology used by v0010

The bot is deliberately restricted to senior structure.

- Senior hierarchy: 1D -> COMPLETE4H.
- COMPLETE4H is built only from exactly four CLOSED 1H candles on UTC buckets 00/04/08/12/16/20.
- 1m/5m/15m/1H never create new Elliott labels.
- Search targets only global W2 and senior nested W3-(2).
- Deep W2/W3-(2) is desirable while strict origin remains intact.
- A wick below strict origin invalidates the old count immediately, even inside an incomplete 4H bucket.
- A new lower COMPLETE4H low above strict origin may re-anchor the working W2/W3-(2).
- Re-anchor recalculates depth, Fib state, entry zones and senior targets.
- Old targets are never retained after strict invalidation.
- Fib structural confirmation is evaluated by COMPLETE4H close/acceptance, not by a random intrabar reclaim.
- Rating is opportunity/convexity, not “coin quality”.

## Deterministic discovery heuristic

The migration state specifies how a senior count must be maintained but does not give a fully mechanical definition of a brand-new “clear senior W1”. v0010 keeps that engineering layer explicit:

1. Find local 1D pivots with a 3-day window.
2. A global W1 candidate must rise at least 18% and at least 3x daily ATR from a daily pivot low to a later daily pivot high.
3. A candidate W2 must retrace 50%–99.5% of W1 while remaining strictly above W1 origin.
4. A W1 high older than the available COMPLETE4H history is not eligible; the bot will not infer the correction from a truncated 4H tail.
   The daily timestamp denotes candle open. The matching high is located in COMPLETE4H within that daily candle; W2 lows must occur in later COMPLETE4H buckets. This corrects chronology without changing the pivot or retracement thresholds.
5. Nested W3-(1) is identified only after W2 and must advance at least 10%; W3-(2) must then retrace 50%–97% while staying above W2.
6. If a valid W3-(2) exists, it is preferred over the parent W2 for that asset row.

These thresholds are isolated in `DetectorConfig` so they can be validated without changing the strict-origin doctrine.

## Fib and targets

Retracement prices are calculated from the relevant impulse origin/high. The migrated nested DOGE impulse `0.07831 -> 0.10589` gives `.500 = 0.09210`, `.382 = 0.09535444`, `.236 = 0.09938112`.

Senior projection targets use `working_low + {1.0, 1.618, 2.618, 4.236} * impulse_length`.

For a fresh global W2, the same projection multipliers are applied to W1 length. This remains an explicit engineering rule because the migration note did not define a separate mechanical global-W2 target formula.

## Entry-zone automation

The migration state contains hand-maintained base/deep zones but no universal generation formula. v0010 therefore uses:

- `База — лимитка`: .786–.886 retracement band.
- `На вынос — лимитка`: .886–.950 band, clipped above strict origin.

## Fresh-data and integrity policy

Search and manual ticker analysis never reuse candle files or downloaded OHLC history. Tracking stores only senior state and downloads current market history fresh.

Before detection, OHLC histories pass integrity checks for timestamps, duplicates, OHLC validity, continuity and freshness. A broken crypto series is excluded from Search rather than silently generating a senior count.

XAU/USOIL are sourced only from the currently selected exchange. If unavailable there, no substitute market is used.

## /walk methodology

`/walk` is isolated from production Search/Accompaniment state. It uses historical checkpoints and slices the downloaded data at each checkpoint so the detector cannot see later candles. For each qualifying historical signal it observes the next configured horizon and records which event occurred first: T1, strict-origin invalidation, neither, or both within one 1H candle (ambiguous ordering).

The diagnostic uses the current Top-N membership as its universe. It is intended to detect obvious detector/rating weaknesses, not to claim a bias-free historical portfolio backtest.
