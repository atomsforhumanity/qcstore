"""An optional SQLite index of a store, for questions across calculations.

The index is a cache: the store's files are the truth, and `Index.rebuild` remakes the
index from them. `Index.update` instead reads the journal lines it has not seen and
re-reads from the store every key they name, so updating and rebuilding end with the
same index (a journal line only says which key changed; the rows always come from the
store).

Tables:

- ``runs``: one row per run: ``key``, ``run``, ``time`` (UTC, ISO 8601), ``program``,
  ``version`` (as the output reports it), ``calctype``, ``method``, ``basis``,
  ``formula``, ``success``, ``error_kind``, ``wall_time`` (s), ``identities`` (JSON),
  ``withdrawn``, ``preferred`` (the run `Store.get` returns without identities),
  ``input`` (the input as JSON, files left out), and the environment it ran in, as
  its output reports it: ``host``, ``cpu_model``, ``devices`` (JSON list) and ``libc``.
- ``carries``: ``key``, ``run``, ``carry`` (its id), ``identities`` (JSON),
  ``evidence`` (JSON), ``time``: one row per run carried.
- ``tags``: ``key``, ``tag``.
- ``meta``: ``key``, ``run``, ``name``, ``value`` (strings and numbers as they are;
  lists and objects as JSON).

Query JSON columns with SQLite's JSON functions:
``json_extract(identities, '$.linus') = '8'``, ``json_extract(input, '$.keywords.maxiter')``.
"""

from __future__ import annotations

import json
import sqlite3
import warnings
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from qcdata import Structure

from .store import _canonical, _key_dir, _preferred, _run_time, _RunFiles

if TYPE_CHECKING:
    from .store import Store

__all__ = ["Index"]

#: The index's schema version; an index file of another version is rebuilt.
SCHEMA = 1

_TABLES = (
    """CREATE TABLE runs (
        key TEXT NOT NULL, run TEXT NOT NULL, time TEXT NOT NULL,
        program TEXT NOT NULL, version TEXT, calctype TEXT, method TEXT, basis TEXT,
        formula TEXT, success INTEGER NOT NULL, error_kind TEXT, wall_time REAL,
        identities TEXT NOT NULL, withdrawn INTEGER NOT NULL,
        preferred INTEGER NOT NULL, input TEXT NOT NULL, host TEXT, cpu_model TEXT,
        devices TEXT NOT NULL, libc TEXT,
        PRIMARY KEY (key, run))""",
    """CREATE TABLE carries (
        key TEXT NOT NULL, run TEXT NOT NULL, carry TEXT NOT NULL,
        identities TEXT NOT NULL, evidence TEXT NOT NULL, time TEXT NOT NULL,
        PRIMARY KEY (key, run, carry))""",
    "CREATE TABLE tags (key TEXT NOT NULL, tag TEXT NOT NULL, PRIMARY KEY (key, tag))",
    """CREATE TABLE meta (
        key TEXT NOT NULL, run TEXT NOT NULL, name TEXT NOT NULL, value,
        PRIMARY KEY (key, run, name))""",
    # How far each journal file has been read.
    "CREATE TABLE journals (path TEXT PRIMARY KEY, offset INTEGER NOT NULL)",
    # Changes started but not yet done: their keys are re-read on every update.
    """CREATE TABLE pending (
        path TEXT NOT NULL, n INTEGER NOT NULL, key TEXT NOT NULL,
        PRIMARY KEY (path, n))""",
    "CREATE INDEX runs_by_program ON runs (program, calctype)",
    "CREATE INDEX runs_by_formula ON runs (formula)",
    "CREATE INDEX tags_by_tag ON tags (tag)",
)
_STORE_TABLES = ("runs", "carries", "tags", "meta")
_COLUMNS = (
    "key run time program version calctype method basis formula success error_kind "
    "wall_time identities withdrawn preferred input host cpu_model devices libc"
).split()
_BOOLEAN = ("success", "withdrawn", "preferred")


class Index:
    """A store's SQLite index; `Store.index` opens one, up to date.

    Each method opens its own connection, so an index may be used from several
    threads and processes at once (on a local disk: SQLite's locks need one).

    Attributes:
        store: The store indexed.
        path: The SQLite file.
    """

    def __init__(self, store: Store, path: Path) -> None:
        """Open the index file, creating or rebuilding it if needed, and update it."""
        self.store = store
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._writing() as db:
            fresh = db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA
            if fresh:
                tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                for (name,) in tables.fetchall():
                    db.execute(f"DROP TABLE {name}")
                for statement in _TABLES:
                    db.execute(statement)
                db.execute(f"PRAGMA user_version = {SCHEMA}")
        if fresh:
            self.rebuild()
        else:
            self.update()

    def __repr__(self) -> str:
        """Show the file."""
        return f"Index({str(self.path)!r})"

    def update(self) -> None:
        """Bring the index up to date: re-read every key the new journal lines name."""
        with self._writing() as db:
            offsets: dict[str, int] = dict(
                db.execute("SELECT path, offset FROM journals")
            )
            pending = {(p, n): k for p, n, k in db.execute("SELECT * FROM pending")}
            done = self._read_journals(offsets, pending)
            for key in sorted(done | set(pending.values())):
                self._refresh(db, key)
            self._save_journals(db, offsets, pending)

    def rebuild(self) -> None:
        """Remake the index from every run in the store."""
        with self._writing() as db:
            for table in (*_STORE_TABLES, "journals", "pending"):
                db.execute(f"DELETE FROM {table}")
            # Journals first: a change still running is pending, so the next update
            # re-reads its key even if this scan saw the key before it finished.
            offsets: dict[str, int] = {}
            pending: dict[tuple[str, int], str] = {}
            self._read_journals(offsets, pending)
            for key in self.store._keys():
                self._refresh(db, key)
            self._save_journals(db, offsets, pending)

    def query(
        self,
        *,
        tag: str | None = None,
        identities: Mapping[str, str] | None = None,
        **columns: Any,
    ) -> list[dict[str, Any]]:
        """Runs whose columns equal the values given, oldest first within each key.

        Args:
            tag: Only calculations with this tag.
            identities: Only runs valid under these numerics, produced by them or
                carried to them: ``{"linus": "8"}`` matches every run whose linus
                numerics is 8, whatever its other programs'.
            **columns: Values of the ``runs`` columns (``program="xtb"``,
                ``success=True``, ``preferred=True``); None matches NULL.

        Returns:
            One dict per run, with ``identities``, ``input`` and ``devices`` parsed
            from JSON.

        Raises:
            ValueError: An unknown column, or a program name holding ``"``.
        """
        where: list[str] = []
        params: list[Any] = []
        for name, value in columns.items():
            if name not in _COLUMNS:
                raise ValueError(f"No column {name!r}; the columns are {_COLUMNS}.")
            where.append(f"{name} IS ?")
            params.append(value)
        if tag is not None:
            where.append("key IN (SELECT key FROM tags WHERE tag = ?)")
            params.append(tag)
        if identities:
            paths = []
            for program, numerics in identities.items():
                if '"' in program:
                    raise ValueError(f"Not a program name: {program!r}.")
                paths += [f'$."{program}"', numerics]
            match = " AND ".join(
                ["json_extract({0}.identities, ?) = ?"] * len(identities)
            )
            where.append(
                f"(({match.format('runs')}) OR EXISTS (SELECT 1 FROM carries WHERE "
                f"carries.key = runs.key AND carries.run = runs.run AND "
                f"{match.format('carries')}))"
            )
            params += paths + paths
        text = "SELECT * FROM runs"
        if where:
            text += " WHERE " + " AND ".join(where)
        rows = self.sql(text + " ORDER BY key, run", params)
        for row in rows:
            for name in _BOOLEAN:
                row[name] = bool(row[name])
            for name in ("identities", "input", "devices"):
                row[name] = json.loads(row[name])
        return rows

    def sql(
        self, text: str, params: Sequence[Any] | Mapping[str, Any] = ()
    ) -> list[dict[str, Any]]:
        """Run a read-only SQL query; return one dict per row, by column name.

        Example:
            How many calculations per molecule:

            ```python
            index.sql(
                "SELECT formula, COUNT(DISTINCT key) AS n FROM runs "
                "GROUP BY formula ORDER BY n DESC"
            )
            ```
        """
        uri = self.path.absolute().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=60)) as db:
            cursor = db.execute(text, params)
            names = [d[0] for d in cursor.description or ()]
            return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    # ------------------------------------------------------------------ internals

    @contextmanager
    def _writing(self) -> Iterator[sqlite3.Connection]:
        """One write transaction; writers wait for each other."""
        with closing(
            sqlite3.connect(self.path, timeout=600, isolation_level=None)
        ) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

    def _read_journals(
        self, offsets: dict[str, int], pending: dict[tuple[str, int], str]
    ) -> set[str]:
        """Read new journal lines; update ``offsets`` and ``pending`` in place.

        Returns:
            The keys of the changes that finished.
        """
        backend = self.store.backend
        done = set()
        for writer in backend.list("journals"):
            for batch in backend.list(f"journals/{writer}"):
                path = f"journals/{writer}/{batch}"
                start = offsets.get(path, 0)
                data = backend.get(path, start)
                end = data.rfind(b"\n") + 1  # a line still being written waits
                for line in data[:end].splitlines():
                    try:
                        event = json.loads(line)
                        if "done" in event:
                            key = pending.pop((path, event["done"]), None)
                            if key is not None:
                                done.add(key)
                        else:
                            pending[(path, event["n"])] = event["key"]
                    except (ValueError, KeyError, TypeError):
                        warnings.warn(
                            f"Skipping a malformed line in {path}: {line[:200]!r}",
                            stacklevel=3,
                        )
                offsets[path] = start + end
        return done

    def _save_journals(
        self,
        db: sqlite3.Connection,
        offsets: dict[str, int],
        pending: dict[tuple[str, int], str],
    ) -> None:
        db.execute("DELETE FROM journals")
        db.executemany("INSERT INTO journals VALUES (?, ?)", offsets.items())
        db.execute("DELETE FROM pending")
        db.executemany(
            "INSERT INTO pending VALUES (?, ?, ?)",
            [(path, n, key) for (path, n), key in pending.items()],
        )

    def _refresh(self, db: sqlite3.Connection, key: str) -> None:
        """Replace a key's rows with what the store holds now."""
        for table in _STORE_TABLES:
            db.execute(f"DELETE FROM {table} WHERE key = ?", (key,))
        runs = self.store._read_runs(key)
        preferred = _preferred(runs)
        names = ", ".join(_COLUMNS)
        values = ", ".join(f":{c}" for c in _COLUMNS)
        for run in runs:
            db.execute(
                f"INSERT INTO runs ({names}) VALUES ({values})",
                _row(key, run, preferred),
            )
            for carry in run.carries:
                db.execute(
                    "INSERT INTO carries VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        key,
                        run.id,
                        carry["id"],
                        _canonical(carry["identities"]),
                        json.dumps(carry["evidence"]),
                        carry["time"],
                    ),
                )
            db.executemany(
                "INSERT INTO meta VALUES (?, ?, ?, ?)",
                [
                    (key, run.id, name, _sql_value(value))
                    for name, value in run.record["meta"].items()
                ],
            )
        db.executemany(
            "INSERT INTO tags VALUES (?, ?)",
            [(key, tag) for tag in self.store.backend.list(f"{_key_dir(key)}/tags")],
        )


def _row(key: str, run: _RunFiles, preferred: str | None) -> dict[str, Any]:
    """A run's row in ``runs``, read from its record as stored."""
    output = run.record["output"]
    inp = output["input_data"]
    model = inp.get("model") or {}
    execution = output.get("execution") or {}
    return {
        "key": key,
        "run": run.id,
        "time": _run_time(run.id).isoformat().replace("+00:00", "Z"),
        "program": inp["program"],
        "version": output["results"]["provenance"].get("program_version"),
        "calctype": inp.get("calctype"),
        "method": model.get("method"),
        "basis": model.get("basis"),
        "formula": _formula(inp.get("structure")),
        "success": output["success"],
        "error_kind": (output.get("error") or {}).get("kind"),
        "wall_time": execution.get("wall_time"),
        "identities": _canonical(run.record["identities"]),
        "withdrawn": run.withdrawn is not None,
        "preferred": run.id == preferred,
        "input": json.dumps(inp, ensure_ascii=False),
        "host": execution.get("hostname"),
        "cpu_model": execution.get("cpu_model"),
        "devices": json.dumps(execution.get("devices", []), ensure_ascii=False),
        "libc": execution.get("libc"),
    }


def _formula(structure: Any) -> str | None:
    """The Hill formula of a stored structure, by qcdata's rule."""
    if not structure:
        return None
    try:
        return Structure.model_validate(structure).formula
    except ValueError:  # coordinates saved apart in an array file
        return None


def _sql_value(value: Any) -> Any:
    """A meta value as SQLite holds it: scalars as they are, the rest as JSON."""
    if value is None or isinstance(value, str | int | float):
        return value
    return json.dumps(value, ensure_ascii=False)
