# AGENTS.md

Instructions for AI coding agents (and a quick reference for people) working in `qcstore`. CONTRIBUTING.md explains the same workflow in more detail.

## This package

`qcstore` keeps every run of every quantum chemistry calculation as plain JSON records, filed under qcdata's calculation key, with the numerics identities of the programs that produced each run. `docs/design.md` describes the layout and the concurrency rules; read it before changing how anything is written.

- It depends on `qcdata` and the standard library only. Do not add runtime dependencies.
- Correctness under concurrent writers and crashes comes before features. Every file is written once, atomically, through the `Backend`; `current.json` is the only file ever replaced; nothing in a run is ever deleted. A change to the write order or the files needs a test with real processes (`tests/test_concurrency.py`).
- The records are read by people and agents with `cat` and `jq`: keep them plain, uncompressed, and self-describing. A change to `record.json` bumps `FORMAT`.
- The index is a cache of the store: its rows always come from the store, never from journal lines.

## The qc* suite

Dependencies point one way: qcref -> qcdata -> {qcinf, qcstore, qccodec} -> qccompute. Never import a package that sits later in that order.

## Commands

```sh
uv sync --all-groups                  # install everything
uv run pre-commit install             # once per clone: run the checks on every commit
uv run pytest                         # tests
uv run ruff check --fix . && uv run ruff format .
uv run mypy
uv run pre-commit run --all-files     # exactly what CI checks
uv run mkdocs build --strict          # the docs
```

Use `uv add` / `uv remove` for dependencies; never edit `uv.lock` by hand.

## Code conventions

- Use type annotations for all function and method parameters (except `self` and `cls`) and return values, including `-> None`. mypy enforces this for library code.
- Prefer precise types; avoid `Any` unless genuinely necessary (JSON documents are `Any`).
- Write Google-style docstrings for all public modules, classes, functions, and methods (ruff enforces this). Start with a one-line summary ending in a period; add Args, Returns, Raises sections where applicable; state units of any number.
- Use `X | None`, not `Optional[X]`. Target Python 3.11.
- Keep changes focused. Do not reformat or refactor unrelated code.

## Before you finish

- `uv run pre-commit run --all-files` and `uv run pytest` pass.
- New behavior has tests; bug fixes have a test that failed before the fix.
- User-visible changes are listed under `## [unreleased]` in CHANGELOG.md.
- `pyproject.toml` has no `[tool.uv.sources]` path or git entries for qc* packages (CI rejects them).
- Do not commit, push, tag, or release unless asked to.
