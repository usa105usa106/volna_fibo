# v0025

- Added an isolated BTC V1/V2/V3 state machine, inclusive structural box, permanent hard retirement, independent score evidence, PRIMARY/ALT and two-C4H hysteresis.
- Added corrected ETH double-bottom chronology, candle-derived internal five, candidate/confirmed transition, recovery Fib and W3-(3) projections recalculated on every completed-low re-anchor.
- Validate strict timestamp chronology before all Fib/target calculation, both for fresh and saved anchors. Verify saved anchors against the same exchange's fresh candles.
- Retain current candle OHLC/high for wick invalidation. Reject malformed, duplicate, missing, stale and inconsistent input without changing the saved count.
- Persist BTC/ETH lifecycle separately from tracked-set/timers, with revision checks and cancellation draining. Reset and Telegram failure cannot revive a retired count.
- Add full count reports and separate major context outside the W2/W3-(2) Top-10. Keep `/walk` isolated and forbid retrospective use of today's reference scenario.
- BTC/ETH-only full daily history requests and same-market ETH/BTC supporting ratio. Other assets' Elliott anchor selection and target geometry are unchanged.
- Replaced the shared Fib/status grid with recovery from working low to preceding high, for Search/manual/Track/walk and W4 scenario rows. Working-low correction depth remains separate.
- Show the actual consecutive CLOSED COMPLETE4H count without a 3/3 cap; downgrade on level loss and reset/recompute on re-anchor. Legacy grids/counters are never reused. Track downloads back to the frozen low when the rolling window is insufficient.
- Updated recovery-dependent rating, state and highlighting: a higher recovery level means stronger recovery; >=3 closes confirms recovery. This does not confirm ETH W3-(3) before acceptance above its impulse high.

See AUDIT_v0025.md, METHODOLOGY_v0025.md and audit/v0025 for evidence, test results and limits. No deployment was performed.
