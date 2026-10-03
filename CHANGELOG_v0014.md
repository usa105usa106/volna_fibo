# v0014

Точечный structural-lifecycle fix поверх предыдущей версии.

## Senior-wave detector

- Исправлена главная ошибка предыдущей версии: один parent W2 больше не может порождать бесконечную цепочку новых `W3-(2)` после каждого следующего higher high.
- `W3-(1)` теперь **расширяется higher highs**, пока не появился первый qualifying senior pullback. Только первый senior pullback становится `W3-(2)` данного parent.
- После COMPLETE4H acceptance выше зафиксированного `W3-(1)` эта `W3-(2)` считается consumed/locked. Последующие pullbacks того же parent не переименовываются в новую `W3-(2)`.
- Global W2 может re-anchor только до первого COMPLETE4H close выше W1 high. После этого parent W2 locked; более поздние W3/W4 lows не двигают старый W2.
- Detector больше не откатывается к старому label `W2`, если W1 high уже принят обратно и W3 действительно началась, но активной first `W3-(2)` сейчас нет.
- При fresh search оцениваются все валидные senior parents. Активная nested `W3-(2)` имеет structural priority над более новым локальным W2, поэтому старший count не затирается child-count'ом.

## Targets / rating

- Senior targets по-прежнему считаются строго от **locked impulse**: `working_low + (1.0 / 1.618 / 2.618 / 4.236) × impulse_length`.
- Locked W3-(1) high больше не дрейфует после того, как первая W3-(2) уже сформировалась; это фиксит BCH-подобный сдвиг целей `400.90 -> 450+`.
- Добавлены regression anchors для пользовательской parquet-разметки:
  - BCH: `212.90 -> 317.70 -> 296.10`, targets `400.90 / 465.6664 / 570.4664 / 740.0328`;
  - GRAM: `1.286 -> 1.740 -> 1.460`, targets `1.914 / 2.194572 / 2.648572 / 3.383144`.
- Shallow `20–38.2%` nested correction больше не получает слишком большой автоматический rating premium. BCH-reference с `.382 · 2/3 C4H` калибруется к `8.8`, а не искусственному `9.x`.
- XAU/USOIL без crypto liquidity-rank получают neutral/high-liquidity control treatment, а не скрытый штраф.

## Tracking / walk

- Accompaniment не re-anchor'ит уже consumed W2/W3-(2) более поздним pullback'ом.
- `/walk` идентифицирует `W3-(2)` по parent W2, а не по каждому новому W3 high; один parent = максимум один senior W3-(2) signal.
- Fixed 10-major diagnostic, no-look-ahead outcome scoring и detailed `.txt` сохранены.

## UI / behaviour

- Формат PNG + `.txt`, sparse highlighting, XAU/USOIL controls, reset, cyclic toggles и fresh-search/no candle cache не менялись.
- Версия runtime/Ping/Docker/docs: `0014`.
