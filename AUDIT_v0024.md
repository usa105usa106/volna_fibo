# Проверка v0024

Дата: 05.10.2026. Проверены только указанные пользователем маршруты металлов и происхождение TON/GRAM. До изменения выполнен весь suite v0023: 347 passed, 9528 warnings. Подтверждённые причины сообщены пользователю до редактирования.

## Найденные причины в v0023

| Severity | Файлы и строки исходной v0023 | Причина |
|---|---|---|
| High | data_collector.py:119–126; data_exchanges.py:121–133,145–146 | Canonical XAU использует Spot-клиент; Futures-каталог и свечи Binance не запрашиваются. В каталоге Spot XAU/XAG отсутствуют, хотя доступны Futures. |
| High | core_symbols.py:49–60; data_exchanges.py:259–268,280; data_collector.py:115 | XAG превращается в XAG_USDT и не распознаётся как commodity. MEXC отображает SILVER(XAG), но API идентифицирует контракт как SILVER_USDT. Серебро также не получает существующую commodity-policy. |
| Low, отображение | services_formatter.py:154–159 | Успешно восстановленная родная история Binance имеет status=restored, но подпись источника выводится только для отдельного spot fallback. Отсутствие подписи не означает отсутствие загруженных свечей. |

`data_lineage.py:163–184,214–250` действительно выполняет HTTP, проверяет SHA-256 месячных архивов Binance и границы перехода. Подстановки готовых волн/цен для GRAM не обнаружено. Documented rename mapping нужен для идентичности актива; коэффициент 1:1 и даты не являются разметкой волн.

## Проверка источника TON

Через действующий код `load_daily_context` и настоящий HTTP успешно скачаны 9 месячных ZIP TONUSDT с официального data.binance.vision, каждый с отдельным CHECKSUM. Префикс: 270 закрытых D1, 03.10.2025–29.06.2026. Неполный последний день TON 30 июня и календарный разрыв перехода не превращаются в выдуманные OHLC.

Для текущего GRAM использованы неизменённые Binance Spot parquet свечи на 03.10.2026 17:54 UTC. Это смешанный по способу получения, но один и тот же биржевой рынок: живое скачивание старого TON + зафиксированные ранее реальные GRAM свечи. Проверка не доказывает доступность текущего GRAM REST из этой среды.

Результат детектора: родитель 1.124 (06.02.2026) → 2.907 (07.05.2026) → 1.286 (16.09.2026); активный импульс 1.286→1.74, low=1.46, W3-(2), targets 1.914 / 2.194572 / 2.648572 / 3.383144. Все значения выведены из свечей. Полные URL и SHA-256: `audit/v0024/live-binance-ton-verification.json`.

Для MEXC порядок остался прежним: TON_USDT futures, затем MEXC Spot TONUSDT/GRAMUSDT, затем Binance Spot. История спота проверяется отдельно и не подменяет фьючерсные OHLC или проекционные якоря. Существующие tests v0023 покрывают отказ native, оба fallback, несовместимость basis/идентичности/даты low, cancellation и отсутствие lookahead. Новый production-path test дополнительно проверяет Binance snapshot → HTTP ZIP → checksum → daily_context, включая 503 и неверную SHA-256.

## Ограничения живой проверки

- `https://fapi.binance.com/fapi/v1/exchangeInfo` вернул HTTP451: региональное ограничение этой среды. Проверены официальные спецификации и HTTP mock цепочка с обеими частотами, но настоящий текущий анализ XAU/XAG через Binance REST здесь не выполнен.
- MEXC contract ticker вернул HTTP200 с HTML `Site Unavailable`. Маршрут SILVER_USDT подтверждён официальной страницей контракта/объявлением, HTTP mock и validation pipeline. Настоящая доступность MEXC Silver klines с сервера пользователя не проверена.
- Нет live Telegram отправки, доступа к Coolify или Docker daemon. Зависимости, Docker COPY и версия проверены локально; deploy не выполнялся.
- Положительный тест routing означает, что свечи поступают к детектору. При отсутствии подтверждённой senior-структуры корректен NO_SETUP; при действительном повреждении данных по-прежнему DATA_INCOMPLETE. Готовая волна не навязывается по имени металла.

## Официальные источники

- Binance metal contracts: https://www.binance.com/en/academy/articles/how-to-trade-gold-and-silver-on-binance-futures
- Binance USD-M exchangeInfo/klines: https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data
- MEXC SILVER(XAG): https://www.mexc.com/futures/SILVER_USDT
- MEXC display rename: https://www.mexc.com/announcements/article/mexc-to-rename-xaut-paxg-and-silver-futures-17827791532996

Результаты тестов/статических проверок и проверка ограниченности diff приложены в `audit/v0024/`.

## Итог проверки

- Полный suite: **377 passed**, в том числе **30 новых** проверок; 14202 warnings, 26.13s. Старые version-asserts обновлены с 0023 на 0024; поведенческие проверки не удалялись.
- Ruff E9/F: pass. Полный Ruff: 52 существовавших замечания, 0 новых. Единственная новая ошибка сортировки imports теста устранена без изменения поведения.
- compileall: pass; pip check: no broken requirements.
- Bandit runtime: 0 findings.
- Визуально проверены подпись TON→GRAM, явный Futures market и секция XAG; пример `layout-only.png` содержит синтетическую строку XAG и не является анализом серебра.
- data_lineage, core_models, core_ranking, db_repository, services_scheduler, data_integrity, requirements и METHODOLOGY побайтово совпадают с v0023. В core_senior только общий список commodity names вместо прежних XAU/USOIL; ни одна формула не изменена.
- Документы и Docker COPY/version согласованы. Сборка Docker и deploy не выполнялись.
