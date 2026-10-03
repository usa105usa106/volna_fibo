# Senior-wave methodology used by v0015

## Structural hierarchy

- 1D задаёт senior impulse/origin context.
- COMPLETE4H строится только из четырёх полностью закрытых 1H свечей.
- Новые senior anchors/re-anchors создаются только COMPLETE4H.
- 1H intrabar может немедленно инвалидировать count при strict-origin wick break, но не создаёт новый Elliott label.
- Бот ищет только **global W2** и **senior nested W3-(2)**. Микроволны не размечаются.

## Global W2 lifecycle

1. На 1D/COMPLETE4H определяется senior W1 origin -> W1 high.
2. W2 может углубляться/re-anchor, пока рынок не принял W1 high обратно.
3. Первый устойчивый COMPLETE4H acceptance выше W1 high фиксирует parent W2 и запускает поиск nested W3-(1) / W3-(2).
4. Более поздний pullback внутри уже идущей W3 не имеет права переписать старый global W2 low.
5. Если strict origin пробит wick'ом — старый count INVALID немедленно.

## Nested W3-(2) lifecycle

1. После parent W2 начинается senior W3-(1).
2. Локальные 4H подволны не считаются W3-(1): nested impulse должен быть значим и сам по себе, и относительно parent W1 degree.
3. Пока senior correction не сформирована, higher highs расширяют тот же W3-(1).
4. Когда senior W3-(2) определена, её W3-(1) high **замораживается как projection anchor**.
5. COMPLETE4H acceptance выше этого high НЕ удаляет W3-(2) и НЕ сдвигает W3-(1) high. Если позже появляется новый более низкий COMPLETE4H low выше strict origin, рабочий W3-(2) low re-anchor'ится, а цели пересчитываются от нового low по тому же frozen impulse.
6. Если strict origin (parent W2) пробит — nested count INVALID и нужен senior recount.

Такой lifecycle соответствует ручной parquet-разметке: BCH может сохранить W3-(1)=317.70, а W3-(2) re-anchor'ить к ~296 без переноса projection anchor на более поздние higher highs.

## Hierarchy / lineage

Если новый геометрический «global W1» фактически начинается от уже известного senior W2, он не создаёт новый глобальный count. Это **W3-(1)** внутри старшего parent, а его последующая коррекция — **W3-(2)**.

Именно это правило не позволяет GRAM `1.286 -> 1.740 -> 1.460` ошибочно печатать как новый W2.

## Active-state selection

Fresh Search пересчитывает всё с нуля и оценивает все валидные senior parents. Приоритет:

1. валидная senior **W3-(2)**;
2. валидная текущая **W2**;
3. внутри одного класса — более свежий senior parent W2, затем размер старшего impulse.

## Fib / recovery

Recovery измеряется COMPLETE4H closes по `.236/.382/.500/.618/.705/.786/.886/.950`. Intrabar reclaim сам по себе структурным подтверждением не считается. `3/3 C4H` сильнее `2/3`, `2/3` сильнее одиночного reclaim.

## Targets

Targets только senior:

`Tn = working_low + multiplier × locked_impulse_length`

где multipliers: `1.0 / 1.618 / 2.618 / 4.236`.

Для W2 locked impulse = global W1. Для W3-(2) locked impulse = `parent W2 -> frozen W3-(1) high`. При re-anchor W3-(2) меняется только working low; impulse length не дрейфует вслед за последующими higher highs.

Reference regressions:

- BCH: `parent W2 212.90 -> W3-(1) 317.70 -> W3-(2) 296.10` -> `400.90 / 465.6664 / 570.4664 / 740.0328`.
- GRAM: `1.286 -> 1.740 -> 1.460` -> `1.914 / 2.194572 / 2.648572 / 3.383144`.

## Rating

Rating — opportunity score, не «качество монеты». Учитываются senior degree, retrace, свежесть от low, COMPLETE4H recovery, T1 convexity, liquidity/execution и fragility до strict origin.

Для crypto W3-(2) 9.x требует действительно сильного durable recovery. `2/3 C4H` ограничивается `8.8`; слабая `3/3` recovery только выше `.618` ограничивается `8.4`. Это сделано по результатам подробного walk, где старые 9.x не показывали лучшего качества, чем 8.x.

## Search / Tracking

- Search каждый раз скачивает/строит candles заново; candle/parquet cache отсутствует.
- Accompaniment сопровождает только symbols последнего успешного Search.
- W2 после acceptance W1 high больше не re-anchor'ится как global W2.
- W3-(2) может re-anchor новым COMPLETE4H low выше strict origin, сохраняя frozen W3-(1) high.
- Strict-origin break инвалидирует старый count немедленно.

## /walk

`/walk` изолирован от Search/Track и всегда использует:

`BTC, ETH, SOL, BNB, XRP, DOGE, ADA, LINK, LTC, BCH`.

Каждый COMPLETE4H проверяется без look-ahead. Для W3-(2) identity привязана к parent W2, поэтому re-anchor одной и той же senior correction не считается новым fresh signal. Future 1H используются только после фиксации fresh signal для T1/invalid/unresolved и MFE/MAE.
