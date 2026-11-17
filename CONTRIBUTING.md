# Contributing to qcstore

Thanks for helping! This guide covers setup, the pre-commit workflow, tests, pull requests, and releases. Code conventions (typing, docstrings, units) are in [AGENTS.md](./AGENTS.md), which applies to people and AI agents alike.

## Development setup

You need [uv](https://docs.astral.sh/uv/). Then:

```sh
git clone https://github.com/atomsforhumanity/qcstore.git
cd qcstore
uv sync --all-groups   # creates .venv with the package and all dev tools
uv run pre-commit install           # run the checks automatically on every commit
```

## Pre-commit workflow

Every `git commit` runs these checks on the files you staged. All of them use the tool versions locked in `uv.lock`, so they behave the same on your machine and in CI.

| Check | What it does |
|---|---|
| `ruff check` | Lints (pyflakes, pycodestyle, import order, bugbear, pyupgrade, Google docstrings) and fixes what it can |
| `ruff format` | Formats Python code |
| `mypy` | Type-checks the package; library functions must be fully annotated |
| `typos` | Spell-checks code and docs |
| `detect-secrets` | Blocks commits that contain credentials or tokens |
| `uv lock --check` | Fails if `uv.lock` is out of date with `pyproject.toml` |
| file checks | Valid YAML and TOML, no merge-conflict markers, no files over 250 KB outside `tests/data/` and `docs/` |

If a hook fixes files (ruff often does), the commit stops so you can review the changes. Stage them and commit again:

```sh
git add -u && git commit
```

Run every check on the whole repository, exactly as CI does:

```sh
uv run pre-commit run --all-files
```

You can run the tools directly too: `uv run ruff check --fix .`, `uv run ruff format .`, `uv run mypy`. Please don't skip hooks with `--no-verify`; CI runs the same checks and will fail.

## Running tests

```sh
uv run pytest                               # all tests
uv run pytest --cov --cov-report=term-missing   # with coverage
```

## Pull requests

1. Branch from `next`.
2. Make your change with tests. Bug fixes should include a test that fails without the fix.
3. Add a line under `## [unreleased]` in [CHANGELOG.md](./CHANGELOG.md) for anything users would notice.
4. Open a pull request. CI runs the pre-commit checks and the test suite on the oldest and newest supported Python versions.

Never commit `[tool.uv.sources]` entries that point qc* packages at local paths or git branches; CI rejects them. When a change needs an unreleased version of another qc* package, release that package first.

## Releases (maintainers)

Alphas (`0.1.0a1`, ...) are tagged from `next`; final releases from `main`.

1. Set the version in `pyproject.toml`, date the changelog section, run `uv lock`, and
   commit "Release X.Y.Z" through a pull request.
2. Tag the release commit and push the tag: `git tag X.Y.Z && git push origin X.Y.Z`.
3. The tag starts the Release workflow. Approve the waiting deployment to the `pypi` environment in the Actions tab; the workflow then publishes to PyPI and creates the GitHub release from the changelog section. The workflow refuses a tag that doesn't match the version in `pyproject.toml`.
4. A final release tag also starts the Docs workflow, which builds the site and publishes it to
   [qcstore.docs.afh.org](https://qcstore.docs.afh.org). Run it
   by hand from the Actions tab ("Run workflow") to republish without a release.

## Documentation

The site is built with MkDocs from `docs/` and the package's docstrings:

```sh
uv run mkdocs serve        # live preview at http://127.0.0.1:8000
uv run mkdocs build --strict
```

Pull requests that touch the docs or source build the site in CI.
