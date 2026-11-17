# qcstore

A store for quantum chemistry results. Every run of every calculation is kept as plain
JSON you can read with `cat` and `jq`, filed under the calculation's key, and nothing is
ever overwritten or deleted. Many processes can write to one store at once, on a local
disk or NFS, without locks, and a writer killed mid-write leaves nothing a reader trusts.

qcstore depends only on [qcdata](https://github.com/atomsforhumanity/qcdata) and the
Python standard library.

## Install

```sh
pip install qcstore
```

## Five minutes

A store files each calculation under its key, `qcdata.calculation_key(inp)`: the input
alone. Each run also records the *numerics identity* of every program that produced it
(`{"xtb": "6.7.1"}`), so results of different code stay apart while the key stays the
same across releases.

```python
from qcdata import ProgramInput, ProgramOutput, SinglePointData, Structure
from qcstore import Store

water = Structure(
    symbols=["O", "H", "H"],
    geometry=[[0.0, 0.0, 0.0], [0.0, 1.43, 1.11], [0.0, -1.43, 1.11]],  # bohr
)
inp = ProgramInput(
    program="xtb", calctype="energy", structure=water, model={"method": "gfn2"}
)
output = ProgramOutput(
    input_data=inp,
    success=True,
    logs="normal termination of xtb\n",
    results=SinglePointData(
        provenance={"program": "xtb", "program_version": "6.7.1"}, energy=-5.07
    ),
)

store = Store("./store")
run = store.put(output, {"xtb": "6.7.1"}, tags=["demo"], meta={"by": "readme"})

key = store.key(inp)
store.get(key)                     # the latest success, whatever produced it
store.get(key, {"xtb": "6.7.1"})   # only a run of xtb 6.7.1
store.get(key, {"xtb": "6.8.0"})   # None: never run with 6.8.0
store.blob(key, "logs")            # b"normal termination of xtb\n"
```

A new release that gives the same numbers need not rerun anything: carry the run to the
new identities, with the evidence.

```python
store.carry(key, run, {"xtb": "6.8.0"}, {"compared": "energy", "max_abs_diff": 0.0})
store.get(key, {"xtb": "6.8.0"})   # the 6.7.1 run, valid for 6.8.0 too
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
index.query(tag="demo", program="xtb")
index.query(identities={"xtb": "6.8.0"})   # every run valid under xtb 6.8.0
index.sql(
    "SELECT formula, COUNT(DISTINCT key) AS calculations FROM runs GROUP BY formula"
)
```

With [qccompute](https://github.com/atomsforhumanity/qccompute), a store caches
calculations: `qccompute.compute(inp, store=store)` returns a stored result when one
from the same code exists, and stores what it runs.

## On disk

```text
store/
  records/3f/k1:3f9a.../               one directory per calculation
    current.json                       the preferred run, overall and per identity set
    tags/demo                          an empty file per tag
    20261117T101500.123456Z-7c1e9a0b/  one directory per run, never changed
      record.json                      identities, meta, and the output
      blobs/output.logs                logs, files and large arrays, named by JSON path
      carries/5d41402abc4b2a76.json    "also valid for xtb 6.8.0", with the evidence
      withdrawn.json                   who withdrew it, when, and why
  journals/<writer>/<batch>.jsonl      what each writer changed, for the index
  index.sqlite                         the optional index, rebuildable at any time
```

`python -m qcstore verify ./store` checks every record: keys, blob digests, pointers.

## Documentation

[qcstore.docs.afh.org](https://qcstore.docs.afh.org): the API, and the
[design](https://qcstore.docs.afh.org/design/): the layout, how writers agree without
locks, and what a crash can and cannot leave behind.
