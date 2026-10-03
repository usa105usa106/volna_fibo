> Исторический документ исходной v0018. Подтверждённые результаты текущей проверки см. в `AUDIT_v0019.md`.

# Changelog v0018

## Target-anchor repair

- Исправлена главная live-проблема v0017: корректная формула целей могла получать неправильный W3-(1) high и раздувать T1/T2/T3/T4.
- Для W3-(2) projection high теперь фиксируется на первом senior structural W3-(1), который реально сформировал qualifying W3-(2).
- Последующие higher highs внутри продолжения W3 больше не переписывают уже установленный W3-(1) anchor.
- Добавлен фильтр isolated upper-wick для MEXC Futures: одиночный spike без close/neighbor confirmation не может стать senior projection high.
- Добавлен minimum COMPLETE4H development filter для слишком ранних micro W3-(1).
- Исправлен GRAM-like lineage fallback: daily child W1/W2, чей origin доказан как предыдущая senior W2, повышается до W3-(1) -> W3-(2) и использует собственные child anchors.
- Для lineage child больше не пересчитывается correction low по всему future-tail: сохраняются его собственные origin/high/low.

## Targets

Формула не менялась:

- W2: W2 low + (1.000 / 1.618 / 2.618 / 4.236) * длина W1.
- W3-(2): W3-(2) low + (1.000 / 1.618 / 2.618 / 4.236) * (W3-(1) high - parent W2 low).

Golden regressions остаются:

- BCH: 400.90 / 465.6664 / 570.4664 / 740.0328
- GRAM: 1.914 / 2.194572 / 2.648572 / 3.383144
- USOIL: 111.02 / 125.05478 / 147.76478 / 184.50956

## Diagnostics

- `target_projection` остаётся в техническом `.txt` и показывает source/origin/high/length.
- `/walk` dedupe для W3-(2) привязан к senior parent W2: последующие continuation highs и re-observations не считаются новыми fresh senior-сигналами.

## Version

- Bot version: `0018`.
