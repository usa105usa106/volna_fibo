# v0010 verification report

## Basis

The input v0009 archive carried independent Astra/Sol audit evidence. Its recorded final suite result is:

- `144 passed` in the supplied Python 3.12 audit environment;
- compileall: pass;
- Ruff correctness (`E4,E7,E9,F`): pass;
- Bandit: 0 findings;
- `pip check`: pass.

Those are historical v0009 results and are not relabeled as a fresh v0010 full-suite run.

## v0010 changes reviewed

Only four production areas were changed:

1. exact Top-N exclusion rules (`core_symbols.py`, `data_exchanges.py`);
2. cleanup of stale derived state after INVALID/RECOUNT (`core_senior.py`);
3. exact dependency pins / production lock (`requirements*.txt`, `Dockerfile`);
4. version/docs/tests for `0010`.

No XAU/USOIL runtime logic was changed.

## Checks executable in this environment

- `python -m compileall -q app tests`: **PASS**.
- direct regression script for Top-N filter: **PASS** (`JUP` accepted; `BTCUP`, `ETHDOWN`, `AEUR`, `XUSD`, `XAU` rejected from crypto Top-N as intended).
- mocked Binance Top-N adapter: **PASS** (`JUPUSDT`, `SOLUSDT` retained; stables/leveraged excluded).
- mocked MEXC Top-N adapter: **PASS** (`JUP_USDT`, `SOL_USDT` retained; stables/leveraged excluded).
- direct SeniorWaveDetector terminal-state regression: **PASS** for both `INVALID` and `RECOUNT`; stale Fib/targets/zones/retrace/growth/distance/rating are cleared.
- current source scan: no suffix-only leveraged filter remains in runtime exchange code.
- current runtime/docs version scan: `0010` is the active version; prior `0009` references are retained only inside historical audit evidence / filenames where they describe v0009.

## Full-suite limitation here

A fresh v0010 `pytest` run could not be executed in this container because the environment does not include `aiogram` or `aiosqlite`, and outbound package installation fails at DNS/network resolution. Docker is also unavailable here. The attempted install failed before modifying the project.

For that reason this report does **not** claim that the full v0010 suite passed locally. The new focused regressions are included in `test_v0010_hardening.py` and should be run in Coolify/CI with:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m compileall -q app tests
```

## Production dependency reproducibility

The exact direct production versions are pinned in `requirements.txt`. `requirements.lock.txt` additionally pins the runtime dependency graph to the package versions recorded in the successful v0009 Python 3.12 audit environment, and Docker installs that lock file.

## Conclusion

v0010 is a narrow hardening release. The previously identified `JUP` false-positive filter bug and stale INVALID/RECOUNT derived-state leakage are fixed without changing the trading methodology or XAU/USOIL behavior. The remaining acceptance step is a full dependency-backed test run / live Coolify smoke test.
