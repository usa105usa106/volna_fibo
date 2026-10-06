# BTC / ETH protocol v0025

## Scope and provenance

Only BTC/ETH use `MajorWaveEngine`. Other tickers retain the generic Elliott pivot/degree selector and target geometry. The additional user-authorized recovery Fib/status correction is shared across all tickers, including its rating/state/formatting dependencies. These BTC/ETH counts are explicitly requested, dated manual structural hypotheses, not a claim of universal automatic Elliott recognition. Prices in `core_major_rules.py` identify reference structures; production values come from the selected exchange's COMPLETE4H candles in the corresponding ±8-hour windows. A missing window, >3.5% reference deviation, chronology conflict, changed saved anchor or D1 disagreement yields DATA INCOMPLETE with no targets, never copied Binance prices on MEXC.

BTC/ETH request all available daily history from 2010 (pagination starts before listing) and hourly history at least from 2026-06-04. Other tickers keep their configured lookbacks. Senior pivots and re-anchors use exact four closed UTC hourly slots. 1H supports internal subdivisions and wick checks; smaller execution patterns cannot promote a new senior degree. This release does not need 15m execution signals to confirm these counts.

## BTC

V1 stays primary inside the inclusive exchange-adjusted box. Reference boundaries: 82300 and 94990.70. Valid low strictly below the lower boundary or high strictly above the upper boundary permanently retires V1. The current OHLC high is retained by the loader even if price closes back inside. A lower COMPLETE4H W4 low inside the box reduces its upper limit using the unchanged W3-(3) length. The length cap is a structural ceiling, not a profit target.

V2/V3 survive independently after box exit. Their strict origins are measured equivalents of 62275 and 57800.19. A score is not a probability: subdivision 0–3, 1D turning-point coherence 0–2, duration coherence 0–2, Fib quality 0–1, momentum 0–1, ETH/BTC consistency 0–0.5. Hard-invalid counts cannot win. V2 without a clean independent five remains a clearly marked conditional ALT, with subdivision UNPROVEN, rather than invented micro pivots. Equal scores choose V3 by degree parsimony. Differences below 2 explicitly remain UNRESOLVED. A soft switch requires an advantage >=2 at two consecutive different COMPLETE4H observations; duplicates do not count and a skipped bucket resets the streak. Hard invalidation switches immediately.

V1 projects no artificial extension targets. V2 projects W3-(3) from its correction low using the 62275–87395.67 reference impulse. V3 projects senior W3 from its W2 low using 57800.19–87395.67. Both lists and anchor provenance appear in the detailed report.

## ETH — user-corrected chronology

Double bottom 1505.68 / 1512; W1 1512–1846; W2 1846–1750.20. W3-(1) 1750.20–2807.34; internal wave-(1) high is measured from the structural pivot engine (1981.24 in the checked Binance data), followed by 1822.06, 2566.53, 2356.41, 2807.34. W3-(2) low 2600.15 is provisional.

The old reference 1849.54 is a June 15 high before the June 26 launch. It is not silently moved to July. The corrected July 13 high is 1846.00. All reference and derived anchor timestamps must strictly increase, including after JSON/SQLite reload; a violation is rejected before Fib/targets.

Until a COMPLETE4H close strictly exceeds 2807.34, ETH is `W3-(2) recovery / W3-(3) CANDIDATE`. A wick alone is insufficient. New lower COMPLETE4H lows above 1750.20 re-anchor all recovery levels and projections; intrabar lows do not move anchors. A valid wick strictly below 1750.20 permanently retires the count and emits `OLD COUNT INVALID — FULL SENIOR RECOUNT REQUIRED.`

Recovery is `low + r*(preceding_high-low)` for .236/.382/.500/.618/.705/.786/.886; status uses the latest COMPLETE4H close. Targets are `low + m*(2807.34-1750.20)`, m=1/1.618/2.618/4.236: 3657.29, 4310.60252, 5367.74252, 7078.19504 when low stays 2600.15. They are explicitly W3-(3) targets. Decimal arithmetic is used for projections, preserving small decimal precision elsewhere unchanged.

## Shared recovery Fib/status semantics

`Раб. low` is working low + correction depth `(high-low)/(high-origin)`. `Fib / статус` is recovery `low + r*(high-low)` for r=.236/.382/.500/.618/.705/.786/.886. This also applies to the recovery column of W4/W5 scenario rows, without altering their separate W5 targets.

Official recovery uses only the latest CLOSED COMPLETE4H close and the highest level it is strictly above. The count is the uninterrupted sequence of closes strictly above that exact level, ending at the latest bucket and beginning no earlier than the working-low bucket. It is not capped: `>R.236 · 7 C4H` is valid. A level loss selects the next lower held level and recounts its entire streak. Re-anchor rebuilds every level and count. A touch at R236 is `=R.236 · 0 C4H`; equality at a higher level falls back to the highest level strictly below the close. Missing 4H buckets, including commodity session closures, reset the streak.

No recovery: DEEP; one/two closes: RECOVERING (first reclaim / acceptance building); >=3: CONFIRMED recovery. Existing EXTENDED/invalid lifecycle rules take precedence. ETH wave confirmation remains a separate condition: recovery confirmation cannot turn a sub-2807.34 rebound into confirmed W3-(3).

Recovery rating weights retain the previous 0–1.6 range: R236 .35, R382 .9, R500 1.2, R618 1.45, R705/R786/R886 1.6, with +.15 for >=3 closes or -.15 for one. Weak recovery below R500 retains the 8.4 nested-crypto cap; first/second reclaim retains 8.5/8.8 caps and confirmed R500 retains the 8.9 cap. Other score components are unchanged. These are deterministic opportunity weights, not probabilities. Green recovery emphasis requires >=3 closes above at least R500.

Search/manual/Track and walk diagnostics share this calculation. Saved legacy grids and `3/3` counters are ignored and recalculated from candles even when no new bucket arrives. A Track window that begins after the working low is reloaded back to that low on the same market. If a full streak cannot be reconstructed, DATA INCOMPLETE preserves the saved state; no exact counter is fabricated.

Checked examples: BCH 296.49→366.88 gives R236=313.10204, so close315.84 is >R.236; XAU 4116.59→4701.48 gives R236=4254.62404, so close4143.57 is <R.236; USOIL 88.31→108.88 gives R236=93.16452, so close91.26 is <R.236.

## Durable state, reset, reports and walk

`major_counts` is a separate SQLite safety ledger keyed by market, instrument and dated protocol, with optimistic revision checks. Search/manual/Track update it on validated observations. This does not replace the tracked-set or report timer. It intentionally survives normal Reset, Search, report failure and restart: those actions must not resurrect hard-invalid counts. Once calculation starts, cancellation drains calculation plus its atomic safety write. A new full senior count requires a newly reviewed protocol; there is no automatic revival or silent re-anchor of old impulse origins.

The legacy tracked-set still commits only after successful delivery. BTC V1/W3-(5) and confirmed W3-(3) do not enter the W2/W3-(2) Top-10; their full context is sent separately and included in TXT. Score components, primary/alternative status, invalidation reasons, market and anchor times are auditable.

`/walk` never reads/writes the production ledger. Before protocol publication (2026-10-05 21:07 UTC) it uses the generic historical detector; afterwards it uses a private sequential book. Today's reference pivots or ETH/BTC context are not leaked into earlier checkpoints. The inherited 58 historical signals are not evidence of the new protocol's out-of-sample profitability.

ETH/BTC is a derived ratio of synchronous COMPLETE4H closes on the selected market, explicitly labelled as such; OHLC extrema are never divided into fictitious ratio candles. Higher close-range highs/lows add only a small soft score. Peer-data failure/mismatch removes the score, not the USD count. 0.07–0.08 is a conditional macro objective, never a guarantee or USD invalidation rule.

## Verification limits

Real Binance hourly archives June–September 2026 were checksum-verified and combined with the user's October 3 parquet suffix, plus the supplied 365-day D1 archive. Production requests full available D1; the offline fixture itself is only 365 days. MEXC prices in regression tests are explicitly synthetic basis-adjusted fixtures, not live verification. The live MEXC probe returned HTML instead of API JSON; Binance REST returned HTTP 451. No bypass was attempted. Telegram delivery and Docker/Coolify runtime were not exercised live. OHLC validation detects malformed/impossible candles and gaps; independently proving that an otherwise internally consistent exchange wick was a bad trade requires exchange trade data beyond this release.
