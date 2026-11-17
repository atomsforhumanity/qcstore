"""The store: every run of every calculation, as plain JSON records on disk.

A calculation is named by its key, ``qcdata.calculation_key(inp)``: the input alone.
Each time it runs, `Store.put` adds a run, which is never changed or deleted afterwards.
A run records the numerics identities of the programs that produced it
(``{"tblite": "tblite 0.8.0"}``), so readers can tell results of different code apart
while the key stays the same across releases.

Layout (see ``docs/design.md``)::

    records/k1/<2 hex>/<64 hex>/        a key, k1:<64 hex>, as a path without a colon
        current.json                    the preferred run, overall and per identity set
        tags/<tag>                      an empty file per tag on the key
        <run>/record.json               the run: identities, meta, and the output
        <run>/blobs/<path>              logs, files and large arrays, named by JSON path
        <run>/withdrawn.json            only if withdrawn
        <run>/carries/<digest>          an empty file per carry of the run
    carries/<digest>.json               a carry: new identities and the evidence, once
    journals/<writer>/<batch>.jsonl     what each writer changed, for the index

Concurrency without locks: a run is new files, ``record.json`` written last as its
commit marker; markers and tags are files created exclusively; ``current.json`` is the
only file ever replaced, atomically, and every writer re-reads the runs after replacing
it until what it wrote holds.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import json
import os
import re
import secrets
import socket
import tempfile
import threading
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse

from pydantic import BaseModel
from qcdata import FileInput, Files, OutputBase, ProgramInput, ProgramOutput
from qcdata import calculation_key as _calculation_key

from .backend import Backend, LocalBackend

if TYPE_CHECKING:
    from .index import Index

__all__ = ["Carry", "Run", "Store", "Withdrawal"]

#: The version of the record layout, written in every ``record.json``.
FORMAT = 1

_KEY = re.compile(r"(k[0-9]+):([0-9a-f]{64})")
_RUN = re.compile(r"[0-9]{8}T[0-9]{6}\.[0-9]{6}Z-[0-9a-f]{8}")
_TAG = re.compile(r"[a-z0-9_-]([a-z0-9._-]{0,62}[a-z0-9_-])?")
#: Names Windows reserves for devices, with or without an extension.
_RESERVED = {"con", "prn", "aux", "nul"} | {
    f"{d}{i}" for d in ("com", "lpt") for i in range(1, 10)
}

Input = FileInput | ProgramInput
Output = ProgramOutput[Any, Any]
Identities = Mapping[str, str]


@dataclass(frozen=True)
class Withdrawal:
    """Why a run stopped being preferred.

    Attributes:
        by: The user who withdrew it.
        time: When (UTC).
        reason: Why.
    """

    by: str
    time: datetime
    reason: str


@dataclass(frozen=True)
class Carry:
    """Runs declared valid for other identities: new code, same results.

    Attributes:
        id: The sha256 of the carry's file, ``carries/<id>.json``.
        identities: The identities the runs are also valid for, ``{program: numerics}``.
        evidence: Why: what was compared, and how closely it agreed.
        by: The user who carried them.
        time: When (UTC).
    """

    id: str
    identities: dict[str, str]
    evidence: dict[str, Any]
    by: str
    time: datetime


@dataclass(frozen=True)
class Run:
    """One run of a calculation, as `Store.runs` lists it.

    Attributes:
        id: The run id: its UTC time and a random suffix.
        time: When it was stored (UTC).
        success: Whether the calculation succeeded.
        error_kind: The kind of failure, for a failed run.
        identities: The numerics identity of each program that produced it.
        carries: The other identities it was carried to.
        withdrawn: Why it was withdrawn, if it was.
        meta: What the writer stored with it.
        program_version: The program version its output reports.
        host: The machine it ran on, as its output reports.
        cpu_model: The machine's CPU model, as its output reports.
        devices: The GPU devices it ran with, as its output reports.
        libc: The C library version it ran with, as its output reports.
    """

    id: str
    time: datetime
    success: bool
    error_kind: str | None
    identities: dict[str, str]
    carries: list[Carry]
    withdrawn: Withdrawal | None
    meta: dict[str, Any]
    program_version: str | None
    host: str | None
    cpu_model: str | None
    devices: list[str]
    libc: str | None


@dataclass
class _RunFiles:
    """What a run's files say: its record and its markers, as stored."""

    id: str
    record: dict[str, Any]
    withdrawn: dict[str, Any] | None
    carries: list[dict[str, Any]]

    @property
    def valid_for(self) -> list[dict[str, str]]:
        """Every identity set the run's results are valid for: its own, then carried."""
        return [self.record["identities"], *(c["identities"] for c in self.carries)]


class Store:
    """Every run of every calculation, under its key.

    Example:
        ```python
        store = Store("./store")
        run = store.put(output, {"tblite": "tblite 0.8.0"}, tags=["screen"])
        store.get(store.key(inp), {"tblite": "tblite 0.8.0"})
        ```

    Attributes:
        backend: Where the files live.
    """

    def __init__(self, uri: str | os.PathLike[str] | Backend) -> None:
        """Open a store.

        Args:
            uri: A directory (a path or a ``file://`` URI), local or on NFS, made on the
                first write; or a `Backend`.

        Raises:
            ValueError: A URI scheme without a backend.
        """
        if isinstance(uri, str | os.PathLike):
            text = os.fspath(uri)
            if "://" in text:
                parsed = urlparse(text)
                if parsed.scheme != "file":
                    raise ValueError(f"No qcstore backend for {parsed.scheme}:// URIs.")
                text = unquote(parsed.path)
            self.backend: Backend = LocalBackend(text)
        else:
            self.backend = uri
        self._journal = _Journal(self.backend)

    def __repr__(self) -> str:
        """Show the backend."""
        return f"Store({self.backend!r})"

    # ------------------------------------------------------------------ the API

    def key(self, inp: Input) -> str:
        """The key a calculation is filed under: qcdata's `calculation_key`."""
        return _calculation_key(inp)

    def get(
        self, key: str, identities: Identities | None = None, *, blobs: bool = True
    ) -> Output | None:
        """Return the preferred run's output, or None if there is none.

        The preferred run is the latest success not withdrawn, else the latest run not
        withdrawn. With ``identities``, only runs produced by exactly those identities,
        or carried to them, count.

        Args:
            key: The calculation's key.
            identities: The numerics identity of each program the input names, as the
                caller would run them now; None for any.
            blobs: Load logs and files. False leaves them out (``logs`` None, ``files``
                empty) and reads only the record and its arrays.

        Returns:
            The output as stored, or None.
        """
        run = self._preferred(_check_key(key), identities)
        return None if run is None else self._load(key, run, blobs)

    def put(
        self,
        output: Output,
        identities: Identities,
        *,
        tags: Iterable[str] = (),
        meta: Mapping[str, Any] | None = None,
    ) -> str:
        """Add a run under the key of ``output.input_data``.

        Args:
            output: The calculation's output, successful or not.
            identities: The numerics identity of each program that ran,
                ``{program: numerics}``, as the runner saw them; the run is found
                again under exactly these.
            tags: Tags for the key.
            meta: JSON metadata stored with the run.

        Returns:
            The new run's id.

        Raises:
            ValueError: ``output`` is a session frame (``record.warm_start`` true): a
                warm start can reach another solution than the input alone, so it is
                not that input's result. Or a tag is invalid, or ``meta`` is not
                JSON.
            TypeError: ``identities`` are not strings to strings.
        """
        if output.record.get("warm_start") is True:
            raise ValueError(
                "This output started warm from a session's history (record.warm_start "
                "is true), so it may not be the result of its input alone; a store "
                "keeps only calculations run from their input."
            )
        inp = output.input_data
        key = self.key(inp)
        identities = dict(sorted(_check_strings(identities).items()))
        tags = _check_tags(tags)
        meta = _json_object(meta or {}, "meta")
        run = _new_run_id()
        files = _run_files(key, run, output, identities, meta)
        with self._journal.op("put", key, run=run, tags=tags):
            self._write_run(key, run, files)
            for tag in tags:
                _put_once(self.backend, f"{_key_dir(key)}/tags/{tag}", b"")
            self._update_pointer(key)
        return run

    def runs(self, key: str) -> list[Run]:
        """Every complete run of a calculation, oldest first.

        Raises:
            ValueError: ``key`` is not a key.
        """
        return [_run(f) for f in self._read_runs(_check_key(key))]

    def blob(self, key: str, path: str, run: str | None = None) -> bytes:
        """Read one log or file of a run, without loading anything else.

        Args:
            key: The calculation's key.
            path: The blob's path in the output: ``"logs"``, ``"results.files.orca.gbw"``,
                ``"results.frames.3.logs"``, ``"input_data.files.input.xyz"``.
            run: The run; None for the preferred one.

        Returns:
            The bytes (text is UTF-8).

        Raises:
            KeyError: No such run or blob.
        """
        run = self._run_id(key, run)
        try:
            return self.backend.get(f"{_run_dir(key, run)}/blobs/{_blob_name(path)}")
        except FileNotFoundError:
            raise KeyError(f"Run {run} has no blob {path!r}.") from None

    def withdraw(self, key: str, run: str, reason: str) -> None:
        """Withdraw a run: it stops being preferred, and nothing is deleted.

        Withdrawing a withdrawn run keeps the first withdrawal.

        Raises:
            KeyError: No such run.
        """
        run = self._run_id(key, run)
        marker = _dumps({"by": _user(), "time": _now(), "reason": reason})
        with self._journal.op("withdraw", key, run=run):
            _put_once(self.backend, f"{_run_dir(key, run)}/withdrawn.json", marker)
            self._update_pointer(key)

    def carry(
        self,
        runs: Iterable[tuple[str, str]],
        identities: Identities,
        evidence: Mapping[str, Any],
    ) -> str:
        """Declare runs valid for other identities too, citing the evidence once.

        A carry asserts that each run's whole output, ``record`` included, is what the
        code of the new identities would produce for its input, within the
        calculation's accuracy. Carry after a release that changes no results, with
        evidence that it does not (a benchmark rerun); a release that changes the shape
        of ``record`` cannot be carried. Lookups under the new identities then reuse
        the runs. Nothing is copied: the carry is one file, ``carries/<id>.json``, and
        each run gets an empty marker naming it.

        Args:
            runs: ``(key, run)`` pairs: from `runs`, `iter`, or an index query.
            identities: The identities the runs are also valid for.
            evidence: JSON: what was compared, and how closely it agreed.

        Returns:
            The carry's id: the sha256 of its file.

        Raises:
            KeyError: A run that is not in the store; nothing is carried then.
            ValueError: No runs, or ``evidence`` is not JSON.
            TypeError: ``identities`` are not strings to strings.
        """
        identities = dict(sorted(_check_strings(identities).items()))
        by_key: dict[str, list[str]] = {}
        for key, run in runs:
            by_key.setdefault(key, []).append(self._run_id(key, run))
        if not by_key:
            raise ValueError("Name at least one (key, run) to carry.")
        document = _dumps(
            {
                "identities": identities,
                "evidence": _json_object(evidence, "evidence"),
                "by": _user(),
                "time": _now(),
            }
        )
        carry = _digest(document)
        _put_once(self.backend, f"carries/{carry}.json", document)
        for key, key_runs in by_key.items():
            with self._journal.op("carry", key, runs=key_runs, carry=carry):
                for run in dict.fromkeys(key_runs):
                    _put_once(
                        self.backend, f"{_run_dir(key, run)}/carries/{carry}", b""
                    )
                self._update_pointer(key)
        return carry

    def tag(self, key: str, *tags: str) -> None:
        """Add tags to a calculation; tags already there stay.

        Raises:
            KeyError: The store has no run of this calculation.
            ValueError: A tag is not 1-64 of ``a-z0-9._-`` without a dot at either
                end, or is a name Windows reserves.
        """
        checked = _check_tags(tags)
        if not self._has_runs(_check_key(key)):
            raise KeyError(f"No run of {key} in this store.")
        with self._journal.op("tag", key, tags=checked):
            for tag in checked:
                _put_once(self.backend, f"{_key_dir(key)}/tags/{tag}", b"")

    def untag(self, key: str, *tags: str) -> None:
        """Remove tags from a calculation; tags not there are ignored."""
        checked = _check_tags(tags)
        with self._journal.op("untag", _check_key(key), tags=checked):
            for tag in checked:
                self.backend.delete(f"{_key_dir(key)}/tags/{tag}")

    def iter(self) -> Iterator[str]:
        """Every key with at least one complete run, in key order."""
        for key in self._keys():
            if self._has_runs(key):
                yield key

    def copy_to(self, other: Store, keys: Iterable[str] | None = None) -> int:
        """Copy runs into another store, unchanged: ids, times and contents.

        Runs the other store has are skipped, but their withdrawals and carries are
        copied, as are the keys' tags, so the other store prefers the same runs.

        Args:
            other: The destination.
            keys: The calculations to copy; None for all.

        Returns:
            The number of runs copied.
        """
        copied = 0
        for key in self.iter() if keys is None else keys:
            runs = self._run_ids(_check_key(key))
            if not runs:
                continue
            tags = self.backend.list(f"{_key_dir(key)}/tags")
            with other._journal.op("copy", key, runs=runs, tags=tags):
                for run in runs:
                    copied += other._copy_run(self.backend, key, run)
                for tag in tags:
                    _put_once(other.backend, f"{_key_dir(key)}/tags/{tag}", b"")
                other._update_pointer(key)
        return copied

    def index(self, path: str | os.PathLike[str] | None = None) -> Index:
        """Open the store's SQLite index, brought up to date from the journals.

        Args:
            path: The index file; by default ``index.sqlite`` in a local store's
                directory. SQLite needs a local disk: for a store on NFS, pass a path on
                a local disk.

        Returns:
            The index.
        """
        from .index import Index

        if path is None:
            if not isinstance(self.backend, LocalBackend):
                raise ValueError("Pass the index's path: the store is not local.")
            path = self.backend.root / "index.sqlite"
        return Index(self, Path(path))

    # ------------------------------------------------------------------ reading

    def _keys(self, strays: list[str] | None = None) -> Iterator[str]:
        """Every key directory, complete runs or not, in key order.

        Args:
            strays: Collects the paths under ``records/`` that are not part of the
                layout (`verify` reports them).
        """
        strays = [] if strays is None else strays
        for version in self.backend.list("records"):
            if not re.fullmatch(r"k[0-9]+", version):
                strays.append(f"records/{version}")
                continue
            for shard in self.backend.list(f"records/{version}"):
                if not re.fullmatch(r"[0-9a-f]{2}", shard):
                    strays.append(f"records/{version}/{shard}")
                    continue
                for digest in self.backend.list(f"records/{version}/{shard}"):
                    if re.fullmatch(r"[0-9a-f]{64}", digest) and digest[:2] == shard:
                        yield f"{version}:{digest}"
                    else:
                        strays.append(f"records/{version}/{shard}/{digest}")

    def _run_ids(self, key: str) -> list[str]:
        """The ids of a calculation's complete runs, oldest first."""
        directory = _key_dir(key)
        return [
            name
            for name in self.backend.list(directory)
            if _RUN.fullmatch(name)
            and self.backend.exists(f"{directory}/{name}/record.json")
        ]

    def _has_runs(self, key: str) -> bool:
        return self.backend.exists(f"{_key_dir(key)}/current.json") or bool(
            self._run_ids(key)
        )

    def _read_runs(
        self, key: str, cache: dict[str, Any] | None = None
    ) -> list[_RunFiles]:
        """Read every complete run of a key with its markers.

        Args:
            key: The key.
            cache: Records and carries already read, by path; they never change.
        """
        cache = {} if cache is None else cache
        out = []
        for run in self._run_ids(key):
            directory = _run_dir(key, run)
            if directory not in cache:
                cache[directory] = _read_record(self.backend, directory)
            withdrawn = None
            if self.backend.exists(f"{directory}/withdrawn.json"):
                withdrawn = _read_json(self.backend, f"{directory}/withdrawn.json")
            carries = []
            for carry in self.backend.list(f"{directory}/carries"):
                if carry not in cache:
                    cache[carry] = _read_carry(self.backend, carry)
                carries.append(cache[carry])
            out.append(_RunFiles(run, cache[directory], withdrawn, carries))
        return out

    def _pointer_now(self, key: str) -> dict[str, Any] | None:
        """The pointer as stored, or None before a writer has written one."""
        try:
            return _read_json(self.backend, f"{_key_dir(key)}/current.json")
        except FileNotFoundError:
            return None

    def _preferred(self, key: str, identities: Identities | None) -> str | None:
        """The preferred run, from the pointer when it can be trusted.

        A writer killed between changing a run and replacing the pointer leaves the
        pointer behind. A run it names is complete either way, but may since have
        been withdrawn; then, as when there is no pointer yet, the runs decide.
        """
        wanted = None if identities is None else _check_strings(identities)
        pointer = self._pointer_now(key)
        run = None if pointer is None else _select(pointer, wanted)
        if pointer is None or (
            run is not None
            and self.backend.exists(f"{_run_dir(key, run)}/withdrawn.json")
        ):
            run = _select(_pointer(self._read_runs(key)), wanted)
        return run

    def _run_id(self, key: str, run: str | None) -> str:
        """A complete run's id: ``run`` checked, or the preferred run's."""
        if run is None:
            preferred = self._preferred(_check_key(key), None)
            if preferred is None:
                raise KeyError(f"No preferred run of {key} in this store.")
            return preferred
        if not (
            _RUN.fullmatch(run)
            and self.backend.exists(f"{_run_dir(_check_key(key), run)}/record.json")
        ):
            raise KeyError(f"No run {run!r} of {key} in this store.")
        return run

    def _load(self, key: str, run: str, blobs: bool) -> Output:
        """Rebuild a run's output from its record and blobs."""
        directory = _run_dir(key, run)
        record = _read_record(self.backend, directory)
        arrays = {}
        for name, entry in record["blobs"].items():
            if name.endswith(".npy"):
                arrays[name] = self.backend.get(f"{directory}/blobs/{name}")
            elif blobs:
                data = self.backend.get(f"{directory}/blobs/{name}")
                if _digest(data) != entry["sha256"]:
                    raise ValueError(
                        f"The blob {directory}/blobs/{name} does not match the sha256 "
                        "in its record: it changed after it was stored."
                    )
                value = (
                    "base64:" + base64.b64encode(data).decode("ascii")
                    if entry.get("binary")
                    else data.decode("utf-8")
                )
                _set(record, _blob_path(name), value)
        if not arrays:
            return ProgramOutput.model_validate(record["output"])
        # qcdata reads large arrays from .npy files beside the model's file.
        with tempfile.TemporaryDirectory(prefix="qcstore-") as tmp:
            for name, data in arrays.items():
                (Path(tmp) / name).write_bytes(data)
            (Path(tmp) / "output.json").write_text(json.dumps(record["output"]))
            return ProgramOutput.open(Path(tmp) / "output.json")

    # ------------------------------------------------------------------ writing

    def _write_run(self, key: str, run: str, files: Mapping[str, bytes]) -> None:
        """Write a run's files in order; the last, ``record.json``, commits it."""
        for name, data in files.items():
            self.backend.put(f"{_run_dir(key, run)}/{name}", data)

    def _copy_run(self, source: Backend, key: str, run: str) -> int:
        """Copy one run from another store's backend; return 1 if it was new."""
        directory = _run_dir(key, run)
        new = not self.backend.exists(f"{directory}/record.json")
        if new:
            record = source.get(f"{directory}/record.json")
            files = {
                f"blobs/{name}": source.get(f"{directory}/blobs/{name}")
                for name in json.loads(record)["blobs"]
            }
            self._write_run(key, run, {**files, "record.json": record})
        if source.exists(f"{directory}/withdrawn.json"):
            withdrawn = source.get(f"{directory}/withdrawn.json")
            _put_once(self.backend, f"{directory}/withdrawn.json", withdrawn)
        for carry in source.list(f"{directory}/carries"):
            document = source.get(f"carries/{carry}.json")
            _put_once(self.backend, f"carries/{carry}.json", document)
            _put_once(self.backend, f"{directory}/carries/{carry}", b"")
        return int(new)

    def _update_pointer(self, key: str) -> None:
        """Write ``current.json`` from the runs until what was written still holds.

        Writers race: one may compute the pointer from runs that another writer's
        newer run has since joined, and replace the newer pointer with its older one.
        Every writer therefore re-reads the runs after replacing the pointer and writes
        again until its pointer matches them. The last replacement then follows a
        reading of every run, so the pointer ends right.
        """
        cache: dict[str, Any] = {}
        pointer = _pointer(self._read_runs(key, cache))
        while True:
            self.backend.put(
                f"{_key_dir(key)}/current.json", _dumps(pointer), replace=True
            )
            latest = _pointer(self._read_runs(key, cache))
            if latest == pointer:
                return
            pointer = latest


# ---------------------------------------------------------------------- the journal


class _Journal:
    """A writer's journal: two lines per change, so the index misses none.

    Each change appends a line naming its key before it starts (``n``, ``op``, ``key``
    and the change's details) and ``{"done": n}`` after it ends. The index re-reads a
    key from the store when its change is done, and keeps re-reading the keys of
    changes that started but never ended, whose writer was killed mid-change.

    One writer per process (``<host>-<pid>-<random>``) and one batch file per store
    object; a forked child starts its own.
    """

    def __init__(self, backend: Backend) -> None:
        self.backend = backend
        self.lock = threading.Lock()
        self.pid = -1
        self.path = ""
        self.n = 0

    def _write(self, line: Mapping[str, Any]) -> int:
        with self.lock:
            if self.pid != os.getpid():
                self.pid = os.getpid()
                self.path = f"journals/{_writer()}/{_new_run_id()}.jsonl"
                self.n = 0
            if "done" not in line:
                self.n += 1
                line = {"n": self.n, **line}
            text = json.dumps(line, separators=(",", ":"), ensure_ascii=False)
            self.backend.append(self.path, (text + "\n").encode("utf-8"))
            return self.n

    @contextmanager
    def op(self, op: str, key: str, **details: Any) -> Iterator[None]:
        n = self._write({"op": op, "key": key, "time": _now(), **details})
        try:
            yield
        finally:
            self._write({"done": n})


_WRITER: tuple[int, str] | None = None


def _writer() -> str:
    """This process's writer id, unique per process."""
    global _WRITER
    if _WRITER is None or _WRITER[0] != os.getpid():
        host = re.sub(r"[^A-Za-z0-9.-]", "-", socket.gethostname()) or "host"
        _WRITER = (os.getpid(), f"{host}-{os.getpid()}-{secrets.token_hex(3)}")
    return _WRITER[1]


# ---------------------------------------------------------------------- records


def _run_files(
    key: str,
    run: str,
    output: Output,
    identities: dict[str, str],
    meta: dict[str, Any],
) -> dict[str, bytes]:
    """A run's files, ``record.json`` last: the output split into record and blobs."""
    with tempfile.TemporaryDirectory(prefix="qcstore-") as tmp:
        saved = output.save(Path(tmp) / "output.json")
        document = json.loads(saved[0].read_text())
        arrays = {path.name: path.read_bytes() for path in saved[1:]}
    record: dict[str, Any] = {
        "format": FORMAT,
        "key": key,
        "run": run,
        "identities": identities,
        "meta": meta,
        "blobs": {},
        "output": document,
    }
    files: dict[str, bytes] = {}
    for path, value in _blob_values(output, ("output",)):
        del _at(record, path[:-1])[path[-1]]
        name = ".".join(_escape(part) for part in path)
        data = value if isinstance(value, bytes) else value.encode("utf-8")
        entry: dict[str, Any] = {"sha256": _digest(data)}
        if isinstance(value, bytes):
            entry["binary"] = True
        record["blobs"][name] = entry
        files[f"blobs/{name}"] = data
    for name, data in arrays.items():
        record["blobs"][name] = {"sha256": _digest(data)}
        files[f"blobs/{name}"] = data
    files["record.json"] = _dumps(record)
    return files


def _blob_values(
    value: Any, path: tuple[str, ...]
) -> Iterator[tuple[tuple[str, ...], str | bytes]]:
    """Every log and file in a model, at any depth, with its path in the document."""
    if isinstance(value, OutputBase) and value.logs is not None:
        yield (*path, "logs"), value.logs
    if isinstance(value, Files):
        for name, data in value.files.items():
            yield (*path, "files", name), data
    if isinstance(value, BaseModel):
        for name in type(value).model_fields:
            child = getattr(value, name)
            if isinstance(child, BaseModel | list | tuple | dict):
                yield from _blob_values(child, (*path, name))
    elif isinstance(value, list | tuple):
        for i, item in enumerate(value):
            if isinstance(item, BaseModel | list | tuple | dict):
                yield from _blob_values(item, (*path, str(i)))
    elif isinstance(value, dict):
        for name, item in value.items():
            if isinstance(item, BaseModel | list | tuple | dict):
                yield from _blob_values(item, (*path, str(name)))


def _escape(part: str) -> str:
    """A path part safe in a file name: other characters, dots included, as %XX.

    The same rule as qcdata's array file names, so ``output.results.files.a%2Etxt`` and
    ``output.results.hessian.npy`` read alike.
    """
    return re.sub(
        r"[^A-Za-z0-9_-]",
        lambda m: "".join(f"%{b:02X}" for b in m.group().encode()),
        part,
    )


def _blob_name(path: str) -> str:
    """A blob's file name from its readable path in the output (``results.files.a.b``).

    Field names hold no dots, so a file's name is everything after its ``files`` part.
    """
    head, found, filename = f".{path}".partition(".files.")
    parts = ["output", *head[1:].split(".")] if head else ["output"]
    parts += ["files", filename] if found else []
    return ".".join(_escape(part) for part in parts)


def _blob_path(name: str) -> list[str]:
    """A blob's path in the record, from its file name."""
    return [unquote(part) for part in name.split(".")]


def _at(document: Any, path: Iterable[str]) -> Any:
    """The node at a path of a JSON document; list indices are decimal strings."""
    for part in path:
        document = document[int(part)] if isinstance(document, list) else document[part]
    return document


def _set(document: Any, path: list[str], value: Any) -> None:
    _at(document, path[:-1])[path[-1]] = value


def _dumps(value: Any) -> bytes:
    """JSON for people: indented, with short objects and lists on one line.

    A list of numbers or strings is always one line; any other object or list is one
    line when it fits in 88 characters.
    """

    def write(value: Any, indent: str, lead: int = 0) -> str:
        if not isinstance(value, dict | list) or not value:
            return json.dumps(value, ensure_ascii=False, allow_nan=False)
        inner = indent + "  "
        if isinstance(value, dict):
            keys = [json.dumps(k, ensure_ascii=False) + ": " for k in value]
            items = [
                k + write(v, inner, len(k))
                for k, v in zip(keys, value.values(), strict=True)
            ]
        else:
            items = [write(v, inner) for v in value]
        brackets = "{}" if isinstance(value, dict) else "[]"
        line = brackets[0] + ", ".join(items) + brackets[1]
        scalars = isinstance(value, list) and not any(
            isinstance(v, dict | list) for v in value
        )
        if scalars or ("\n" not in line and len(indent) + lead + len(line) <= 88):
            return line
        body = ",\n".join(inner + item for item in items)
        return f"{brackets[0]}\n{body}\n{indent}{brackets[1]}"

    return (write(value, "") + "\n").encode("utf-8")


def _read_json(backend: Backend, path: str) -> Any:
    return json.loads(backend.get(path))


def _read_carry(backend: Backend, carry: str) -> dict[str, Any]:
    """A carry's file, checked against its name, the sha256 of its bytes."""
    data = backend.get(f"carries/{carry}.json")
    if _digest(data) != carry:
        raise ValueError(f"carries/{carry}.json does not match its sha256.")
    return {"id": carry, **json.loads(data)}


def _read_record(backend: Backend, directory: str) -> dict[str, Any]:
    """A run's ``record.json``, refused if another qcstore format wrote it."""
    record: dict[str, Any] = _read_json(backend, f"{directory}/record.json")
    if record.get("format") != FORMAT:
        raise ValueError(
            f"{directory}/record.json has record format {record.get('format')!r}; "
            f"this qcstore reads format {FORMAT}."
        )
    return record


def _put_once(backend: Backend, path: str, data: bytes) -> None:
    """Create a file that is never replaced; if it exists, the first one stands."""
    try:
        backend.put(path, data)
    except FileExistsError:
        pass


# ---------------------------------------------------------------------- preference


def _preferred(runs: Iterable[_RunFiles]) -> str | None:
    """The latest success not withdrawn, else the latest run not withdrawn."""
    live = [r for r in runs if r.withdrawn is None]
    successes = [r for r in live if r.record["output"]["success"]]
    candidates = successes or live
    return max(r.id for r in candidates) if candidates else None


def _select(pointer: dict[str, Any], identities: dict[str, str] | None) -> str | None:
    """The run a pointer prefers for some identities, or for any."""
    if identities is None:
        return pointer["preferred"]  # type: ignore[no-any-return]
    for entry in pointer["by_identities"]:
        if entry["identities"] == identities:
            return entry["preferred"]  # type: ignore[no-any-return]
    return None


def _pointer(runs: list[_RunFiles]) -> dict[str, Any]:
    """``current.json``: the preferred run, overall and for each identity set.

    An identity set gets an entry when a run was produced by it or carried to it, and
    a run of it is not withdrawn. Entries are in a fixed order, so two writers reading
    the same runs write the same bytes.
    """
    sets = {_canonical(ids): ids for r in runs for ids in r.valid_for}
    by_identities = []
    for text in sorted(sets):
        run = _preferred(r for r in runs if sets[text] in r.valid_for)
        if run is not None:
            by_identities.append({"identities": sets[text], "preferred": run})
    return {"preferred": _preferred(runs), "by_identities": by_identities}


def _run(files: _RunFiles) -> Run:
    """A run as `Store.runs` shows it."""
    output = files.record["output"]
    execution = output.get("execution") or {}
    withdrawn = files.withdrawn
    return Run(
        id=files.id,
        time=_run_time(files.id),
        success=output["success"],
        error_kind=(output.get("error") or {}).get("kind"),
        identities=files.record["identities"],
        carries=[
            Carry(c["id"], c["identities"], c["evidence"], c["by"], _time(c["time"]))
            for c in files.carries
        ],
        withdrawn=None
        if withdrawn is None
        else Withdrawal(withdrawn["by"], _time(withdrawn["time"]), withdrawn["reason"]),
        meta=files.record["meta"],
        program_version=output["results"]["provenance"].get("program_version"),
        host=execution.get("hostname"),
        cpu_model=execution.get("cpu_model"),
        devices=execution.get("devices", []),
        libc=execution.get("libc"),
    )


# ---------------------------------------------------------------------- small helpers


def _key_dir(key: str) -> str:
    """``records/k1/3f/3f9a...``: the key without its colon, which Windows forbids."""
    version, _, digest = key.partition(":")
    return f"records/{version}/{digest[:2]}/{digest}"


def _run_dir(key: str, run: str) -> str:
    return f"{_key_dir(key)}/{run}"


def _check_key(key: str) -> str:
    if not isinstance(key, str) or not _KEY.fullmatch(key):
        raise ValueError(f"Not a calculation key: {key!r}.")
    return key


def _check_tags(tags: Iterable[str]) -> list[str]:
    if isinstance(tags, str):
        raise TypeError("Pass tags as a list of strings, not one string.")
    checked = list(dict.fromkeys(tags))
    for tag in checked:
        if (
            not isinstance(tag, str)
            or not _TAG.fullmatch(tag)
            or tag.split(".")[0] in _RESERVED
        ):
            raise ValueError(
                "A tag is 1-64 of a-z, 0-9, '.', '_' and '-', neither starting nor "
                "ending with '.', and not a name Windows reserves (con, nul, com1, "
                f"...); got {tag!r}."
            )
    return checked


def _check_strings(identities: Identities) -> dict[str, str]:
    if not isinstance(identities, Mapping) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in identities.items()
    ):
        raise TypeError(
            f"Identities map each program to its numerics, strings to strings; got "
            f"{identities!r}."
        )
    return dict(identities)


def _json_object(value: Mapping[str, Any], what: str) -> dict[str, Any]:
    try:
        text = json.dumps(dict(value), allow_nan=False)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{what} must be JSON with string keys: {e}") from None
    return json.loads(text)  # type: ignore[no-any-return]


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def _new_run_id() -> str:
    """A UTC timestamp to the microsecond and a random suffix; sorts by time."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"{stamp}-{secrets.token_hex(4)}"


def _run_time(run: str) -> datetime:
    return datetime.strptime(run[:23], "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=UTC)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # no user name in some containers
        return "unknown"
