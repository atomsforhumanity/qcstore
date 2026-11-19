import os
import threading
from pathlib import Path

import pytest

from qcstore import Backend, LocalBackend, Store, verify
from tests import outputs
from tests.outputs import XTB, ran, unmarked


class MemoryBackend:
    """The Backend interface over a dict: what an object store would offer."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.lock = threading.Lock()

    def get(self, path: str, start: int = 0) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path][start:]

    def put(self, path: str, data: bytes, *, replace: bool = False) -> None:
        with self.lock:
            if not replace and path in self.files:
                raise FileExistsError(path)
            self.files[path] = bytes(data)

    def append(self, path: str, data: bytes) -> None:
        with self.lock:
            self.files[path] = self.files.get(path, b"") + data

    def exists(self, path: str) -> bool:
        return path in self.files or any(p.startswith(path + "/") for p in self.files)

    def list(self, path: str) -> list[str]:
        prefix = path + "/"
        return sorted(
            {p[len(prefix) :].split("/")[0] for p in self.files if p.startswith(prefix)}
        )

    def delete(self, path: str) -> None:
        self.files.pop(path, None)


def test_the_store_needs_only_the_backend_interface(tmp_path):
    backend = MemoryBackend()
    assert isinstance(backend, Backend)
    store = Store(backend)
    hessian, optimization = outputs.hessian(), outputs.optimization()
    run = store.put(ran(hessian, XTB), tags=["big"])
    store.put(ran(optimization, {"qcopt": "0.3.0", "xtb": "6.7.1"}))
    key = store.key(hessian.input_data)
    assert unmarked(store.get(key)) == hessian
    assert unmarked(store.get(store.key(optimization.input_data))) == optimization
    store.carry([(key, run)], {"xtb": "6.8.0"}, {"why": "test"})
    store.withdraw(key, run, "test")
    assert store.get(key) is None and len(store.runs(key)) == 1
    assert store.blob(key, "logs", run) == b"hessian done\n"
    assert verify(store).problems == []
    local = Store(tmp_path / "local")
    assert store.copy_to(local) == 2
    assert local.runs(key) == store.runs(key)
    assert verify(local).problems == []
    index = store.index(tmp_path / "index.sqlite")
    assert len(index.query()) == 2
    with pytest.raises(ValueError, match="not local"):
        store.index()


def test_put_is_exclusive_unless_replacing(tmp_path):
    backend = LocalBackend(tmp_path)
    backend.put("a/b.json", b"1")
    with pytest.raises(FileExistsError):
        backend.put("a/b.json", b"2")
    assert backend.get("a/b.json") == b"1"
    backend.put("a/b.json", b"3", replace=True)
    assert backend.get("a/b.json") == b"3" and backend.get("a/b.json", 0) == b"3"
    assert os.listdir(tmp_path / "a") == ["b.json"]  # no temporary files left


def test_exactly_one_of_racing_creators_wins(tmp_path):
    backend = LocalBackend(tmp_path)
    won = []

    def create(i: int) -> None:
        try:
            backend.put("x", str(i).encode())
            won.append(i)
        except FileExistsError:
            pass

    threads = [threading.Thread(target=create, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(won) == 1 and backend.get("x") == str(won[0]).encode()


def test_append_list_exists_delete(tmp_path):
    backend = LocalBackend(tmp_path)
    backend.append("j/w/1.jsonl", b"a\n")
    backend.append("j/w/1.jsonl", b"b\n")
    assert backend.get("j/w/1.jsonl", 2) == b"b\n"
    (tmp_path / "j" / ".hidden.tmp").write_text("")
    assert backend.list("j") == ["w"] and backend.list("missing") == []
    assert backend.exists("j/w") and not backend.exists("j/x")
    backend.delete("j/w/1.jsonl")
    backend.delete("j/w/1.jsonl")
    assert backend.list("j/w") == []
    with pytest.raises(FileNotFoundError):
        backend.get("j/w/1.jsonl")


@pytest.mark.parametrize("path", ["/etc/passwd", "a/../../b", ""])
def test_paths_stay_inside(tmp_path, path):
    with pytest.raises(ValueError, match="stays inside"):
        LocalBackend(tmp_path).get(path)


def test_root_is_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert LocalBackend("s").root == Path(tmp_path) / "s"
    assert repr(LocalBackend("s")) == f"LocalBackend({str(tmp_path / 's')!r})"
