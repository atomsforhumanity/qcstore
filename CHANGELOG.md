# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [unreleased]

### Changed

- Record paths are Windows-safe: a key `k1:<64 hex>` lives in `records/k1/<2 hex>/<64 hex>/`, with no colon in any name. Stores written by 0.1.0a1 (`records/<2 hex>/k1:<64 hex>/`) are not read; `verify` reports their directories as not part of the layout.
- Tags may not end with `.` or be a name Windows reserves (`con`, `nul`, `com1`, ...).

### Added

- `qcstore.__version__`.
- The README installs the alpha releases together and shows identities as qccompute records them.

## [0.1.0a1] - 2026-11-17

The first alpha of qcstore: a general store for quantum chemistry results, depending on qcdata only.

### Added

- `Store(uri)`: every run of every calculation, filed under `qcdata.calculation_key(inp)` (the input alone) as plain JSON records, on a local disk or NFS, with many writers at once and no locks. Each run records the numerics identities of the programs that produced it.
- `Store.put(output, identities, tags=(), meta=None)` adds a run and returns its id; it refuses warm-started session frames (`record.warm_start` true).
- `Store.get(key, identities=None, blobs=True)`: the preferred run (the latest success not withdrawn, else the latest run not withdrawn), among the runs made with, or carried to, exactly those identities, or among all runs.
- `Store.runs(key)`: every run with its identities, carries, withdrawal, meta, and the environment it ran in (host, CPU model, devices, libc).
- `Store.blob`, `withdraw`, `tag`, `untag`, `iter` and `copy_to`.
- `Store.carry(runs, identities, evidence)`: declares any selection of runs valid for other identities, with the evidence stored once (`carries/<sha256>.json`) and an empty marker on each run.
- `Store.index()`: an optional SQLite index fed by per-writer journals, with `update`, `rebuild`, `query` and `sql`; columns for identities, carries, tags, meta, the environment and each run's whole input.
- `python -m qcstore verify URI [--repair]`: keys recomputed, records complete, blob and carry digests, pointers consistent.
- `Backend`, the interface a store's files go through, and `LocalBackend` for local disks and NFS.
