"""What the writer processes of the concurrency tests do; imported by each child."""

from __future__ import annotations

import json
import os
import random
import signal
from pathlib import Path

from qcstore import LocalBackend, Store
from tests import outputs
from tests.outputs import ran

#: Numerics a writer may report for xtb: runs of one key under several identities.
VERSIONS = ["6.7.1", "6.8.0"]


def mixed(root: str, worker: int, keys: int, steps: int, report: str) -> None:
    """Random puts, withdrawals, carries, tags and untags on a few shared keys.

    Writes the ids of the runs it put to ``report`` when done.
    """
    store = Store(root)
    rng = random.Random(worker)
    mine = []
    for _ in range(steps):
        i = rng.randrange(keys)
        key = store.key(outputs.energy_input(i))
        version = rng.choice(VERSIONS)
        action = rng.random()
        runs = store.runs(key)
        if action < 0.6 or not runs:
            output = outputs.energy(i, success=rng.random() < 0.7)
            tags = [f"w{worker}"] if rng.random() < 0.5 else []
            mine.append(
                store.put(ran(output, {"xtb": version}), tags=tags, meta={"w": worker})
            )
        elif action < 0.7:
            store.withdraw(key, rng.choice(runs).id, f"by writer {worker}")
        elif action < 0.8:
            run = rng.choice(runs)
            other = {"xtb": next(v for v in VERSIONS if v != run.identities["xtb"])}
            store.carry([(key, run.id)], other, {"writer": worker})
        elif action < 0.9:
            store.tag(key, "shared", f"w{worker}")
        else:
            store.untag(key, "shared")
    Path(report).write_text(json.dumps(mine))


def forever(root: str, worker: int, keys: int) -> None:
    """Put, withdraw and tag until killed."""
    store = Store(root)
    rng = random.Random(os.getpid())
    while True:
        i = rng.randrange(keys)
        output = outputs.energy(i, success=rng.random() < 0.7)
        run = store.put(ran(output, {"xtb": rng.choice(VERSIONS)}), tags=[f"w{worker}"])
        if rng.random() < 0.2:
            store.withdraw(store.key(output.input_data), run, "test")
        if rng.random() < 0.2:
            store.untag(store.key(output.input_data), f"w{worker}")


class Dying(LocalBackend):
    """A local backend whose process is killed (SIGKILL) at a chosen point."""

    def __init__(self, root: str, point: str) -> None:
        super().__init__(root)
        self.point = point

    def put(self, path: str, data: bytes, *, replace: bool = False) -> None:
        if path.endswith(f"/{self.point}"):
            os.kill(os.getpid(), signal.SIGKILL)
        super().put(path, data, replace=replace)

    def append(self, path: str, data: bytes) -> None:
        if self.point == "done" and b'"done"' in data:
            os.kill(os.getpid(), signal.SIGKILL)
        super().append(path, data)


def die_while_putting(root: str, point: str, i: int) -> None:
    """Put one energy of molecule ``i``, killed at ``point``: a file name or "done"."""
    Store(Dying(root, point)).put(ran(outputs.energy(i), {"xtb": "6.8.0"}))


def die_while_withdrawing(root: str, key: str, run: str) -> None:
    """Withdraw a run, killed before the pointer is replaced."""
    Store(Dying(root, "current.json")).withdraw(key, run, "bad")
