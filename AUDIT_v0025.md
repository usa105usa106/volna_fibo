# v0025 — confirmed findings before implementation

Baseline: v0024, 377 tests passed (32.31 seconds). Scope: BTC/ETH plus the user's subsequent all-ticker recovery Fib/status correction.

* HIGH — core_senior.py:29,774–787: the generic W2 >=50% gate cannot represent the requested shallow ETH W2 and BTC box. No alternative-count state machine exists.
* HIGH — services_scanner.py:399 and db_repository.py:25–40: manual discovery has no durable independent count identity; hard invalidation would be forgotten on a new scan.
* HIGH — data_exchanges.py:32–39: current candle high is discarded, preventing upper-box wick invalidation.
* HIGH — user ETH reference chronology: 1849.54 is the 2026-06-15 12:00 UTC C4H high, preceding the second base 1512 on 2026-06-26 00:00 UTC. Measured post-base high preceding 1750.20 is 1846 on 2026-07-13 00:00 UTC. This demonstrable reference error is corrected explicitly, without modifying other assets.
* MEDIUM — user ETH internal fourth-wave reference 2386.19: the full correction low is 2356.41 on 2026-09-02 08:00 UTC. Confirmed hourly archive, aggregated to COMPLETE4H. The target impulse 1750.20–2807.34 and working low 2600.15 remain unchanged.
* HIGH — core_senior.py:117–140,1373–1375 (v0024): Fib/status uses the original impulse retracement grid instead of recovery from working low to impulse high.
* HIGH — core_senior.py:122–140,187–191 (v0024): the three-bar count is capped and sums nonconsecutive closes, so historical touches can appear as confirmation.
* MEDIUM — core_senior.py:355–433, core_ranking.py:16–17, services_formatter.py:120–128 (v0024): rating, state and highlighting parse the obsolete reverse grid and `3/3` labels. These dependencies must migrate together with the column.
* MEDIUM — rolling-window Track can omit the beginning of a long recovery streak. v0025 reloads back to the frozen low and fails closed on missing history instead of inventing an exact count.

Evidence: supplied v0024 search and walk reports, original Binance parquet snapshot from 2026-10-03, official Binance hourly monthly archives June–September 2026 (SHA-256 checked; provenance in fixtures/majors_v0025). Binance public-data timestamp/checksum documentation: https://github.com/binance/binance-public-data . MEXC candle schema: https://mexcdevelop.github.io/apidocs/contract_v1_en/ .

The supplied /walk has 58 historical fresh signals, 17 T1-first and 17 invalid-first, with 24 unresolved; it does not prove the latest BTC/ETH manual count. ETH signal 5 freezes 1510.97–1833 with low 1712.45, explaining the stale current output. Historical reference counts selected today must not be represented as unbiased past predictions.
