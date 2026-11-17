"""Many writer processes at once, and writers killed mid-write (SIGKILL)."""

import json
import multiprocessing
import random
import time
from pathlib import Path

import pytest

from qcstore import Store, verify
from qcstore.store import _key_dir, _pointer
from tests import outputs, writers
from tests.test_index import contents, rebuilt

spawn = multiprocessing.get_context("spawn")


def pointers_are_exact(store: Store) -> None:
    """Every current.json says what the runs say, and names only complete runs."""
    for key in store.iter():
        stored = json.loads(store.backend.get(f"{_key_dir(key)}/current.json"))
        assert stored == _pointer(store._read_runs(key)), key
        complete = {r.id for r in store.runs(key)}
        named = {stored["preferred"]} | {
            e["preferred"] for e in stored["by_identities"]
        }
        assert named - {None} <= complete


def test_many_writers_on_the_same_and_different_keys(tmp_path):
    root = str(tmp_path / "store")
    store = Store(root)
    index = store.index(tmp_path / "live.sqlite")
    reports = [tmp_path / f"w{w}.json" for w in range(8)]
    # Writers 0-5 share three keys; 6 and 7 have keys of their own.
    procs = [
        spawn.Process(
            target=writers.mixed,
            args=(root, w, 3 if w < 6 else 100 + w, 40, str(reports[w])),
        )
        for w in range(8)
    ]
    for p in procs:
        p.start()
    while any(p.is_alive() for p in procs):
        index.update()  # the index reads journals while they are written
        time.sleep(0.05)
    assert all(p.exitcode == 0 for p in procs)

    put = {run for r in reports for run in json.loads(r.read_text())}
    stored = {run.id for key in store.iter() for run in store.runs(key)}
    assert stored == put and len(put) > 150
    pointers_are_exact(store)
    report = verify(store)
    assert report.problems == [] and report.incomplete == []
    index.update()
    assert contents(index) == rebuilt(store, tmp_path)
    assert index.sql("SELECT * FROM pending") == []


@pytest.mark.parametrize("point", ["record.json", "current.json", "done"])
def test_a_writer_killed_at_each_step(tmp_path, point):
    root = str(tmp_path / "store")
    store = Store(root)
    first = store.put(outputs.energy(), {"xtb": "6.7.1"})
    index = store.index()
    key = store.key(outputs.energy_input())

    child = spawn.Process(target=writers.die_while_putting, args=(root, point, 0))
    child.start()
    child.join()
    assert child.exitcode == -9  # killed

    report = verify(store)
    runs = store.runs(key)
    if point == "record.json":  # killed before its commit: the run does not exist
        assert [r.id for r in runs] == [first] and len(report.incomplete) == 1
        assert report.problems == []
    elif point == "current.json":  # committed, but the pointer not yet updated
        assert len(runs) == 2 and report.incomplete == []
        assert store.get(key) == outputs.energy()
        assert store.get(key, {"xtb": "6.8.0"}) is None  # stale until repaired
        assert len(report.problems) == 1 and "stale" in report.problems[0]
        assert verify(store, repair=True).repaired
        assert store.get(key, {"xtb": "6.8.0"}) == outputs.energy()
    else:  # all done but the journal's done line
        assert len(runs) == 2 and report.problems == []
    assert verify(store).problems == []
    pointers_are_exact(store)
    index.update()
    assert index.sql("SELECT key FROM pending") == [{"key": key}]
    assert contents(index) == rebuilt(store, tmp_path)


def test_a_writer_killed_on_a_new_key_leaves_no_pointer_to_trust(tmp_path):
    root = str(tmp_path / "store")
    store = Store(root)
    child = spawn.Process(
        target=writers.die_while_putting, args=(root, "current.json", 5)
    )
    child.start()
    child.join()
    key = store.key(outputs.energy_input(5))
    # No current.json yet: readers work it out from the runs.
    assert store.get(key, {"xtb": "6.8.0"}) == outputs.energy(5)
    assert list(store.iter()) == [key]
    assert "missing" in verify(store).problems[0]


def test_a_withdrawal_holds_even_if_its_writer_dies_before_the_pointer(tmp_path):
    root = str(tmp_path / "store")
    store = Store(root)
    key = store.key(outputs.energy_input())
    older = store.put(outputs.energy(), {"xtb": "6.7.1"})
    newer = store.put(outputs.energy(), {"xtb": "6.7.1"})
    child = spawn.Process(target=writers.die_while_withdrawing, args=(root, key, newer))
    child.start()
    child.join()
    assert child.exitcode == -9
    pointer = json.loads(store.backend.get(f"{_key_dir(key)}/current.json"))
    assert pointer["preferred"] == newer  # behind: it still names the withdrawn run
    assert [r.id for r in store.runs(key) if r.withdrawn] == [newer]
    for identities in (None, {"xtb": "6.7.1"}):
        assert store.get(key, identities) is not None
        assert store._preferred(key, identities) == older
    verify(store, repair=True)
    pointers_are_exact(store)


def test_writers_killed_at_random(tmp_path):
    root = str(tmp_path / "store")
    store = Store(root)
    index = store.index(tmp_path / "live.sqlite")
    rng = random.Random(7)
    procs = [spawn.Process(target=writers.forever, args=(root, w, 4)) for w in range(4)]
    for p in procs:
        p.start()
    time.sleep(2.0)  # let them import and start writing
    kills = 0
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline:
        time.sleep(rng.uniform(0.0, 0.3))
        w = rng.randrange(len(procs))
        procs[w].kill()
        procs[w].join()
        kills += 1
        procs[w] = spawn.Process(target=writers.forever, args=(root, w, 4))
        procs[w].start()
        index.update()
    for p in procs:
        p.kill()
        p.join()
    assert kills > 10

    keys = list(store.iter())
    assert keys and sum(len(store.runs(k)) for k in keys) > 20
    report = verify(store)
    # A kill may leave an unfinished run (ignored) or a stale pointer (repairable);
    # nothing else.
    assert all("current.json" in p for p in report.problems), report.problems
    for key in keys:  # every pointer names complete runs only
        stored = json.loads(store.backend.get(f"{_key_dir(key)}/current.json"))
        complete = {r.id for r in store.runs(key)}
        assert stored["preferred"] in complete | {None}
        assert {e["preferred"] for e in stored["by_identities"]} <= complete
        assert store.get(key) is not None or all(r.withdrawn for r in store.runs(key))
    verify(store, repair=True)
    assert verify(store).problems == []
    pointers_are_exact(store)
    index.update()
    assert contents(index) == rebuilt(store, tmp_path)


def test_index_updates_from_several_processes(tmp_path):
    root = str(tmp_path / "store")
    store = Store(root)
    for i in range(3):
        store.put(outputs.energy(i), {"xtb": "6.7.1"})
    path = tmp_path / "shared.sqlite"
    store.index(path)
    procs = [
        spawn.Process(target=_update_index, args=(root, str(path))) for _ in range(4)
    ]
    writer = spawn.Process(
        target=writers.mixed, args=(root, 0, 3, 30, str(tmp_path / "w.json"))
    )
    for p in [*procs, writer]:
        p.start()
    for p in [*procs, writer]:
        p.join()
        assert p.exitcode == 0
    index = store.index(path)
    assert contents(index) == rebuilt(store, tmp_path)


def _update_index(root: str, path: str) -> None:
    store = Store(root)
    for _ in range(20):
        store.index(Path(path))
