# qcstore

A store for quantum chemistry results. Every run of every calculation is kept as plain
JSON you can read with `cat` and `jq`, filed under the calculation's key, and nothing is
ever overwritten or deleted. Many processes can write to one store at once, on a local
disk or NFS, without locks, and a writer killed mid-write leaves nothing a reader trusts.

qcstore depends only on [qcdata](https://github.com/atomsforhumanity/qcdata) and the
Python standard library.

## Install

qcstore is in alpha. Install it with the alpha releases of the rest of the stack:

```sh
uv pip install --prerelease=allow "qcdata==0.24.0a2" "qccompute[qccodec]==0.23.0a1" "qcstore==0.1.0a1"
```

## Five minutes

A store files each calculation under its key, `qcdata.calculation_key(inp)`: the input
alone. Each run also records the *numerics identity* of every program that produced it,
so results of different code stay apart while the key stays the same across releases.
With qccompute, the runner supplies the identities: it asks each program for a string
that changes whenever its results may change. For a Python library that is its name and
version (`{"tblite": "tblite 0.8.0"}`); for an external executable, the sha256 of the
file (`{"xtb": "sha256:9c1f…"}`); for a program with numerics epochs, the epoch.
`qccompute.Runner().identities(inp)` shows them for an input.

```python
from qcdata import ProgramInput, ProgramOutput, SinglePointData, Structure
from qcstore import Store

water = Structure(
    symbols=["O", "H", "H"],
    geometry=[[0.0, 0.0, 0.0], [0.0, 1.43, 1.11], [0.0, -1.43, 1.11]],  # bohr
)
inp = ProgramInput(
    program="tblite", calctype="energy", structure=water, model={"method": "GFN2-xTB"}
)
output = ProgramOutput(
    input_data=inp,
    success=True,
    logs="SCF converged in 9 iterations\n",
    results=SinglePointData(
        provenance={"program": "tblite", "program_version": "0.8.0"}, energy=-5.07
    ),
)

store = Store("./store")
tblite = {"tblite": "tblite 0.8.0"}
run = store.put(output, tblite, tags=["demo"], meta={"by": "readme"})

key = store.key(inp)
store.get(key)                     # the latest success, whatever produced it
store.get(key, tblite)             # only a run of tblite 0.8.0
store.get(key, {"tblite": "tblite 0.9.0"})   # None: never run with 0.9.0
store.blob(key, "logs")            # b"SCF converged in 9 iterations\n"
store.get(key).execution.store_run # "k1:35a1.../20261117T...": read from this store
```

A new release that gives the same results need not rerun anything: carry the runs to the
new identities, with the evidence. The evidence is stored once, however many runs it
covers, and asserts that each run's whole output is what the new code would produce.

```python
newer = {"tblite": "tblite 0.9.0"}
store.carry([(key, run)], newer, {"benchmark": "S22", "max_abs_diff": 0.0})
store.get(key, newer)              # the 0.8.0 run, valid for 0.9.0 too
```

A wrong result is withdrawn, not deleted:

```python
store.withdraw(key, run, "wrong geometry")
store.get(key)                     # None: no other run
for r in store.runs(key):
    print(r.id, r.success, r.identities, r.withdrawn.reason)
```

Questions across calculations go to an optional SQLite index, kept up to date from the
store's journals:

```python
index = store.index()
index.query(tag="demo", program="tblite")
index.query(identities=newer)      # every run valid under tblite 0.9.0
index.sql(
    "SELECT formula, COUNT(DISTINCT key) AS calculations FROM runs GROUP BY formula"
)
```

With [qccompute](https://github.com/atomsforhumanity/qccompute), a store caches
calculations: `qccompute.compute(inp, store=store)` returns a stored result when one
from the same code exists, and stores what it runs. A stored result says so:
`execution.store_run` names the key and run it was read from, and is None on a fresh
run.

## On disk

```text
store/
  records/k1/3f/3f9a.../               one directory per calculation, k1:3f9a...
    current.json                       the preferred run, overall and per identity set
    tags/demo                          an empty file per tag
    20261117T101500.123456Z-7c1e9a0b/  one directory per run, never changed
      record.json                      identities, meta, and the output
      blobs/output.logs                logs, files and large arrays, named by JSON path
      carries/9c1f...                  an empty marker per carry of this run
      withdrawn.json                   who withdrew it, when, and why
  carries/9c1f....json                 a carry: "also valid for tblite 0.9.0", the evidence
  journals/<writer>/<batch>.jsonl      what each writer changed, for the index
  index.sqlite                         the optional index, rebuildable at any time
```

`python -m qcstore verify ./store` checks every record: keys, blob digests, pointers.

## Documentation

[qcstore.docs.afh.org](https://qcstore.docs.afh.org): the API, and the
[design](https://qcstore.docs.afh.org/design/): the layout, how writers agree without
locks, and what a crash can and cannot leave behind.
