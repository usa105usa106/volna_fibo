# Senior-wave methodology used by v0018

Цель детектора — только **global/senior W2 и W3-(2)**. Микроволны и обычные локальные откаты не должны получать senior label.

## Таймфреймы

- 1D задаёт global W1 candidates и старший degree.
- COMPLETE4H (строго четыре закрытые UTC 1H свечи) подтверждает W2/W3-(1)/W3-(2), recovery и re-anchor.
- 1H используется для strict invalidation внутри незакрытой 4H, но не создаёт senior labels.
- 15m не создаёт senior labels.

## Global W1 -> W2

1. Global W1 требует достаточную амплитуду относительно цены и ATR.
2. W2 ищется после W1 high и должна оставаться выше W1 origin.
3. W2 retrace: `0.50 .. 0.995`.
4. Пока рынок не закрыл COMPLETE4H выше W1 high, W2 может уточнять working low.
5. После COMPLETE4H acceptance выше W1 high эта W2 считается исторически завершённой как W2; поздний W3/W4 pullback нельзя переименовать в ту же старую W2.

## Senior W3-(1) -> W3-(2)

Для каждого валидного parent W2:

1. W3-(1) должна быть meaningful senior leg: достаточная амплитуда, degree и минимум несколько COMPLETE4H баров развития.
2. Lower-high bounce внутри коррекции не является W3-(1).
3. Один isolated MEXC upper-wick без подтверждения телом/соседними COMPLETE4H не может стать projection high.
4. W3-(2) retrace допускается `0.20 .. 0.97`.
5. **Ключевое правило v0018:** как только первый senior structural W3-(1) сформировал валидную W3-(2), его high замораживается для этой структуры. Последующие higher highs относятся к продолжению W3 и не пересчитывают старую W3-(1).
6. Working low W3-(2) берётся только из её correction window; после подтверждения continuation старые targets не re-anchor к следующему high.
7. Новый самостоятельный W3-(2) требует нового senior parent count, а не просто очередного higher high внутри уже идущей W3.

Это правило устраняет live-ошибку v0017, когда правильный BCH/USOIL working low оставался на месте, но более поздний high раздувал длину projection impulse и цели.

## Global-lineage fallback

Иногда реальный W3-(1)→W3-(2) геометрически выглядит как отдельная daily W1/W2. Тогда detector проверяет lineage:

- если child origin совпадает с известным parent W2, child автоматически наследует nested degree;
- если daily parent enumeration старую ветку не сохранил, COMPLETE4H history может доказать, что child origin сам является предыдущей senior W2;
- child high должен быть structural, а не isolated wick;
- child correction low должен оставаться выше child origin и удовлетворять nested retrace.

После такого доказательства используются **child origin/high/low**, а не более старые global anchors. Это generic GRAM-like repair без symbol-specific `if GRAM`.

## Strict invalidation

- Для W2 strict origin = W1 origin.
- Для W3-(2) strict origin = parent W2.
- Wick/low ниже strict origin на закрытой 1H или live low немедленно инвалидирует count.
- INVALID/RECOUNT очищает Fib, zones, targets, growth, strict distance и rating; старые derived values не должны протекать в отчёт.

## Fibonacci

Fib retracement строится по **тому impulse, который соответствует wave type**:

- W2: `global W1 origin -> global W1 high`.
- W3-(2): `parent W2 -> active W3-(1) high`.

Уровни: `.236, .382, .500, .618, .705, .786, .886, .950`.
Recovery статус оценивается только по COMPLETE4H close и последним трём COMPLETE4H.

## Targets

v0018 считает цели отдельным projection engine. Generic wave geometry не имеет права подменять projection anchors.

Для `W2`:

```text
L = W1_high - W1_origin
T1 = W2_low + 1.000L
T2 = W2_low + 1.618L
T3 = W2_low + 2.618L
T4 = W2_low + 4.236L
```

Для `W3-(2)`:

```text
L = W3-(1)_high - parent_W2_low
T1 = W3-(2)_low + 1.000L
T2 = W3-(2)_low + 1.618L
T3 = W3-(2)_low + 2.618L
T4 = W3-(2)_low + 4.236L
```

У `W3-(2)` отсутствие `parent_W2_low` или `W3-(1)_high` означает `RECOUNT`, а не fallback на global W1. В техническом `.txt` всегда выводятся `target_projection: source/origin/high/length`.

Golden references:

- BCH: `400.90 / 465.6664 / 570.4664 / 740.0328`;
- GRAM: `1.914 / 2.194572 / 2.648572 / 3.383144`;
- USOIL: `111.02 / 125.05478 / 147.76478 / 184.50956`.

## Execution zones

Zones являются execution context и строятся около текущего working low. Они не имеют права тянуть fresh entry обратно к старому global origin. v0018 не вводит symbol-specific zone hacks: сначала исправляется hierarchy/anchor selection.

## Rating

Rating — **opportunity score**, а не «качество монеты». Он учитывает:

- wave type/degree;
- retrace;
- свежесть относительно working low;
- COMPLETE4H recovery persistence;
- strict-origin distance;
- upside до T1;
- liquidity rank как небольшой tie-breaker.

Высокий rating не должен возникать только из-за label W3-(2). Слабая recovery ограничивает верхний score. После исправления hierarchy рейтинг снова проверяется `/walk`; его нельзя подгонять по одному BCH/GRAM примеру.

## Tracking

`Сопровождение` работает только по symbols последнего Search. Оно может повысить сохранённую W2 до W3-(2), если тот же parent count развился соответствующим образом.

Если COMPLETE4H уже принял цену выше projection high сохранённой коррекции, эта конкретная историческая структура блокируется от дальнейшего re-anchor. Следующий fresh setup появляется только через новый `Поиск`, а не путём переписывания прошлой сделки.

## /walk

`/walk` — отдельная диагностика fixed 10 majors. Он не меняет Search/Track state.

Для W3-(2) diagnostic identity привязан к senior parent W2. Повторные наблюдения, уточнение working low и последующие higher highs внутри той же W3 считаются одной живущей структурой, а не новыми fresh-сигналами.
