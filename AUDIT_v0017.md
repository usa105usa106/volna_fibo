# v0017 verification notes

## Цель версии

Зафиксировать target engine так, чтобы он не мог молча строить W3-(2) цели от старого/global импульса. Формула и senior anchors теперь проверяются независимо от wave-detection логики.

## Production-правила

`W2`:

`L = W1_high - W1_origin`

`W3-(2)`:

`L = W3-(1)_high - parent_W2_low`

Для обоих:

`T1/T2/T3/T4 = working_low + (1.000 / 1.618 / 2.618 / 4.236) * L`

Malformed W3-(2) без nested anchors не получает targets: tracking переводит его в `RECOUNT` с `TARGET ANCHORS INVALID`.

## Локальная проверка

- `python -m compileall` — PASS.
- Новый target-projection regression module: **6 PASS / 0 FAIL**.
- BCH / GRAM / USOIL golden target math — PASS точно до floating-point tolerance.
- Combined target + parquet + lifecycle + hierarchy regression sweep: **23 PASS / 0 FAIL**.
- Дополнительный доступный legacy synchronous sweep: **34 PASS / 0 FAIL**, 5 fixture/async tests пропущены ad-hoc runner; ещё два test-модуля не импортируются без `aiogram`/`aiosqlite`.

Полный `pytest` в этой среде честно не заявляется: runtime/dev dependencies `aiogram` и `aiosqlite` отсутствуют.

## Важная граница

v0017 гарантирует корректность **target projection при корректных senior anchors**. Она не может сделать правильные BCH/GRAM targets, если detector выбрал другую волну/другой working low. Именно поэтому `.txt` теперь печатает target source/origin/high/length: следующий ручной тест сразу покажет, проблема в wave hierarchy или уже в target engine.
