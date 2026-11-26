# Contributing to DevMem

## Security reports

Do not open a public issue for a security problem. Follow [SECURITY.md](SECURITY.md) and use the repository's
private vulnerability reporting: https://github.com/Thamer-A/DevMem/security/advisories/new

## Set up

Python 3.10 or newer. In a virtual environment, from the repository root:

```bash
python -m pip install -e ".[dev]"            # pytest, build, ruff
python -m pip install -e ".[dev,embeddings]" # only to work on the optional embeddings (fastembed)
```

The package lives in `plugin/devmem/` (the same folder is the Claude Code plugin). pytest imports it from there
without an install; a subprocess needs `PYTHONPATH=plugin`. Run the CLI from the working tree with
`PYTHONPATH=plugin python -m devmem <command>`.

## Tests

The full suite is slow: a CI job takes 20 to 65 minutes. While working, run the test files your change touches:

```bash
python -m pytest tests/test_pack_gate.py tests/test_scan_map.py -q -p no:cacheprovider   # a quick subset
python -m pytest tests -q -p no:cacheprovider                                             # the full suite
python -m evals.retrieval.run                    # offline retrieval eval, a few seconds; after retrieval changes
```

Stress tests carry the `stress` marker and are skipped by default and in CI (`python -m pytest tests -m stress`).
Tests build everything in a temp folder and must not need the network, an API key or the `claude` binary.

## What CI runs

`.github/workflows/tests.yml`, on every push and pull request:

```bash
python -m ruff check plugin/devmem tests evals   # ruff 0.17.5
python -m pyright                                # pyright 1.1.414, checks plugin/devmem
python -m pytest -q -p no:cacheprovider          # Windows and Ubuntu, Python 3.10 and 3.13, PYTHONUTF8=1
```

## Rules

- The core stays standard-library only (`dependencies = []`); anything heavier is an optional extra with a fallback.
- `fixtures/`, `evals/retrieval/cases/` and `tests/test_hooks_fixtures/` are recorded data: do not edit them, and
  never change an evaluation case to improve a score.
- Never run devmem against `fixtures/` in place; copy the project to a temp folder first.
- Do not commit anything from a real `.devmemory/` store or real session content.

[CLAUDE.md](CLAUDE.md) has more detail on the layout and conventions.
