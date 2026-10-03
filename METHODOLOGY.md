# Senior-wave methodology used by v0014

## Structural hierarchy

- 1D задаёт senior impulse/origin context.
- COMPLETE4H строится только из четырёх полностью закрытых 1H свечей.
- Новые senior anchors/re-anchors создаются только COMPLETE4H.
- 1H intrabar может немедленно инвалидировать count при strict-origin wick break, но не создаёт новый Elliott label.
- Бот ищет только **global W2** и **first nested W3-(2)**. Микроволны не размечаются.

## Global W2 lifecycle

1. На 1D/COMPLETE4H определяется senior W1 origin -> W1 high.
2. W2 может углубляться/re-anchor только пока рынок не принял W1 high обратно.
3. Первый COMPLETE4H close выше W1 high **lock'ит W2**.
4. После lock более поздний pullback внутри W3/W4 не имеет права переписать старый W2 low.
5. Если strict origin пробит wick'ом — старый count INVALID немедленно.

## Nested W3-(2) lifecycle

1. После W2 начинается W3-(1).
2. Пока senior pullback не достиг минимальной глубины, higher highs просто **расширяют тот же W3-(1)**.
3. Первый qualifying COMPLETE4H senior pullback `>=20%` от W3-(1) становится единственной W3-(2) этого parent W2.
4. Working low W3-(2) может re-anchor только пока W3-(1) high ещё не принят обратно.
5. Первый COMPLETE4H close выше locked W3-(1) high завершает/consumes эту W3-(2).
6. После consumption тот же parent W2 **не может** породить вторую W3-(2). Следующая коррекция относится к дальнейшему развитию W3, а не переименовывается в `(2)`.

Это правило специально защищает от предыдущей версии failure mode, где BTC/ETH и другие активы получали новую W3-(2) после почти каждого higher high.

## Active-state selection

Fresh Search пересчитывает всё с нуля и оценивает все валидные senior parents. Приоритет:

1. текущая **не consumed W3-(2)**;
2. текущая **не locked W2**;
3. внутри одного класса — более крупный senior parent impulse, затем более свежий working low.

Так более новый child-count не затирает всё ещё активный senior W3-(2).

## Fib / recovery

Recovery измеряется COMPLETE4H closes по `.236/.382/.500/.618/.705/.786/.886/.950`. Intrabar reclaim сам по себе структурным подтверждением не считается. `3/3 C4H` сильнее `2/3`, `2/3` сильнее одиночного reclaim.

## Targets

Targets только senior:

`Tn = working_low + multiplier × locked_impulse_length`

где multipliers: `1.0 / 1.618 / 2.618 / 4.236`.

Для W2 locked impulse = global W1. Для W3-(2) locked impulse = `parent W2 -> W3-(1) high`. После появления первой W3-(2) W3-(1) high больше не дрейфует вслед за дальнейшими higher highs.

Reference regressions:

- BCH: `parent W2 212.90 -> W3-(1) 317.70 -> W3-(2) 296.10` -> `400.90 / 465.6664 / 570.4664 / 740.0328`.
- GRAM: `1.286 -> 1.740 -> 1.460` -> `1.914 / 2.194572 / 2.648572 / 3.383144`.

## Rating

Rating — opportunity score, не «качество монеты». Учитываются senior degree, retrace, свежесть от low, COMPLETE4H recovery, T1 convexity, liquidity/execution и fragility до strict origin.

Shallow `20–38.2%` W3-(2) остаётся валидной, но больше не получает чрезмерный premium только за label. Для BCH-reference `.382 · 2/3 C4H` калибровочный rating = `8.8`.

## Search / Tracking

- Search каждый раз скачивает/строит candles заново; candle/parquet cache отсутствует.
- Accompaniment сопровождает только symbols последнего успешного Search.
- Consumed W2/W3-(2) не re-anchor'ится более поздними lows.
- Strict-origin break инвалидирует старый count немедленно.

## /walk

`/walk` изолирован от Search/Track и всегда использует:

`BTC, ETH, SOL, BNB, XRP, DOGE, ADA, LINK, LTC, BCH`.

Каждый COMPLETE4H проверяется без look-ahead. Для W3-(2) identity привязана к parent W2: **one parent = one W3-(2)**. Future 1H используются только после фиксации fresh signal для T1/invalid/unresolved и MFE/MAE.
