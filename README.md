# Telegram Senior Wave Scanner v0018 — flat GitHub edition

Telegram-бот для поиска только **global/senior W2 и W3-(2)** по Binance Spot или MEXC Futures. Все файлы проекта лежат в корне — архив специально плоский для простой загрузки в GitHub/Coolify.

## Основной режим

- `● Поиск W2/W3-(2)` — каждый цикл собирает свежие данные и начинает scan с нуля. Search сам по себе не включает сопровождение.
- `Сопровождение` — следит только за set последнего Search; новых symbols не добавляет.
- `Top-100 / Top-200 / Top-300` — циклический тумблер crypto universe.
- `30 мин / 1 час / 4 часа / 12 часов` — интервал следующего запуска считается **после завершения предыдущего анализа**.
- `Binance Spot / MEXC Futures` — источник market data.
- `Сброс` — останавливает цикл, очищает tracked state/temp data и возвращает defaults.
- `Пинг` — latency, uptime, memory, version.
- Ручной запрос: `DOGE` или `DOGE,POL,SOL` анализирует только указанные symbols с нуля.

XAU и USOIL анализируются дополнительно, если доступны на выбранной бирже. Их crypto/commodity duplicates (`XAUT`, `UKOIL`) не занимают места в crypto Top-N.

## Ответ бота

Основной анализ отправляет:

1. PNG-таблицу с вертикальными колонками;
2. `.txt` с полным техническим разбором;
3. подпись с временем анализа, выбранным Top-N, количеством ошибок и коротким summary лучших setups.

Хорошие значения подсвечиваются выборочно; сам факт наличия цены/W2/W3-(2)/working low не делает всю строку жирной.

## Что именно исправляет v0018

v0018 чинит **не коэффициенты целей, а выбор projection anchors**. Формула `1.000 / 1.618 / 2.618 / 4.236` в v0017 уже была правильной, но live MEXC мог подменить W3-(1) более поздним record-high/одиночным futures wick. Поэтому BCH при working low около `296.49` получал T1 `450.47` вместо района `400.90`, а USOIL — T1 `122.83` вместо `111.02`.

Новая логика:

- W3-(1) фиксируется на **первом senior structural impulse**, который после parent W2 реально сформировал валидную W3-(2);
- после появления W3-(2) последующие continuation highs больше не имеют права переписывать W3-(1) и раздувать цели;
- одинокие MEXC upper-wicks без close/peer confirmation не становятся senior projection high;
- слишком ранние 4H micro-legs отсекаются минимальной длительностью senior impulse;
- daily/global child, чей origin доказан как предыдущая senior W2, автоматически повышается до W3-(1)→W3-(2) — это GRAM-like случай;
- `W2` по-прежнему считает targets только от W1, `W3-(2)` — только от parent W2 → frozen W3-(1);
- если nested anchors неоднозначны, бот делает `RECOUNT`, а не публикует красивую, но ложную математику.

В `.txt` сохраняется `target_projection: source/origin/high/length`, поэтому теперь можно сразу увидеть не только конечные цели, но и точные anchors, которыми они были построены.

## Golden references в тестах

v0018 содержит regressions на сохранённые parquet/manual anchors:

```text
BCH   W3-(2) targets: 400.90 / 465.6664 / 570.4664 / 740.0328
GRAM  W3-(2) targets: 1.914 / 2.194572 / 2.648572 / 3.383144
USOIL W3-(2) targets: 111.02 / 125.05478 / 147.76478 / 184.50956
DOGE  W3-(2) targets: 0.11871 / 0.13575444 / 0.16333444 / 0.20795888
```

Это regression anchors, а не symbol-specific production overrides: код не содержит `if BCH`/`if GRAM` для принудительной разметки.

## /walk v0018

`/walk` использует фиксированный diagnostic universe:

```text
BTC, ETH, SOL, BNB, XRP, DOGE, ADA, LINK, LTC, BCH
```

Он создаёт подробный `.txt` для полировки детектора: first/last seen, wave type, anchors, retrace, Fib, rating, targets, T1/invalid/unresolved, MFE/MAE до resolution и за 30 дней.

`/walk` не меняет Search/Track timers/state. Для W3-(2) dedupe идёт по senior parent W2: higher highs внутри уже идущей W3 не создают новые «fresh» W3-(2) и не раздувают статистику.

## Coolify

Минимально требуется:

```text
BOT_TOKEN=ваш_токен_от_BotFather
```

Persistent volume `/data` рекомендуется, но для самого запуска не обязателен. Без него состояние SQLite может исчезнуть при recreate/redeploy контейнера.

Пример Docker:

```bash
docker build -t senior-wave-bot:0018 .
docker run --rm -e BOT_TOKEN='...' -v senior_wave_data:/data senior-wave-bot:0018
```

## Проверка

В среде сборки выполняются syntax/compile и доступные regression tests. Полный pytest требует runtime/dev dependencies (`aiogram`, `aiosqlite` и др.). Если они отсутствуют и внешний pip недоступен, проект не заявляет фиктивный full-suite PASS.

Версия: **0018**.
