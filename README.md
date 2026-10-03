# Telegram Senior Wave Scanner v0017 — flat GitHub edition

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

## Что именно исправляет v0017

v0017 — это **target-engine hardening** поверх предыдущей версии. В этой версии цель — не очередной раз подкручивать wave hierarchy, а гарантировать, что после выбора структуры T1/T2/T3/T4 считаются тем же способом, что и в ручной parquet-разметке.

Правила теперь жёстко разделены по типу волны:

- `W2`: проекция строится только по длине предыдущей `W1 = W1 high - W1 origin`;
- `W3-(2)`: проекция строится только по длине `W3-(1) = W3-(1) high - parent W2 low`;
- коэффициенты фиксированы: `1.000 / 1.618 / 2.618 / 4.236`;
- база проекции — текущий `working_low` корректирующей волны;
- generic `origin/impulse_high` больше не могут случайно подменить nested anchors у `W3-(2)`;
- если у `W3-(2)` нет валидной пары `parent_w2_low + w3_1_high`, бот делает `RECOUNT`, а не публикует цели от старой/global W1;
- target anchors сохраняются прямо в `WaveState` и печатаются в `.txt` как `target_projection`, чтобы сразу было видно, откуда реально посчитаны цели.

Важно: эта версия **не маскирует неправильную wave-разметку**. Если detector выбрал не ту senior структуру, target engine не придумывает symbol-specific override. Но если anchors совпадают с ручной parquet-разметкой, targets обязаны совпасть точно.

## Golden references в тестах

v0017 содержит regressions на сохранённые parquet/manual anchors:

```text
BCH   W3-(2) targets: 400.90 / 465.6664 / 570.4664 / 740.0328
GRAM  W3-(2) targets: 1.914 / 2.194572 / 2.648572 / 3.383144
USOIL W3-(2) targets: 111.02 / 125.05478 / 147.76478 / 184.50956
DOGE  W3-(2) targets: 0.11871 / 0.13575444 / 0.16333444 / 0.20795888
```

Это regression anchors, а не symbol-specific production overrides: код не содержит `if BCH`/`if GRAM` для принудительной разметки.

## /walk v0017

`/walk` использует фиксированный diagnostic universe:

```text
BTC, ETH, SOL, BNB, XRP, DOGE, ADA, LINK, LTC, BCH
```

Он создаёт подробный `.txt` для полировки детектора: first/last seen, wave type, anchors, retrace, Fib, rating, targets, T1/invalid/unresolved, MFE/MAE до resolution и за 30 дней.

`/walk` не меняет Search/Track timers/state. Для W3-(2) dedupe учитывает parent W2 + active W3-(1), поэтому диагностика не скрывает реально сменившийся senior leg.

## Coolify

Минимально требуется:

```text
BOT_TOKEN=ваш_токен_от_BotFather
```

Persistent volume `/data` рекомендуется, но для самого запуска не обязателен. Без него состояние SQLite может исчезнуть при recreate/redeploy контейнера.

Пример Docker:

```bash
docker build -t senior-wave-bot:0017 .
docker run --rm -e BOT_TOKEN='...' -v senior_wave_data:/data senior-wave-bot:0017
```

## Проверка

В среде сборки выполняются syntax/compile и доступные regression tests. Полный pytest требует runtime/dev dependencies (`aiogram`, `aiosqlite` и др.). Если они отсутствуют и внешний pip недоступен, проект не заявляет фиктивный full-suite PASS.

Версия: **0017**.
