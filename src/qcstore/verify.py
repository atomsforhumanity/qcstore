"""Check a store's files: records complete and consistent, blobs intact, pointers right."""

from __future__ import annotations

from dataclasses import dataclass, field

from .store import (
    _RUN,
    Store,
    _key_dir,
    _pointer,
    _run_dir,
)

__all__ = ["Report", "verify"]


@dataclass
class Report:
    """What `verify` found.

    Attributes:
        keys: Calculations checked.
        runs: Complete runs checked.
        incomplete: Runs without ``record.json``: being written, or their writer died.
            Readers ignore them; they are not problems.
        problems: Everything wrong, one message each.
        repaired: Pointers rewritten (with ``repair``).
    """

    keys: int = 0
    runs: int = 0
    incomplete: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    repaired: list[str] = field(default_factory=list)


def verify(store: Store, *, repair: bool = False) -> Report:
    """Check every run of every calculation in a store.

    For each complete run: its record names its key and run, its output loads (every
    blob and array file matching its sha256), and the key recomputed from its input is
    the key it is filed under. For each key: ``current.json`` names the runs the rules prefer.

    Args:
        store: The store.
        repair: Rewrite each ``current.json`` that is missing or wrong (a writer died
            between storing a run and updating the pointer). Nothing else is changed.

    Returns:
        The report.
    """
    report = Report()
    for key in store._keys():
        report.keys += 1
        directory = _key_dir(key)
        for name in store.backend.list(directory):
            if _RUN.fullmatch(name) and not store.backend.exists(
                f"{directory}/{name}/record.json"
            ):
                report.incomplete.append(f"{directory}/{name}")
        try:
            runs = store._read_runs(key)
        except Exception as e:
            report.problems.append(f"{directory}: unreadable: {e}")
            continue
        for run in runs:
            report.runs += 1
            where = _run_dir(key, run.id)
            try:
                if (run.record["key"], run.record["run"]) != (key, run.id):
                    raise ValueError(
                        f"the record names {run.record['key']} {run.record['run']}"
                    )
                output = store._load(key, run.id, blobs=True)
                if store.key(output.input_data) != key:
                    raise ValueError("its input's key is not the key it is filed under")
            except Exception as e:
                report.problems.append(f"{where}: {e}")
        if not runs:
            continue
        stored = store._pointer_now(key)
        if stored != _pointer(runs):
            if repair:
                store._update_pointer(key)
                report.repaired.append(f"{directory}/current.json")
            else:
                state = "missing" if stored is None else "stale"
                report.problems.append(
                    f"{directory}/current.json: {state}; `verify --repair` rewrites it"
                )
    return report
