# Telegram Senior Wave Scanner v0011 — flat GitHub edition

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

## v0011: новый формат отчёта

Search, Сопровождение и ручной анализ теперь присылают:

1. **PNG-картинку** с нормальной таблицей и вертикальными столбцами;
2. **`.txt` файл** с полной технической диагностикой;
3. подпись к `.txt`: время самого анализа, Top-N/число ошибок и кратко лучшие монеты.

Колонки PNG:

`# | Актив | Рейтинг | Цена | Сейчас | Раб. low | Fib / статус | Рост от low | База | На вынос | Цели`

Благоприятные параметры выделяются жирным и мягкой подсветкой. XAU/USOIL идут отдельным блоком вне TOP-10 crypto.

Если PNG по локальной причине не удалось отрисовать, бот использует старый текстовый table fallback и всё равно прикладывает `.txt`.

## Crypto universe и commodities

Top-N строится по выбранной бирже. Из crypto universe исключаются stablecoins, exact leveraged products, постоянные `XAU`/`USOIL`, а также дубли commodities **`XAUT` и `UKOIL`**.

`XAU` и `USOIL` анализируются отдельно и **только с выбранной биржи**. Если конкретного инструмента на выбранной бирже нет, он пропускается и основной crypto Search не ломается. Yahoo/GC=F/CL=F fallback отсутствует.

## Search / Tracking

Search каждый раз скачивает полную свежую историю и пересчитывает рынок с нуля; candle/parquet cache отсутствует. Предыдущий tracked-set не удаляется до успешной доставки нового отчёта и atomic commit.

Сопровождение использует только senior-state последнего успешного Search. Оно не ищет новые монеты. Плохие/неполные свежие свечи не перезаписывают последний подтверждённый state.

Интервал следующего автоматического запуска отсчитывается **после успешной выдачи отчёта**, а не от начала расчёта.

## /walk v0011

`/walk` — отдельная диагностика детектора. Она **не использует текущий Top-100/200/300 как набор анализа** и всегда проверяет фиксированные десять ликвидных монет:

`BTC, ETH, SOL, BNB, XRP, DOGE, ADA, LINK, LTC, BCH`.

Текущий market Top-300 может быть легко запрошен только для liquidity-rank context; historical candle replay скачивается и считается исключительно для этих 10 монет.

`/walk` присылает **один `.txt` файл** для последующей проверки/полировки алгоритма. В нём:

- агрегаты T1-first / strict-invalid-first / unresolved / ambiguous;
- median MFE / MAE;
- разбиение по rating buckets;
- разбиение W2 против W3-(2);
- ошибки отдельно по каждой монете;
- каждый исторический signal: checkpoint, wave, rating, retrace, Fib status, entry, working low, strict origin, T1, MFE/MAE и outcome.

Search/Tracking state и их таймер `/walk` не меняет.

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
- `Версия: 0011`.

## Docker

```bash
docker build -t senior-wave-bot:0011 .
docker run --rm -e BOT_TOKEN='...' -v senior_wave_data:/data senior-wave-bot:0011
```

Docker устанавливает `fonts-dejavu-core` для кириллицы в PNG-таблицах и locked Python dependencies из `requirements.lock.txt`.

## Проверки

```bash
python -m compileall -q .
python -m pytest -q
```

Для полного pytest нужны dev/runtime dependencies. `test_v0011_features.py` содержит regressions для XAUT/UKOIL exclusion, fixed 10-major `/walk`, PNG renderer, tiny-price decimal formatting и версии 0011.
