# Changelog v0021

- BCH: старший подтверждённый D1 parent сохраняет приоритет над внутренними 4H коррекциями. На проверенных Binance свечах origin 212.8 заменён на 199.7; T1 449.7 → 462.8. Промежуточное дробление до 239 в релиз не вошло.
- Все targets, Fib, strict origin и рейтинг пересчитываются из одной согласованной пары origin/high. Коэффициенты 1/1.618/2.618/4.236 и шкала рейтинга сохранены.
- Недостаточная старшая H1-история теперь явно помечается DATA_INCOMPLETE без целей. Search не принимает её за успешный рынок; Track не затирает подтверждённую запись.
- TON→GRAM: same-exchange history adapter, официальные Binance ZIP с checksum, обработка микросекунд и исключение неполного final day; MEXC запрашивает свой TON_USDT. Чужие биржи и старый несвязанный GRAM не смешиваются.
- GRAM получает W3-(2) при доказанном старшем parent; при недоступной истории видна неопределённость. Возврат истории запускает повторный same-symbol recount.
- Первый Track после обновления пересчитывает сохранённые тикеры; INVALID/RECOUNT не оживают. Reset для обновления не требуется.
- Offline replay поддерживает проверенный hourly prefix и TON daily prefix, проверяет provenance/overlap и печатает реальное время snapshot.
- Добавлены реальные fixtures, regression tests, полный replay и диагностические отчёты. Итог: 262 tests passed; E9/F, compileall, Bandit, pip check и runtime dependency audit пройдены. Full-style Ruff: прежние 52 замечания.
- Dockerfile, defaults, .env.example и версия отчёта обновлены до 0021. Docker/Coolify/Telegram live deployment не выполнялся.

Подробности, исходные строки v0020, точные BCH anchors и ограничения — AUDIT_v0021.md.
