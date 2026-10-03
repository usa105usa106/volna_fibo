> Исторический документ исходной v0018. Подтверждённые результаты текущей проверки см. в `AUDIT_v0019.md`.

# Audit v0018

## Причина фикса

По live-скриншоту v0017 было видно, что ошибка уже не в множителях targets.

- BCH: working low 296.49 и T1 450.47 означают projection length 153.98, тогда как ручная parquet-разметка использует примерно 104.80.
- USOIL: working low 88.31 и T1 122.83 означают length 34.52, тогда как ручной reference использует 22.71.

Следовательно v0017 подставлял слишком поздний/ложный W3-(1) high. Target engine честно умножал неправильную длину.

## Исправление

1. W3-(1) anchor замораживается после первого senior qualifying W3-(2).
2. Поздние higher highs не меняют projection length этой структуры.
3. Isolated MEXC upper-wicks фильтруются до wave/target projection.
4. Global child lineage сохраняет собственные origin/high/low, а не старые parent anchors.
5. Добавлен H4 ancestry fallback для GRAM-like cases, когда старый parent не попал в daily candidate enumeration.
6. `/walk` для W3-(2) дедуплицирует по senior parent W2, чтобы higher highs той же W3 не превращались в новые «fresh» сигналы.

## Проверки в текущей среде

- `python -m compileall` по проекту: PASS.
- v0018 isolated-wick BCH regression: PASS.
- v0018 isolated-wick USOIL regression: PASS.
- target projection regressions: PASS.
- parquet/manual synthetic regressions для BCH/GRAM/USOIL/DOGE: PASS.
- targeted senior hierarchy/anchor/target regressions: 25/25 PASS.
- `python -m compileall` после финальной сборки: PASS.

Полный `pytest` в этой среде не заявляется: отсутствуют runtime/dev packages `aiosqlite`/`aiogram`, а внешний pip недоступен.

## Ограничение проверки

Архив 01:04 содержит реальные parquet, но в текущем runtime нет `pyarrow`/`fastparquet`, поэтому exact replay этих parquet локально не выполнен. Golden references и live-screen arithmetic используются как regressions, а не как symbol-specific production overrides.
