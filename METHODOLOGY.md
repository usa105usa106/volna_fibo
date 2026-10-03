# Senior-wave methodology used by v0017

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

1. W3-(1) должна быть meaningful senior leg: абсолютный рост от parent W2 не меньше `10%`, плюс мягкая проверка degree относительно parent W1.
2. Кандидат W3-(1) должен быть **record high с момента parent W2**. Lower-high bounce внутри коррекции не является новой W3-(1).
3. После high требуется как минимум две COMPLETE4H свечи и pullback выше parent W2.
4. W3-(2) retrace допускается `0.20 .. 0.97`. Неглубокие ~20–38.2% коррекции валидны, если сам impulse senior.
5. Если **после candidate high уже был COMPLETE4H close выше этого high**, данный pair считается завершённым для свежего поиска. Это не означает «стереть историю» — просто текущая W3-(2) уже сыграна.
6. Детектор продолжает вперёд по тому же parent count и может найти следующий senior record-high leg + текущий pullback. Это не разрешает lower-high микроволны: новый high обязан быть record high и пройти degree-фильтр.
7. Среди ещё активных вариантов берётся наиболее поздняя senior структура.

Такой lifecycle нужен, чтобы не застревать навсегда на старом раннем subwave (ошибка BCH в v0015), но и не объявлять каждый lower-high pullback новой senior W3-(2).

## Global-lineage fallback

Иногда nested W3-(1) геометрически выглядит как отдельная daily W1/W2. Тогда detector проверяет lineage:

- child origin должен совпадать с parent W2 в разумном допуске;
- child high должен быть senior record high от parent W2;
- после high не должно быть COMPLETE4H acceptance выше него;
- current correction low должен оставаться выше parent W2 и удовлетворять nested retrace.

Если это выполняется, hierarchy трактуется как **W3-(1) -> W3-(2)**, а не как новая глобальная W2. Это важный GRAM-like случай.

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

v0017 считает цели отдельным projection engine. Generic wave geometry не имеет права подменять projection anchors.

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

Zones являются execution context и строятся около текущего working low. Они не имеют права тянуть fresh entry обратно к старому global origin. v0017 не вводит symbol-specific zone hacks: сначала исправляется hierarchy/anchor selection.

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

Для W3-(2) diagnostic identity включает parent W2 и active W3-(1), чтобы новый действительно senior record-high leg был виден отдельно, а обычные re-observations одной коррекции подавлялись как duplicates.
