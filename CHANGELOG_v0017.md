# v0017

Target-engine hardening после расхождений целей с ручной parquet-разметкой.

## Что исправлено

- Добавлен отдельный **projection-anchor resolver**. Цели больше не считаются напрямую из generic `origin/impulse_high` без проверки типа волны.
- Для `W2` разрешён только импульс `W1 origin -> W1 high`.
- Для `W3-(2)` разрешён только импульс `parent W2 low -> W3-(1) high`.
- Для `W3-(2)` запрещён fallback на global W1. Если `parent_w2_low` или `w3_1_high` отсутствуют/некорректны, state переводится в `RECOUNT`, targets очищаются.
- Коэффициенты целей жёстко зафиксированы: `1.000 / 1.618 / 2.618 / 4.236`.
- `working_low` остаётся базой проекции. При допустимом re-anchor low может уточниться, но длина импульса берётся только из разрешённой пары senior anchors.
- В `WaveState` теперь сохраняются `target_origin`, `target_impulse_high`, `target_impulse_length`, `target_source`.
- В обычном `.txt` и `/walk` выводится `target_projection`, поэтому можно проверить формулу без гадания по итоговым цифрам.

## Golden target regressions

- BCH W3-(2): `212.90 -> 317.70`, correction low `296.10` -> `400.90 / 465.6664 / 570.4664 / 740.0328`.
- GRAM W3-(2): `1.286 -> 1.740`, correction low `1.460` -> `1.914 / 2.194572 / 2.648572 / 3.383144`.
- USOIL W3-(2): `76.00 -> 98.71`, correction low `88.31` -> `111.02 / 125.05478 / 147.76478 / 184.50956`.
- Отдельный regression доказывает, что даже если generic/global anchors намеренно подставлены неправильные, W3-(2) targets всё равно строятся только из `parent W2 + W3-(1)`.

Версия runtime/Ping/Docker/docs: **0017**.
