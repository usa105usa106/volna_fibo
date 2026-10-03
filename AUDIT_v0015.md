# v0015 verification notes

## Почему v0014 был сломан
v0014 ошибочно удалял W3-(2) из текущего анализа после COMPLETE4H acceptance выше W3-(1) high. Из-за этого BCH стал NO_SETUP, а глубокие nested corrections (например GRAM) продолжали конкурировать как новые global W2.

## Что проверено в v0015
- BCH synthetic hierarchy: ранние микро-подволны не крадут W3-(1); senior high 317.70 заморожен; более поздний lower COMPLETE4H re-anchor сохраняет этот high.
- GRAM lineage: 1.286 -> 1.740 -> 1.460 классифицируется W3-(2), а не W2.
- BCH rating reference: 8.8 при >.382 · 2/3 C4H.
- GRAM rating reference: 8.4 при >.618 · 3/3 C4H.
- W3-(2) tracking re-anchor после пробоя W3-(1) high сохраняет projection length.
- Старые BCH hierarchy/target tests сохранены и адаптированы под правильный lifecycle.
- Python compileall выполняется отдельно перед упаковкой.

Полный pytest в этой среде может не запускаться из-за отсутствующих runtime/dev зависимостей (aiosqlite/aiogram). Поэтому никаких выдуманных «все тесты прошли» не заявляется.

## Локальный прогон в этой среде
- `python -m compileall -q .` — PASS.
- 56 синхронных core/regression тестов без внешних runtime fixtures — PASS, 0 FAIL; 3 async/fixture теста пропущены в ручном прогоне.
- Отдельный `test_v0015_parquet_regressions.py` — 4/4 PASS.
- Полный pytest не запускался из-за отсутствующего `aiosqlite` в текущей среде.
