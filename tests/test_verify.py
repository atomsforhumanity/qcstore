import json
import subprocess
import sys
from pathlib import Path

import pytest

from qcstore import Store, verify
from qcstore.__main__ import main
from qcstore.store import _key_dir
from tests import outputs
from tests.outputs import XTB, unmarked


@pytest.fixture
def store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "store")
    store.put(outputs.energy(), XTB)
    store.put(outputs.hessian(), XTB)
    store.put(outputs.optimization(), {"qcopt": "0.3.0", "xtb": "6.7.1"})
    return store


def run_dir(store: Store, output) -> Path:
    key = store.key(output.input_data)
    [run] = store.runs(key)
    return store.backend.root / _key_dir(key) / run.id  # type: ignore[attr-defined]


def test_a_sound_store_passes(store, capsys):
    assert main(["verify", str(store.backend.root)]) == 0  # type: ignore[attr-defined]
    assert (
        capsys.readouterr().out == "3 calculations, 3 runs, 0 incomplete, 0 problems\n"
    )


def test_the_command_line(store):
    done = subprocess.run(
        [sys.executable, "-m", "qcstore", "verify", str(store.backend.root)],  # type: ignore[attr-defined]
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0 and "0 problems" in done.stdout


@pytest.mark.parametrize(
    ("make", "damage", "message"),
    [
        (outputs.energy, "blobs/output.logs", "sha256"),
        (outputs.optimization, "blobs/output.results.frames.1.logs", "sha256"),
        (outputs.hessian, "blobs/output.results.hessian.npy", "sha256"),
        (outputs.hessian, "blobs/output.input_data.given.hessian.npy", "sha256"),
    ],
)
def test_damaged_blobs_are_found(store, make, damage, message):
    path = run_dir(store, make()) / damage
    path.write_bytes(path.read_bytes()[:-1] + b"!")
    [problem] = verify(store).problems
    assert message in problem and str(path.parent.parent.name) in problem


def test_a_record_filed_under_the_wrong_key_is_found(store):
    path = run_dir(store, outputs.energy()) / "record.json"
    record = json.loads(path.read_text())
    record["output"]["input_data"]["keywords"]["accuracy"] = 1.0
    path.write_text(json.dumps(record))
    [problem] = verify(store).problems
    assert "not the key it is filed under" in problem


def test_a_stale_pointer_is_found_and_repaired(store, capsys):
    path = run_dir(store, outputs.energy()).parent / "current.json"
    path.write_text('{"preferred": null, "by_identities": []}\n')
    root = str(store.backend.root)  # type: ignore[attr-defined]
    assert main(["verify", root]) == 1
    assert "stale; `verify --repair` rewrites it" in capsys.readouterr().out
    assert main(["verify", root, "--repair"]) == 0
    assert "repaired: records/" in capsys.readouterr().out
    assert unmarked(store.get(store.key(outputs.energy_input()))) == outputs.energy()
    assert main(["verify", root]) == 0


def test_incomplete_runs_are_noted_not_problems(store, capsys):
    directory = (
        run_dir(store, outputs.energy()).parent / "20991231T000000.000000Z-0000abcd"
    )
    (directory / "blobs").mkdir(parents=True)
    assert main(["verify", str(store.backend.root)]) == 0  # type: ignore[attr-defined]
    out = capsys.readouterr().out
    assert "incomplete (ignored): records/" in out and "1 incomplete" in out


def test_an_unreadable_record_is_a_problem(store):
    (run_dir(store, outputs.energy()) / "record.json").write_text("{not json")
    [problem] = verify(store).problems
    assert "unreadable" in problem


def test_a_changed_carry_is_found(store):
    key = store.key(outputs.energy_input())
    [run] = store.runs(key)
    carry = store.carry([(key, run.id)], {"xtb": "6.8.0"}, {"why": "same"})
    path = store.backend.root / "carries" / f"{carry}.json"  # type: ignore[attr-defined]
    path.write_text(path.read_text().replace("same", "different"))
    [problem] = verify(store).problems
    assert "does not match its sha256" in problem


def test_strays_under_records_are_problems(store):
    root = store.backend.root  # type: ignore[attr-defined]
    (root / "records" / "ab" / ("k1:" + "ab" * 32)).mkdir(parents=True)  # 0.1.0a1
    (root / "records" / "k1" / "zz").mkdir()
    (root / "records" / "k1" / "00" / ("ff" * 32)).mkdir(parents=True)
    rule = "not part of the layout (records/k1/<2 hex>/<64 hex>)"
    assert verify(store).problems == [
        f"records/ab: {rule}",
        f"records/k1/00/{'ff' * 32}: {rule}",
        f"records/k1/zz: {rule}",
    ]
