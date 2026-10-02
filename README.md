> **Flat GitHub edition:** все исходники, тесты и документация лежат в корне репозитория; папки `app/`, `tests/`, `docs/` не используются. Функциональная версия бота остаётся **0010**.

# Telegram Senior Wave Scanner v0010

v0010 is a narrow hardening release on top of the independently reviewed v0009. See `AUDIT_v0010.md` and `CHANGELOG_v0010.md`. Elliott/Fib thresholds, ratings, zones, target formulas, XAU/USOIL handling, scheduler semantics and Telegram UX are intentionally unchanged.

Coolify-ready Telegram bot for **global W2 / senior W3-(2)** discovery and stateful accompaniment. Micro Elliott counts are intentionally excluded from structural labeling.

## Telegram keyboard

Only normal Telegram `ReplyKeyboardMarkup` buttons are used. The setting buttons are cyclic toggles: one Top button, one interval button and one exchange button.

```text
[ ● Поиск W2/W3-(2) ]   [ Сопровождение ]
[ Top-200 ]              [ 1 час ]
[ Binance Spot ]
[ Сброс ]                [ Пинг ]
```

Cycles:

```text
Top-100 → Top-200 → Top-300 → Top-100
30 мин → 1 час → 4 часа → 12 часов → 30 мин
Binance Spot ↔ MEXC Futures
```

`●` marks only the active action mode. Toggle buttons show the selected value directly.

## Search semantics

`Поиск W2/W3-(2)` is always a **fresh search from zero candle memory**:

- the selected exchange Top-N is fetched again;
- stable/pegged assets and known leveraged-token products are excluded before Top-N slicing; normal tickers such as `JUP` are not rejected merely because their name ends with `UP`;
- full configured 1H and 1D history is downloaded again for every asset;
- no parquet, candle database or prior OHLC cache is read;
- the senior count is rebuilt from scratch;
- only setups with `Rating >= MIN_RATING` are eligible;
- only the **10 highest-rated crypto setups** are displayed/tracked.

Search does **not** switch to accompaniment automatically. If Search is the current periodic mode, each scheduled cycle performs another full fresh discovery.

### Safe reset

v0010 no longer deactivates the previous accompaniment set at Search start.

```text
old tracked set remains intact
        ↓
new fresh Search calculates
        ↓
result is delivered to Telegram
        ↓
ONLY THEN the new set atomically replaces the old set
```

If the exchange/API/data scan fails or Telegram cannot receive the report after retries, the previous accompaniment set remains available.

A fresh Search is accepted as structurally complete only when at least `SEARCH_MIN_SUCCESS_FRACTION` of its crypto universe passes market-data loading/integrity checks (default 80%). This prevents a partial exchange outage from replacing a good tracked set with a broken one.

## Accompaniment semantics

`Сопровождение` analyzes only the set committed by the most recent **successfully delivered Search**. It never scans the wider market for replacements.

Persistent memory is limited to senior-wave state in SQLite: anchors, W2/W3-(2), strict origin, Fib state, targets, rating, etc. Current candles are downloaded fresh every accompaniment cycle.

If a tracked symbol has incomplete current market data, its prior confirmed senior state is not rewritten from bad candles.

## Manual ticker analysis

Type one ticker or several comma-separated tickers:

```text
DOGE
DOGE,POL,SOL
```

Only those tickers are analyzed. Manual analysis is fully fresh and does not alter Search/Accompaniment mode, tracked set or interval countdown.

`XAU` and `USOIL` may also be typed explicitly. If the selected exchange does not expose that exact instrument, the bot reports it as unavailable instead of substituting another market.

## XAU / USOIL source rule

v0010 has **no Yahoo/proxy commodity source**.

XAU/USOIL are resolved only from the currently selected exchange:

- `Binance Spot`: exact spot instruments only. Tokenized alternatives such as XAUT are not treated as XAU.
- `MEXC Futures`: exact `XAU_USDT` and `USOIL_USDT` contracts when present in the exchange ticker list.

They are always outside the crypto TOP-10. If one or both are absent from the selected exchange, they are simply skipped; their absence never fails the crypto Search.

Therefore `Top-100` means **100 crypto candidates**, plus any of XAU/USOIL that actually exist on the selected exchange.

## Candle integrity guard

Before a market history reaches the senior-wave detector, v0010 validates:

- required OHLCV columns;
- parseable/unique timestamps;
- 1H UTC-hour alignment;
- finite positive OHLC values;
- valid OHLC relationships (`low <= open/close <= high`);
- no internal candle gaps for 24/7 crypto history;
- reasonable freshness of the latest closed candle.

Control-specific candle-integrity tolerances are unchanged from v0009; malformed or duplicate XAU/USOIL candles are still rejected. v0010 intentionally does not change commodity-source or trading-hours logic.

A crypto history that fails these checks is `DATA INCOMPLETE` and cannot receive a Search rating. COMPLETE4H remains separately protected: it is built only from exactly four valid UTC-aligned closed 1H candles.

Daily timestamps are candle opens. W1 high is located in COMPLETE4H before accepting a later W2 low. Tracking never moves its confirmed COMPLETE4H checkpoint backwards; INVALID/RECOUNT states clear obsolete Fib values, targets, zones, retrace/growth/distance metrics and rating.

The detector also refuses to judge a daily W1 whose high predates the available COMPLETE4H history, preventing a long daily candidate from being evaluated from only a truncated 4H tail.

## API and Telegram retry/backoff

Exchange HTTP requests retry boundedly on transport/timeouts and retryable HTTP responses such as `429` and `5xx`, with exponential backoff and `Retry-After` support.

Telegram sends retry on rate-limit, network and Telegram server errors. Server-provided Retry-After is respected without shortening it. Broadcasts try every destination; permanently forbidden chats are removed. A temporary failure prevents Search commit and timer advancement even if another chat received the report. With no successfully delivered report for the selected mode, no automatic countdown is created; retry the initial analysis manually. Telegram does not provide an idempotency key, so a timeout after acceptance may still cause duplicate messages. The periodic interval anchor is written only after the final report was actually delivered and, for Search, the new tracking session was committed.

## Interval semantics

The interval countdown starts **after analysis delivery**, not at calculation start:

```text
analysis starts
      ↓
calculation
      ↓
final Telegram table delivered
      ↓
new tracked set committed (Search only)
      ↓
ONLY NOW 30m / 1h / 4h / 12h countdown begins
```

Changing the cyclic interval toggle applies live.

## /walk — isolated walk-forward diagnostic

`/walk` runs a separate no-look-ahead diagnostic of the detector and **does not modify Search/Accompaniment state or their timer**.

Default diagnostic:

- current selected exchange and current Top-N crypto universe;
- 6 historical checkpoints;
- checkpoints spaced 30 days apart;
- 30-day forward outcome horizon;
- only signals meeting the same `MIN_RATING` threshold;
- at every checkpoint the detector receives only candles that were closed by that historical time;
- outcome compares whether T1 or strict-origin invalidation occurred first;
- reports unresolved/ambiguous cases plus median MFE/MAE and rating buckets.

`/walk` downloads its extended history fresh into RAM for that one command and does not persist candle files. It uses today's Top-N universe for a practical detector diagnostic; it is not a reconstruction of historical exchange Top-N membership.

## Senior methodology

- Structural hierarchy: `1D -> COMPLETE4H`.
- COMPLETE4H comes only from four closed UTC-aligned 1H candles.
- 1m/5m/15m/1H never create new senior Elliott labels.
- Discovery targets only global W2 and senior nested W3-(2).
- Deep strict-valid corrections are preferred, but depth is only one rating factor.
- A wick below strict origin invalidates the old count immediately.
- A new lower COMPLETE4H low above strict origin may re-anchor the working W2/W3-(2).
- Re-anchor recalculates Fib, entry zones and senior targets.

See `METHODOLOGY.md` for the deterministic W1/W2/W3-(2) heuristic.

## Table

The Telegram result preserves the established column order:

```text
# | Актив | Рейтинг | Цена | Сейчас развивается | Рабочий low | Fib / статус | Рост от low | База — лимитка | На вынос — лимитка | Цели
```

Crypto rows are sorted strictly by Rating descending. Favorable values are bolded. XAU/USOIL, when available on the selected exchange, are rendered in a separate unranked section.

Scientific notation is never used for price/low/zone/target fields in Telegram. Small-token values are rendered in fixed decimal notation (for example `DOGS 0.00004901`, not `4.901e-05`).


## Reset

`Сброс` is a hard clean-state action. It stops the scheduler/current heavy work, clears the tracked session and runtime state, deletes bot-owned temporary/legacy files, restores `Top-100 · 1 час · Binance Spot · IDLE`, and leaves the Telegram bot itself online.

## Ping

```text
🟢 Pong

Отклик: 37.4 ms
Работает: 2д 14ч 27м
Память: 186 MB
Версия: 0010
```

## Access and anti-repeat behavior

There is no Telegram account whitelist in v0010: any account/chat that can reach the bot may use it.

Heavy Search/Accompaniment actions retain the 60-second per-chat anti-repeat cooldown. A global admission guard covers calculation, Telegram delivery and commit. Manual analysis and `/walk` also cannot overlap this full lifecycle. Reset cancels and drains owned network tasks and waits for a running detector call to finish before clearing state; computation runs off the event loop.

## Persistent data

The database `/data/senior_wave_bot.sqlite3` stores settings, report destinations and the senior-wave state required for accompaniment. SQLite recovery sidecars (`-wal`, `-shm`, `-journal`) belong to the database and must never be manually deleted. Obsolete Search sessions are removed in the same transaction that installs the new set. No parquet/candle cache exists. Legacy `/data/parquet` is removed on startup.

Use one bot replica because SQLite and the in-process scheduler assume one active worker.

## Coolify deployment

1. Deploy the repository as a Dockerfile application.
2. Mount persistent `/data` for SQLite.
3. Set `BOT_TOKEN` and the remaining variables from `.env.example`.
4. No public HTTP port is required; Telegram long polling is used. Do not configure an HTTP health check against a nonexistent endpoint.
5. Keep exactly one active container, including during redeployment. Use stop-first replacement: Coolify can otherwise overlap old/new containers even with one configured replica. Consistent Container Names or a Custom Container Name prevents application-level rolling replacement (see official Coolify documentation).
6. Configure a stop grace period that accommodates the longest detector call on your host; shutdown drains work before closing HTTP and Telegram sessions.
7. Upgrade from Astra v0008 keeps its SQLite state and report destinations. For upgrades from the original unreviewed v0008, each desired chat must interact once because that build did not persist destinations. Set `BOT_VERSION=0010` if your deployment overrides it.
8. Run a fresh Search after upgrading: old saved counts are retained for safety and are not automatically relabeled with the corrected W1/W2 chronology.
9. When upgrading into a differently named directory, preserve your existing Compose project name with `-p` or `COMPOSE_PROJECT_NAME`; otherwise Compose creates a different project-prefixed volume. Keep the existing Coolify persistent mount as well.

Runtime dependencies are locked to the exact package set used by the successful v0009 audit environment (`Python 3.12`). `requirements.txt` lists the direct production packages; Docker installs the fully pinned `requirements.lock.txt` so a future dependency release cannot silently change production behavior.

Local:

```bash
cp .env.example .env
# set BOT_TOKEN
docker compose up --build
```

## Tests

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m compileall -q app tests
ruff check --isolated --select E4,E7,E9,F app tests
bandit -r app
python -m pip_audit
```

The incoming v0009 archive contains the prior independent full-suite audit evidence. v0010 adds focused regressions for exact leveraged-token filtering (`JUP` must survive while real leveraged products do not), additional stablecoin exclusions, complete INVALID/RECOUNT derived-state cleanup, exact dependency pins and version consistency. See `AUDIT_v0010.md` for what was independently executable in this environment and what still requires the Coolify/runtime dependency set.

## Scope

This is an analysis/signal bot, not an execution bot. A valid Elliott/Fibonacci structure is not a guarantee of a rebound or profit.

The supplied Astra/Sol audit artifacts are retained as historical evidence under `audit/history/`; they describe earlier versions. Current v0010 changes and verification limits are documented in `AUDIT_v0010.md`.
