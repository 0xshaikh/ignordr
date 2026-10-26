# Contributing

Thanks for taking a look. `.gitignore` semantics are subtle and correctness is
the whole point of this project, so the bar for changes is high.

## Development setup

```bash
git clone https://github.com/0xshaikh/ignordr
cd ignordr
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
```

Python 3.9 or newer. **`git` must be on `PATH`** for the differential parity
suite; without it those tests skip, and you have lost the only real proof that
the engine is correct.

## How to propose a change

1. **New behaviour that differs from git** — don't. The engine is a faithful
   reimplementation. If git does something surprising, we match git and
   document the surprise. Open an issue first if you believe git is wrong.
2. **New finding code** — add it to `analyze.py`, add a test in `tests/test_cli.py`,
   and add it to the table in `README.md`.
3. **New pattern corner case** — probe real git first, then add a case to
   `tests/test_git_parity.py`. A unit test alone is not enough; the whole point
   of the parity suite is to stop the engine drifting from git.

## Running the parity tests

```bash
pytest tests/test_git_parity.py -v
```

These build throwaway repositories in a temp dir, run the real
`git check-ignore`, and compare verdicts with the engine. If one of these
starts failing, the engine has drifted from git — treat it as a bug in
`ignordr`, not in the test.

## Style

- Zero runtime dependencies. The standard library only. `pytest` is dev-only.
- Type hints on public functions. `__main__.py` and `engine.py` are checked by
  pyright in CI.
- Never write to a repository unless the user passed `--apply`. The read-only
  guarantee is asserted in the CI workflow; keep it that way.
- Output must be ANSI-clean when piped. Test with `--color never`.

## Reporting bugs

Include the output of `ignordr explain <path>` — it already contains the
decision trail — plus the exact `.gitignore` content and what you expected
instead. That is usually all that is needed.
