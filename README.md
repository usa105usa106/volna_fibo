# Telegram Senior Wave Scanner v0015 — flat GitHub edition

Все файлы проекта лежат **в корне репозитория**, без `app/`, `tests/` и других вложенных папок.

## Что делает бот

Бот ищет и сопровождает только senior Elliott setups: **global W2** и **nested W3-(2)**. Новые senior labels строятся по 1D + COMPLETE4H; 1H используется для сборки закрытых COMPLETE4H и немедленной strict-origin проверки. Micro Elliott-разметка не создаётся.

Основные кнопки — обычная Telegram Reply-клавиатура:

- `Поиск W2/W3-(2)` — fresh scan с нуля;
- `Сопровождение` — только набор последнего успешного Search;
- циклический `Top-100 → Top-200 → Top-300`;
- циклический `30 мин → 1 час → 4 часа → 12 часов`;
- циклический `Binance Spot ↔ MEXC Futures`;
- `Сброс`;
- `Пинг`.

Можно написать `DOGE` или `DOGE,POL,SOL` — это одноразовый fresh-анализ только указанных тикеров.

## v0015: hierarchy repair after the v0014 regression

v0015 отменяет ошибочное правило v0014, из-за которого валидная W3-(2) исчезала после пробоя W3-(1) high. Именно поэтому BCH стал `NO_SETUP`, а GRAM продолжал печататься как W2.

Новая логика:

- nested W3-(1) должен быть senior по масштабу, поэтому микро-4H swings не получают глобальные Elliott labels;
- после формирования W3-(2) её W3-(1) high фиксируется как projection anchor; последующие higher highs его не двигают;
- W3-(2) **не исчезает** после acceptance выше W3-(1). Новый lower COMPLETE4H выше strict origin может re-anchor рабочий W3-(2) low, а targets пересчитываются по прежнему frozen impulse;
- если геометрический новый «global W1» стартует от уже известного parent W2, он автоматически трактуется как W3-(1), а коррекция как W3-(2). Это исправляет GRAM/USOIL-class mislabel;
- global W2 после acceptance W1 high больше не может переписываться поздним W3/W4 pullback;
- crypto W3-(2) rating 9.x теперь требует действительно сильной COMPLETE4H recovery, а не просто nested label и красивую convexity.

Reference regressions из пользовательской parquet-разметки:

- **BCH:** `212.90 -> 317.70 -> 296.10` = W3-(2), targets `400.90 / 465.6664 / 570.4664 / 740.0328`, reference rating `8.8`;
- **GRAM:** `1.286 -> 1.740 -> 1.460` = W3-(2), targets `1.914 / 2.194572 / 2.648572 / 3.383144`, reference rating `8.4`.

Search, Сопровождение и ручной анализ по-прежнему присылают PNG-таблицу + полный `.txt`; sparse bold/highlight и отдельный control-block XAU/USOIL сохранены.

## Crypto universe и commodities

Top-N строится по выбранной бирже. Из crypto universe исключаются stablecoins, exact leveraged products, постоянные `XAU`/`USOIL`, а также дубли commodities `XAUT` и `UKOIL`.

`XAU` и `USOIL` анализируются отдельно и **только с выбранной биржи**. Yahoo/GC=F/CL=F fallback отсутствует.

## Search / Tracking

Search каждый раз скачивает полную свежую историю и пересчитывает рынок с нуля; candle/parquet cache отсутствует. Предыдущий tracked-set не удаляется до успешной доставки нового отчёта и atomic commit.

Сопровождение использует только senior-state последнего успешного Search. Оно не ищет новые монеты. Плохие/неполные свежие свечи не перезаписывают последний подтверждённый state.

Интервал следующего автоматического запуска отсчитывается **после успешной выдачи отчёта**, а не от начала расчёта.

## /walk v0015 — диагностический стенд для полировки детектора

`/walk` не использует текущий Top-100/200/300 как набор анализа и всегда проверяет фиксированные десять ликвидных монет:

`BTC, ETH, SOL, BNB, XRP, DOGE, ADA, LINK, LTC, BCH`.

По умолчанию берётся **180 дней истории** (`WALK_HISTORY_DAYS=180`), результат каждого сигнала оценивается на горизонте **30 дней** (`WALK_HORIZON_DAYS=30`). Detector последовательно запускается на **каждом завершённом COMPLETE4H**, а не только на редких месячных checkpoints.

Главные правила walk:

- одна и та же живущая senior-структура считается сигналом **один раз**, при первом свежем qualifying observation;
- повторные наблюдения этой же структуры считаются отдельно как duplicates, но не улучшают статистику;
- если при первом qualifying observation цена уже `>= T1` или state уже `EXTENDED`, случай исключается из fresh-success статистики;
- MFE/MAE до первого исхода (`T1` или strict invalidation) считаются отдельно от полного `30d MFE/MAE`;
- future candles никогда не попадают в detector: они используются только после фиксации сигнала для оценки результата;
- `/walk` не меняет Search, Сопровождение, tracked-set или их таймер.

`/walk` присылает **один подробный `.txt` файл**. Внутри есть:

- fresh unique signals, resolved-only hit rate, invalid/unresolved/ambiguous;
- duplicates и already-T1/EXTENDED exclusions;
- breakdown по rating bucket и W2/W3-(2);
- pre-resolution MFE/MAE и полный 30d MFE/MAE;
- по каждой монете число COMPLETE4H checks и qualifying observations;
- по каждому сигналу structure_id, first/last seen, anchors, working low, strict origin, retrace, Fib status и все Fib levels, зоны, targets, liquidity rank, outcome, time-to-outcome и обе пары MFE/MAE;
- отдельный список исключённых несвежих структур.

Этот файл предназначен именно для последующей ручной проверки и полировки алгоритма.

## Сброс

`Сброс` прекращает автоматический режим, отменяет owned-задачи, очищает tracked/search state и runtime temp/cache artifacts, возвращает настройки к `Top-100 · 1 час · Binance Spot · IDLE`. SQLite recovery files (`-wal/-shm/-journal`) вручную не удаляются.

## Минимальная настройка Coolify

Обязательная переменная:

```env
BOT_TOKEN=1234567890:telegram_token_here
```

Остальные значения имеют defaults. Для сохранения сопровождения между recreate/redeploy рекомендуется persistent volume на `/data`.

## Версия и Ping

`Пинг` показывает только:

- время отклика;
- uptime;
- RSS memory;
- `Версия: 0015`.

## Docker

```bash
docker build -t senior-wave-bot:0015 .
docker run --rm -e BOT_TOKEN='...' -v senior_wave_data:/data senior-wave-bot:0015
```

Docker устанавливает `fonts-dejavu-core` для кириллицы в PNG-таблицах и locked Python dependencies из `requirements.lock.txt`.

## Проверки

```bash
python -m compileall -q .
python -m pytest -q
```

Для полного pytest нужны dev/runtime dependencies. v0015 добавляет regressions на BCH-like shallow nested W3-(2), active-low execution zones, convexity-aware rating, W2→W3-(2) promotion, sparse highlighting и detailed walk без двойного учёта одной senior-структуры.
