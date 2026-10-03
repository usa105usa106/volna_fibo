# Changelog v0019

Основа: предоставленная v0018; формула Fib и основные численные пороги сохранены.

- Исправлен fallback W3-(2): child origin используется одновременно для strict origin, parent W2, Fib и проекции.
- Lineage проверяет точную цену и дату, больше не склеивает разные волны по допуску 3%.
- Проверяется strict break до high; противоречивые projection anchors не публикуются.
- W1 не заканчивается на lower-high и не растягивается через уже сформированную W2.
- Nested senior high требует дневного подтверждения; устранена ранняя фиксация micro 4H-отката.
- Дневной parent context не теряется только из-за левой границы 1H, при этом новые 4H точки не выдумываются.
- Track применяет re-anchor до breakout/lock/promotion; учитывает low breakout-свечи. Результат не зависит от частоты опроса.
- Ранее достигнутые цели сохраняются в диагностике и исключаются из списка будущих целей.
- Recovery не наследует закрытия до нового low; улучшение persistence не уменьшает рейтинг. Tie-break соответствует инструкции.
- Дубли 1H отклоняются перед COMPLETE4H; nanosecond offsets не превращаются в правильные UTC slots.
- Сохранённые состояния 0018 пересчитываются на тех же тикерах при первом Track; автоматического Reset нет.
- Обновлены Telegram mocks под реальную выдачу PNG + TXT. Полный suite выполняется, а не заявляется по compileall.
- Добавлены read-only replay CLI, реальные parquet fixtures, provenance/SHA256, regression tests и полный отчёт.
- Версия, Docker label, .env.example и документация обновлены до 0019.

Финальные проверки: **222 passed**; Ruff E9/F и compileall пройдены; Bandit — 0 findings; runtime pip-audit — 0 известных уязвимостей. Полный replay — 302 актива, 0 ошибок данных. Остались 52 замечания расширенного Ruff и 2522 dependency warnings; live API/Telegram/Coolify ограничения раскрыты в `AUDIT_v0019.md`.
