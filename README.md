# Telegram Senior Wave Scanner v0013 — flat GitHub edition

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

## v0013: senior-wave hierarchy calibration

v0013 исправляет расхождение production-detector с ручной parquet-разметкой senior-волн:

- nested `W3-(2)` больше не требует аномально глубокого `50%+` отката. Для сильной senior `W3-(1)` допускается подтверждённая COMPLETE4H коррекция от `20%`, поэтому свежая nested-коррекция не остаётся ошибочно подписанной старой global `W2`;
- `Сопровождение` умеет на том же сохранённом активе повысить степень `W2 → W3-(2)`, если сформировались `W3-(1)` и новый senior pullback; новые символы при этом не ищутся;
- `База` и `На вынос` теперь строятся вокруг **активного working low**, а не уносятся к `.786–.950` старого parent impulse;
- рейтинг снова является opportunity/asymmetry score: учитывает senior degree, глубину коррекции, свежесть от low, устойчивость COMPLETE4H recovery, ликвидность и **запас до T1**. Старый глобальный W2, от которого цена уже далеко ушла, больше не должен получать преимущество перед свежим nested W3-(2).

Калибровочный regression-case повторяет найденную на BCH ошибку: parent W2 `212.9`, W3-(1) `317.7`, nested low `296.1` (~20.6% correction). Он обязан размечаться как `W3-(2)`, давать senior targets `400.9 / 465.6664 / 570.4664 / 740.0328`, а не оставаться старым W2 с T1 около 320.

Search, Сопровождение и ручной анализ присылают:

1. **PNG-картинку** с таблицей и вертикальными столбцами;
2. **`.txt` файл** с полной технической диагностикой;
3. подпись к `.txt`: время самого анализа, Top-N/число ошибок и кратко лучшие монеты.

Колонки PNG:

`# | Актив | Рейтинг | Цена | Сейчас | Раб. low | Fib / статус | Рост от low | База | На вынос | Цели`

В v0013 выделение стало **намеренно редким**. Жирный текст/зелёная ячейка означает реально благоприятный параметр, а не просто валидное поле:

- рейтинг `>= 8.5`;
- durable COMPLETE4H reclaim `3/3` уровня `.500` или сильнее (`.382/.236`);
- рост не более `+3%` от working low;
- цена реально внутри `База`/`На вынос`;
- потенциал до T1 `>= 30%`.

Сырые `Цена`, `W2/W3-(2)` и `Раб. low` сами по себе больше не выделяются жирным.

XAU/USOIL идут отдельным дополнительным блоком, если выбранная биржа их реально отдаёт. Если конкретного commodity-инструмента на бирже нет, он пропускается без поломки общего Search.

## Crypto universe и commodities

Top-N строится по выбранной бирже. Из crypto universe исключаются stablecoins, exact leveraged products, постоянные `XAU`/`USOIL`, а также дубли commodities `XAUT` и `UKOIL`.

`XAU` и `USOIL` анализируются отдельно и **только с выбранной биржи**. Yahoo/GC=F/CL=F fallback отсутствует.

## Search / Tracking

Search каждый раз скачивает полную свежую историю и пересчитывает рынок с нуля; candle/parquet cache отсутствует. Предыдущий tracked-set не удаляется до успешной доставки нового отчёта и atomic commit.

Сопровождение использует только senior-state последнего успешного Search. Оно не ищет новые монеты. Плохие/неполные свежие свечи не перезаписывают последний подтверждённый state.

Интервал следующего автоматического запуска отсчитывается **после успешной выдачи отчёта**, а не от начала расчёта.

## /walk v0013 — диагностический стенд для полировки детектора

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
- `Версия: 0013`.

## Docker

```bash
docker build -t senior-wave-bot:0013 .
docker run --rm -e BOT_TOKEN='...' -v senior_wave_data:/data senior-wave-bot:0013
```

Docker устанавливает `fonts-dejavu-core` для кириллицы в PNG-таблицах и locked Python dependencies из `requirements.lock.txt`.

## Проверки

```bash
python -m compileall -q .
python -m pytest -q
```

Для полного pytest нужны dev/runtime dependencies. v0013 добавляет regressions на BCH-like shallow nested W3-(2), active-low execution zones, convexity-aware rating, W2→W3-(2) promotion, sparse highlighting и detailed walk без двойного учёта одной senior-структуры.
