import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from qcstore import Index, Store
from tests import outputs
from tests.outputs import XTB

TABLES = {
    "runs": "key, run",
    "carries": "key, run, identities",
    "tags": "key, tag",
    "meta": "key, run, name",
}


def contents(index: Index) -> dict[str, list[dict[str, Any]]]:
    """Everything the index says about the store."""
    return {t: index.sql(f"SELECT * FROM {t} ORDER BY {o}") for t, o in TABLES.items()}


def rebuilt(store: Store, tmp_path: Path) -> dict[str, list[dict[str, Any]]]:
    """What a fresh index of the store says."""
    return contents(
        store.index(tmp_path / f"fresh-{len(list(tmp_path.iterdir()))}.sqlite")
    )


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "store")


def fill(store: Store) -> dict[str, str]:
    """A store with successes, failures, withdrawals, carries, tags and meta."""
    runs = {}
    runs["ok"] = store.put(outputs.energy(), XTB, tags=["screen"], meta={"batch": 1})
    runs["failed"] = store.put(outputs.energy(1, success=False), {"xtb": "6.8.0"})
    runs["opt"] = store.put(
        outputs.optimization(),
        {"qcopt": "0.3.0", "xtb": "6.7.1"},
        tags=["screen", "opt"],
        meta={"note": "first", "flags": [1, 2], "ok": True},
    )
    runs["files"] = store.put(outputs.files_output(), {"orca": "6.0.1"})
    key = store.key(outputs.energy_input())
    runs["second"] = store.put(outputs.energy(), XTB)
    store.withdraw(key, runs["second"], "duplicate")
    store.carry(key, runs["ok"], {"xtb": "6.8.0"}, {"max_abs_difference": 0.0})
    store.untag(store.key(outputs.optimization().input_data), "opt")
    return runs


def test_query(store, tmp_path):
    runs = fill(store)
    index = store.index()
    assert index.path == store.backend.root / "index.sqlite"  # type: ignore[attr-defined]
    rows = index.query()
    assert len(rows) == 5
    [ok] = index.query(run=runs["ok"])
    assert (
        ok["success"] is True and ok["preferred"] is True and ok["withdrawn"] is False
    )
    assert ok["identities"] == XTB and ok["input"]["keywords"] == {"accuracy": 0.1}
    assert (ok["program"], ok["calctype"], ok["method"], ok["basis"]) == (
        "xtb",
        "energy",
        "gfn2",
        None,
    )
    assert (ok["formula"], ok["version"], ok["host"], ok["wall_time"]) == (
        "H2O",
        "6.7.1",
        "node1",
        0.25,
    )
    assert ok["time"].endswith("Z") and ok["time"][:4] == runs["ok"][:4]
    [second] = index.query(run=runs["second"])
    assert second["withdrawn"] is True and second["preferred"] is False
    assert [r["run"] for r in index.query(success=False)] == [runs["failed"]]
    assert [r["error_kind"] for r in index.query(success=False)] == ["convergence"]
    assert {r["run"] for r in index.query(tag="screen")} == {
        runs["ok"],
        runs["second"],
        runs["opt"],
    }
    assert index.query(tag="opt") == []
    assert [r["run"] for r in index.query(program="orca")] == [runs["files"]]
    assert index.query(basis=None, program="orca")[0]["formula"] is None
    with pytest.raises(ValueError, match="No column"):
        index.query(colour="red")


def test_query_by_identities_includes_carried_runs(store):
    runs = fill(store)
    index = store.index()
    under_new = {r["run"] for r in index.query(identities={"xtb": "6.8.0"})}
    assert under_new == {runs["failed"], runs["ok"]}  # its own run, and a carried one
    under_old = {r["run"] for r in index.query(identities={"xtb": "6.7.1"})}
    assert under_old == {runs["ok"], runs["second"], runs["opt"]}
    both = index.query(identities={"xtb": "6.7.1", "qcopt": "0.3.0"})
    assert [r["run"] for r in both] == [runs["opt"]]
    assert index.query(identities={"xtb": "6.7.1"}, preferred=True, calctype="energy")
    with pytest.raises(ValueError, match="Not a program name"):
        index.query(identities={'x"': "1"})


def test_meta_and_tags_tables(store):
    runs = fill(store)
    index = store.index()
    meta = index.sql(
        "SELECT name, value FROM meta WHERE run = ? ORDER BY name", [runs["opt"]]
    )
    assert meta == [
        {"name": "flags", "value": "[1, 2]"},
        {"name": "note", "value": "first"},
        {"name": "ok", "value": 1},
    ]
    assert index.sql("SELECT run FROM meta WHERE name = 'batch' AND value = 1") == [
        {"run": runs["ok"]}
    ]
    carried = index.sql("SELECT identities, evidence FROM carries")
    assert carried == [
        {"identities": '{"xtb":"6.8.0"}', "evidence": '{"max_abs_difference": 0.0}'}
    ]


def test_calculations_per_molecule(store):
    for i in range(3):
        store.put(outputs.energy(i), XTB)
    store.put(outputs.energy(0), XTB)
    store.put(outputs.hessian(), XTB)
    rows = store.index().sql(
        "SELECT formula, COUNT(DISTINCT key) AS calculations, COUNT(*) AS runs "
        "FROM runs GROUP BY formula ORDER BY formula"
    )
    assert rows == [
        {"formula": "C130", "calculations": 1, "runs": 1},
        {"formula": "H2O", "calculations": 3, "runs": 4},
    ]


def test_sql_is_read_only(store):
    fill(store)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        store.index().sql("DELETE FROM runs")


def test_update_reads_only_new_lines_and_matches_a_rebuild(store, tmp_path):
    index = store.index()
    assert contents(index)["runs"] == []
    runs = fill(store)
    index.update()
    assert contents(index) == rebuilt(store, tmp_path)
    key = store.key(outputs.energy_input())
    store.withdraw(key, runs["ok"], "late doubts")
    store.tag(key, "late")
    store.put(outputs.energy(2), XTB)
    other = Store(tmp_path / "other")
    other.put(outputs.energy(3), XTB)
    other.copy_to(store)
    index.update()
    assert contents(index) == rebuilt(store, tmp_path)
    assert index.query(run=runs["ok"])[0]["withdrawn"] is True
    offsets = index.sql("SELECT * FROM journals")
    index.update()
    assert index.sql("SELECT * FROM journals") == offsets
    assert index.sql("SELECT * FROM pending") == []
    index.rebuild()
    assert contents(index) == rebuilt(store, tmp_path)


def test_a_change_whose_writer_died_is_reread_on_every_update(store, tmp_path):
    """A begin line with no done line: the key stays pending, re-read each update."""
    store.put(outputs.energy(), XTB)
    index = store.index()
    key = store.key(outputs.energy_input())
    tags = store.backend.root / "records" / key[3:5] / key / "tags"  # type: ignore[attr-defined]
    store.backend.append(
        "journals/dead-1/batch.jsonl",
        (json.dumps({"n": 1, "op": "tag", "key": key}) + "\n").encode(),
    )
    index.update()
    tags.mkdir()
    (tags / "slow").touch()  # the change lands after the index read the line
    index.update()
    assert index.sql("SELECT tag FROM tags") == [{"tag": "slow"}]
    (tags / "slow").unlink()
    index.update()
    assert index.sql("SELECT tag FROM tags") == []
    assert index.sql("SELECT key FROM pending") == [{"key": key}]
    index.rebuild()
    assert index.sql("SELECT key FROM pending") == [{"key": key}]
    assert contents(index) == rebuilt(store, tmp_path)


def test_a_line_being_written_waits(store):
    store.put(outputs.energy(), XTB)
    index = store.index()
    key = store.key(outputs.energy_input(1))
    store.backend.append(
        "journals/w/b.jsonl", f'{{"n": 1, "op": "put", "key": "{key}"'.encode()
    )
    index.update()
    assert index.sql(
        "SELECT offset FROM journals WHERE path = 'journals/w/b.jsonl'"
    ) == [{"offset": 0}]
    store.backend.append("journals/w/b.jsonl", b'}\n{"done": 1}\n')
    index.update()
    assert index.sql("SELECT * FROM pending") == []


def test_a_malformed_line_is_skipped(store):
    store.put(outputs.energy(), XTB)
    store.index()
    store.backend.append("journals/w/b.jsonl", b"not json\n")
    with pytest.warns(UserWarning, match="malformed"):
        store.index()


def test_an_index_of_another_schema_is_rebuilt(store, tmp_path):
    fill(store)
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE runs (whatever)")
    index = store.index(path)
    assert len(index.query()) == 5
    assert repr(index) == f"Index({str(path)!r})"
