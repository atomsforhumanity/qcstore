import hashlib
import json
from pathlib import Path

import pytest
from qcdata import calculation_key

from qcstore import Store
from qcstore.store import _key_dir
from tests import outputs
from tests.outputs import XTB


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "store")


def key_dir(store: Store, key: str) -> Path:
    return store.backend.root / _key_dir(key)  # type: ignore[attr-defined]


# ---------------------------------------------------------------- put and get


@pytest.mark.parametrize(
    "make",
    [outputs.energy, outputs.optimization, outputs.hessian, outputs.files_output],
)
def test_get_returns_what_was_put(store, make):
    output = make()
    identities = {"files": "1"} if make is outputs.files_output else {"any": "1"}
    store.put(output, identities)
    key = store.key(output.input_data)
    assert store.get(key) == output
    assert store.get(key, identities) == output


def test_the_key_is_qcdatas(store):
    inp = outputs.energy_input()
    assert store.key(inp) == calculation_key(inp)


def test_get_without_blobs_leaves_logs_and_files_out(store):
    output = outputs.optimization()
    store.put(output, {"qcopt": "0.3.0", "xtb": "6.7.1"})
    got = store.get(store.key(output.input_data), blobs=False)
    assert got.logs is None and got.results.files == {} and got.input_data.files == {}
    assert all(frame.logs is None for frame in got.results.frames)
    assert got.input_data.subprograms[0].files == {}
    assert got.results.energies.tolist() == output.results.energies.tolist()


def test_get_returns_none_for_an_unknown_key(store):
    assert store.get(store.key(outputs.energy_input())) is None


def test_keys_are_checked(store):
    for bad in ["k1:abc", "../../etc", "k1:" + "G" * 64]:
        with pytest.raises(ValueError, match="Not a calculation key"):
            store.get(bad)


def test_put_returns_the_run_id_and_runs_lists_it(store):
    output = outputs.energy()
    run = store.put(output, XTB, meta={"campaign": "demo", "n": 3})
    [listed] = store.runs(store.key(output.input_data))
    assert listed.id == run and listed.success and listed.error_kind is None
    assert listed.identities == XTB and listed.meta == {"campaign": "demo", "n": 3}
    assert listed.program_version == "6.7.1" and listed.host == "node1"
    assert (listed.cpu_model, listed.devices, listed.libc) == (
        "AMD EPYC 9654",
        ["NVIDIA H100"],
        "glibc 2.39",
    )
    assert listed.withdrawn is None and listed.carries == []
    assert listed.time.tzinfo is not None and listed.time.strftime("%Y%m%d") == run[:8]


def test_put_refuses_a_warm_started_frame(store):
    with pytest.raises(ValueError, match="warm_start"):
        store.put(outputs.energy(record={"warm_start": True}), XTB)
    store.put(outputs.energy(record={"warm_start": False}), XTB)


def test_identities_are_strings_to_strings(store):
    with pytest.raises(TypeError, match="strings to strings"):
        store.put(outputs.energy(), {"xtb": 8})  # type: ignore[dict-item]
    with pytest.raises(TypeError, match="strings to strings"):
        store.get(store.key(outputs.energy_input()), ["xtb"])  # type: ignore[arg-type]
    # The runner's identities, whatever programs they name, are recorded as given.
    run = store.put(outputs.energy(), {"xtb": "6.7.1", "xtb-params": "gfn2-2024"})
    key = store.key(outputs.energy_input())
    assert store.runs(key)[0].identities == {"xtb": "6.7.1", "xtb-params": "gfn2-2024"}
    assert store.runs(key)[0].id == run
    assert store.get(key, {"xtb": "6.7.1"}) is None
    store.put(outputs.energy(), {})
    assert store.get(key, {}).success


def test_meta_must_be_json(store):
    with pytest.raises(ValueError, match="meta must be JSON"):
        store.put(outputs.energy(), XTB, meta={"x": object()})
    with pytest.raises(ValueError, match="meta must be JSON"):
        store.put(outputs.energy(), XTB, meta={"x": float("nan")})


# ---------------------------------------------------------------- preference


def test_the_latest_success_is_preferred_over_a_later_failure(store):
    ok = store.put(outputs.energy(), XTB)
    store.put(outputs.energy(success=False), XTB)
    key = store.key(outputs.energy_input())
    assert store.get(key).success
    assert [r.id for r in store.runs(key)][0] == ok


def test_a_failure_is_returned_when_nothing_succeeded(store):
    store.put(outputs.energy(success=False), XTB)
    got = store.get(store.key(outputs.energy_input()))
    assert not got.success and got.error.kind == "convergence"
    assert got.logs == "iteration 1\niteration 2\n"


def test_identities_select_runs(store):
    key = store.key(outputs.energy_input())
    old = store.put(outputs.energy(), {"xtb": "6.7.1"})
    store.put(outputs.energy(success=False), {"xtb": "6.8.0"})
    assert store.get(key, {"xtb": "6.7.1"}).success
    assert not store.get(key, {"xtb": "6.8.0"}).success
    assert store.get(key, {"xtb": "6.9.0"}) is None
    assert store.get(key).success  # any identities: the latest success
    pointer = json.loads((key_dir(store, key) / "current.json").read_text())
    assert pointer["preferred"] == old
    assert [e["identities"] for e in pointer["by_identities"]] == [
        {"xtb": "6.7.1"},
        {"xtb": "6.8.0"},
    ]


def test_withdrawn_runs_are_never_preferred(store):
    key = store.key(outputs.energy_input())
    first = store.put(outputs.energy(), XTB)
    second = store.put(outputs.energy(), XTB)
    store.withdraw(key, second, "wrong charge")
    assert [r.id for r in store.runs(key)] == [first, second]
    assert store.runs(key)[1].withdrawn.reason == "wrong charge"
    assert store.get(key) == outputs.energy()
    store.withdraw(key, first, "also wrong")
    assert store.get(key) is None and store.get(key, XTB) is None
    # A second withdrawal keeps the first reason; nothing was deleted.
    store.withdraw(key, second, "again")
    assert store.runs(key)[1].withdrawn.reason == "wrong charge"
    assert len(store.runs(key)) == 2


def test_withdraw_needs_a_complete_run(store):
    store.put(outputs.energy(), XTB)
    key = store.key(outputs.energy_input())
    with pytest.raises(KeyError):
        store.withdraw(key, "20260101T000000.000000Z-00000000", "no such run")


def test_carry_makes_a_run_valid_for_new_identities(store):
    key = store.key(outputs.energy_input())
    run = store.put(outputs.energy(), {"xtb": "6.7.1"})
    assert store.get(key, {"xtb": "6.8.0"}) is None
    evidence = {"compared": "energy", "max_abs_difference": 0.0}
    carry = store.carry([(key, run)], {"xtb": "6.8.0"}, evidence)
    assert store.get(key, {"xtb": "6.8.0"}) == outputs.energy()
    [listed] = store.runs(key)
    assert listed.carries[0].id == carry
    assert listed.carries[0].identities == {"xtb": "6.8.0"}
    assert listed.carries[0].evidence == evidence
    # Another carry adds its own marker; a run of the new code is then preferred.
    store.carry([(key, run)], {"xtb": "6.8.0"}, {"other": "evidence"})
    carries = store.runs(key)[0].carries
    assert sorted(json.dumps(c.evidence) for c in carries) == sorted(
        json.dumps(e) for e in (evidence, {"other": "evidence"})
    )
    newer = store.put(outputs.energy(), {"xtb": "6.8.0"})
    pointer = json.loads((key_dir(store, key) / "current.json").read_text())
    assert {"identities": {"xtb": "6.8.0"}, "preferred": newer} in pointer[
        "by_identities"
    ]


def test_a_bulk_carry_writes_its_evidence_once(store):
    root = store.backend.root  # type: ignore[attr-defined]
    runs = [store.put(outputs.energy(i), XTB) for i in range(20)]
    runs += [store.put(outputs.energy(i, success=False), XTB) for i in range(5)]
    pairs = [(key, run.id) for key in store.iter() for run in store.runs(key)]
    assert sorted(r for _, r in pairs) == sorted(runs)
    new = {"xtb": "6.8.0"}
    carry = store.carry(pairs, new, {"benchmark": "S22", "max_abs_difference": 1e-9})
    assert [p.name for p in (root / "carries").iterdir()] == [f"{carry}.json"]
    document = (root / "carries" / f"{carry}.json").read_bytes()
    assert hashlib.sha256(document).hexdigest() == carry
    assert json.loads(document)["identities"] == new
    for key, run in pairs:
        assert (key_dir(store, key) / run / "carries" / carry).read_bytes() == b""
    for i in range(20):
        key = store.key(outputs.energy_input(i))
        assert store.get(key, new) == store.get(key, XTB) == outputs.energy(i)


def test_carry_is_checked(store):
    key = store.key(outputs.energy_input())
    run = store.put(outputs.energy(), XTB)
    with pytest.raises(ValueError, match="at least one"):
        store.carry([], {"xtb": "6.8.0"}, {})
    with pytest.raises(ValueError, match="evidence must be JSON"):
        store.carry([(key, run)], {"xtb": "6.8.0"}, {"x": {1, 2}})
    with pytest.raises(TypeError, match="strings to strings"):
        store.carry([(key, run)], {"xtb": 6}, {})  # type: ignore[dict-item]
    missing = (key, "20260101T000000.000000Z-00000000")
    with pytest.raises(KeyError):
        store.carry([(key, run), missing], {"xtb": "6.8.0"}, {})
    assert not (store.backend.root / "carries").exists()  # type: ignore[attr-defined]
    assert store.runs(key)[0].carries == []


# ---------------------------------------------------------------- blobs


def test_blob_reads_one_log_or_file(store):
    output = outputs.optimization()
    run = store.put(output, {"qcopt": "0.3.0", "xtb": "6.7.1"})
    key = store.key(output.input_data)
    assert store.blob(key, "logs") == b"converged in 3 steps\n"
    assert store.blob(key, "results.frames.2.logs", run) == b"gradient 2\n"
    assert store.blob(key, "results.files.trajectory.xyz").startswith(b"3\n")
    assert store.blob(key, "results.files.binary/state.npz") == b"PK\x03"
    assert (
        store.blob(key, "input_data.subprograms.0.files.guess.bin")
        == b"\x00\x01\xfe\xff"
    )
    assert store.blob(key, "results.frames.1.results.files.step.log") == b"step 1"
    with pytest.raises(KeyError):
        store.blob(key, "results.files.missing")
    with pytest.raises(KeyError):
        store.blob(key, "logs", "20260101T000000.000000Z-00000000")


def test_blobs_are_checked_on_load(store):
    output = outputs.energy()
    run = store.put(output, XTB)
    key = store.key(output.input_data)
    (key_dir(store, key) / run / "blobs" / "output.logs").write_text("tampered")
    with pytest.raises(ValueError, match="sha256"):
        store.get(key)
    assert store.get(key, blobs=False).results.energy == output.results.energy


# ---------------------------------------------------------------- layout


def test_the_layout_is_plain_json(store):
    output = outputs.hessian()
    run = store.put(output, XTB, tags=["screen"])
    key = store.key(output.input_data)
    directory = key_dir(store, key)
    # No colon in any name: the key's version and hash are directories.
    assert directory == store.backend.root / "records" / "k1" / key[3:5] / key[3:]
    assert not any(":" in p.name for p in store.backend.root.rglob("*"))
    assert sorted(p.name for p in directory.iterdir()) == sorted(
        ["current.json", run, "tags"]
    )
    assert sorted(p.name for p in (directory / "tags").iterdir()) == ["screen"]
    assert sorted(p.name for p in (directory / run / "blobs").iterdir()) == [
        "output.input_data.given.hessian.npy",
        "output.logs",
        "output.results.hessian.npy",
    ]
    record = json.loads((directory / run / "record.json").read_text())
    assert list(record) == [
        "format",
        "key",
        "run",
        "identities",
        "meta",
        "blobs",
        "output",
    ]
    assert (record["format"], record["key"], record["run"]) == (1, key, run)
    assert record["output"]["results"]["energy"] == -100.0
    assert "logs" not in record["output"]
    assert (
        record["output"]["results"]["hessian"]["path"] == "output.results.hessian.npy"
    )
    assert json.loads((directory / "current.json").read_text()) == {
        "preferred": run,
        "by_identities": [{"identities": XTB, "preferred": run}],
    }


def test_records_of_another_format_are_refused(store):
    output = outputs.energy()
    run = store.put(output, XTB)
    key = store.key(output.input_data)
    path = key_dir(store, key) / run / "record.json"
    path.write_text(path.read_text().replace('"format": 1', '"format": 2'))
    with pytest.raises(ValueError, match="record format 2"):
        store.get(key)


def test_a_run_without_its_record_is_ignored(store):
    """What a writer killed before its commit leaves: blobs, and no record.json."""
    unfinished = "29991231T000000.000000Z-00000000"  # newer than any real run
    new = store.key(outputs.energy_input(1))
    (key_dir(store, new) / unfinished / "blobs").mkdir(parents=True)
    (key_dir(store, new) / unfinished / "blobs" / "output.logs").write_text("log")
    assert store.runs(new) == [] and list(store.iter()) == []
    assert store.get(new) is None
    key = store.key(outputs.energy_input())
    run = store.put(outputs.energy(), XTB)
    (key_dir(store, key) / unfinished / "blobs").mkdir(parents=True)
    store.put(outputs.energy(success=False), XTB)
    assert [r.id for r in store.runs(key)][0] == run and len(store.runs(key)) == 2
    assert store.get(key).success
    with pytest.raises(KeyError):
        store.blob(key, "logs", unfinished)


# ---------------------------------------------------------------- tags, iter


def test_tags(store):
    output = outputs.energy()
    key = store.key(output.input_data)
    store.put(output, XTB, tags=["b", "a"])
    store.tag(key, "c", "a")
    store.untag(key, "b", "never-set")
    assert sorted(p.name for p in (key_dir(store, key) / "tags").iterdir()) == [
        "a",
        "c",
    ]
    store.tag(key, "v1.2", "con-test", "x" * 64)
    bad_tags = ["", ".hidden", "UPPER", "a/b", "x" * 65, "..", "end.", "con", "nul.txt"]
    for bad in bad_tags:
        with pytest.raises(ValueError, match="A tag is"):
            store.tag(key, bad)
    with pytest.raises(TypeError):
        store.put(output, XTB, tags="screen")
    with pytest.raises(KeyError):
        store.tag(store.key(outputs.energy_input(9)), "a")


def test_iter_lists_every_key_with_a_run(store):
    keys = {store.key(outputs.energy_input(i)) for i in range(5)}
    for i in range(5):
        store.put(outputs.energy(i), XTB)
    assert set(store.iter()) == keys
    assert list(store.iter()) == sorted(keys)


# ---------------------------------------------------------------- copy_to


def test_copy_to_copies_runs_unchanged(store, tmp_path):
    key = store.key(outputs.energy_input())
    first = store.put(outputs.energy(), XTB, tags=["t"], meta={"m": 1})
    store.put(outputs.energy(1), XTB)
    other = Store(tmp_path / "other")
    assert store.copy_to(other, [key]) == 1
    assert list(other.iter()) == [key]
    assert other.runs(key) == store.runs(key)
    assert other.get(key) == store.get(key)
    source = key_dir(store, key) / first / "record.json"
    copied = key_dir(other, key) / first / "record.json"
    assert source.read_bytes() == copied.read_bytes()
    # Runs it has are skipped; withdrawals and carries follow.
    store.withdraw(key, first, "bad")
    second = store.put(outputs.energy(), XTB)
    store.carry([(key, second)], {"xtb": "6.8.0"}, {"why": "same"})
    assert store.copy_to(other) == 2
    assert other.runs(key) == store.runs(key)
    assert other.get(key, {"xtb": "6.8.0"}) == outputs.energy()
    assert (key_dir(other, key) / "tags" / "t").exists()
    assert store.copy_to(other) == 0


# ---------------------------------------------------------------- uris


def test_uris(tmp_path):
    assert Store(f"file://{tmp_path}/s").backend.root == tmp_path / "s"  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="s3://"):
        Store("s3://bucket/prefix")
    assert "Store(LocalBackend(" in repr(Store(tmp_path))


def test_the_version():
    import qcstore

    assert qcstore.__version__[0].isdigit()
