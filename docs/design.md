# Design

qcstore keeps every run of every calculation. This page describes the files it writes,
how concurrent writers agree without locks, and what a crash can leave behind.

## Two identities

A stored result answers two questions, kept apart:

- **Which calculation?** The input. Its key, `qcdata.calculation_key(inp)`, is the
  sha256 of qcdata's canonical form of the input as given: `"k1:"` and 64 hex digits.
  It never consults a program, and it does not change when code changes, so anything
  holding keys (reaction networks, campaign lists, tags) stays valid across releases.
- **Which code produced the numbers?** The *numerics identity* of each program the
  input names, `{program: numerics}`: a string that changes whenever results for an
  unchanged input may change. The caller supplies it to `put` (qccompute's runner
  asks each program), and every run records it. A Python library's is its name and
  version (`{"tblite": "tblite 0.8.0"}`), an external executable's the sha256 of the
  file (`{"xtb": "sha256:9c1f…"}`), a program with numerics epochs its epoch, an ML
  potential its model's checksum. qcstore only compares them as strings.

A reader asks for a calculation and, optionally, for identities. `get(key)` returns the
latest success whatever produced it; `get(key, {"tblite": "tblite 0.9.0"})` only a run produced by
exactly those identities, or *carried* to them: marked valid for them, with the evidence,
after a release that changed no results for it.

Each run also records the environment it ran in, as its output reports it
(`execution`: host name, CPU model, GPU devices, libc version). Lookups match on
identities only; a reader who needs results from one kind of device filters on the
environment, in `runs()` or in the index.

## Layout

```text
records/k1/<2 hex>/<64 hex>/
    current.json                    the preferred run, overall and per identity set
    tags/<tag>                      an empty file per tag on the key
    <run>/
        record.json                 identities, meta, and the output
        blobs/<path>                logs, files and large arrays, named by JSON path
        withdrawn.json              only if withdrawn: by, time, reason
        carries/<id>                an empty marker per carry of the run
carries/<id>.json                   a carry: identities, evidence, by, time
journals/<writer>/<batch>.jsonl     what each writer changed, for the index
```

A key `k1:<64 hex>` lives in `records/k1/<first 2 hex>/<64 hex>/`: its version and
hash as directories, so no name holds a colon, which Windows forbids, and keys spread
over 256 shards. Tags are lower case and never start or end with a dot or take a name
Windows reserves (`con`, `nul`, `com1`, ...). A run id is the UTC time of the `put`
to the microsecond and a random suffix (`20261117T101500.123456Z-7c1e9a0b`), so run
directories sort by time.

**`record.json`** is plain, uncompressed JSON:

```json
{
  "format": 1,
  "key": "k1:3f9a...",
  "run": "20261117T101500.123456Z-7c1e9a0b",
  "identities": {"tblite": "tblite 0.8.0"},
  "meta": {"campaign": "screen-1"},
  "blobs": {
    "output.logs": {"sha256": "e3bf..."},
    "output.results.files.orca%2Egbw": {"sha256": "06eb...", "binary": true},
    "output.results.hessian.npy": {"sha256": "56a4..."}
  },
  "output": {"success": true, "input_data": {...}, "results": {...}, ...}
}
```

`output` is the `ProgramOutput` as qcdata saves it, every field written (none left out
as unset, so nothing set after construction is lost), with its bulky parts moved to
`blobs/`:

- every `logs` and every `files` entry, at any depth (the output, each frame, the
  input's files, each subprogram's files, given results' files), named by its JSON path
  in the record. Path parts other than letters, digits, `_` and `-` are written `%XX`,
  as in qcdata's array file names. Text is stored as UTF-8, binary files as their bytes
  (`"binary": true`), so `cp blobs/output.results.files.orca%2Egbw orca.gbw` gives the
  file back;
- every array qcdata's `save` puts in its own `.npy` file (over 1 MiB), under the name
  qcdata gives it, which is again its JSON path.

Tracebacks, errors, warnings and `record` stay in the record. The `blobs` table holds
every blob's sha256, checked when the blob is read back.

**`current.json`** names the preferred run, for any identities and for each identity
set:

```json
{
  "preferred": "20261117T101500.123456Z-7c1e9a0b",
  "by_identities": [
    {"identities": {"tblite": "tblite 0.8.0"}, "preferred": "20261117T101500.123456Z-7c1e9a0b"},
    {"identities": {"tblite": "tblite 0.9.0"}, "preferred": "20261118T090000.000000Z-0b1c2d3e"}
  ]
}
```

The preferred run among some runs is the latest success not withdrawn, else the latest
run not withdrawn, else none. `preferred` applies the rule to every run;
each entry of `by_identities` to the runs produced by, or carried to, that identity
set. An identity set whose runs are all withdrawn has no entry. Entries are a list of
identities rather than a map keyed by a digest of them: the file stays readable, a
lookup compares the identities themselves, and a key has only a few identity sets.

`current.json` is a cache of the runs: `verify --repair` rebuilds it from them.

## Writing without locks

Every file is written atomically: to a hidden temporary file beside it, flushed, then
renamed into place (to replace) or hard-linked into place (to create). Linking fails if
the target exists, so of two writers creating the same file exactly one succeeds, and an
immutable file cannot be overwritten by mistake. Both operations are atomic on POSIX
filesystems and on NFS.

- **A run** is new files in a new directory: the blobs first, then `record.json`. The
  record is the commit marker: a run without one is unfinished (still being written, or
  its writer died) and every reader ignores it.
- **Markers** (`withdrawn.json`, a run's `carries/<id>`) and **tags** are created
  exclusively. The first withdrawal of a run stands. Untagging deletes the tag file;
  nothing else is ever deleted.
- **A carry** is one file, `carries/<id>.json` at the top of the store, written first
  and named by the sha256 of its bytes: the new identities, the evidence, who and when.
  Then each run carried gets an empty marker `carries/<id>` and its key's pointer is
  updated. Carrying 1,600 runs writes one evidence file and 1,600 empty markers. A run
  may be carried more than once, even to the same identities; each carry is more
  evidence. A carry asserts that the run's whole output, `record` included, is what
  the new code would produce for its input, so a release that changes the shape of
  `record` cannot be carried.
- **`current.json`** is the only file ever replaced. After any change to a key's runs, a
  writer reads the runs, computes the pointer, replaces `current.json`, then reads the
  runs again and repeats until the pointer it wrote matches them.

Why the pointer ends right: consider the last replacement of `current.json`, by writer
W. After it, W read the runs once more and found its pointer still correct. Any change
completed after that reading would have been followed by its own writer's replacement,
later than W's, contradicting W's being last. So W's final reading saw every change, and
the pointer is right. Two writers that read the same runs write the same bytes.

## Reading

`get(key, identities)` reads `current.json`, then that run's `record.json`, its arrays
and (unless `blobs=False`) its logs and files. `blob(key, path)` reads one blob alone.
`runs(key)` reads every complete run with its markers.

A pointer can lag behind the runs when a writer is killed between changing them and
replacing it (see below). A run it names is always complete, so a lagging pointer can
only miss newer runs, which costs a recomputation and nothing worse, except in one case:
it may name a run withdrawn since. Readers therefore check that the named run is not
withdrawn, and if it is, or if there is no `current.json` yet, work the preference out
from the runs.

## Crashes

What a writer killed at any point leaves:

| killed | left behind | readers see |
|---|---|---|
| before `record.json` | an unfinished run directory | nothing: the run does not exist |
| after `record.json`, before `current.json` | a complete run; the pointer lags | the run in `runs()`; `get` may return the previous preferred run until the key changes again or `verify --repair` |
| after withdrawing, before `current.json` | the marker; the pointer may name the withdrawn run | `get` sees the withdrawal and skips the run |
| partway through a carry | the carry's file, and markers on some of its runs | the marked runs carried, the rest not; carrying the rest again is harmless |
| anywhere in a journal line | a partial last line | the index waits for the newline |

`python -m qcstore verify URI` checks every complete run (its record names its key and
run, its output loads with every blob and array matching its sha256, its input's key is
the key it is filed under, each of its carries' files matches its sha256) and every
`current.json`. It lists unfinished runs, which are not problems. `--repair` rewrites
missing or lagging pointers and changes nothing else.

## Journals and the index

The SQLite index answers questions across calculations ("how many calculations per
molecule", "every result under linus epoch 8"). It is a cache, rebuildable from the
runs, and optional: the store never needs it.

Each writer process appends to its own journal, one file per store object, so no two
processes write one file. A change appends two lines: one naming the key before the
change starts, `{"n": 7, "op": "put", "key": ..., "run": ..., "time": ...}`, and
`{"done": 7}` after it ends, success or not. The index:

- `update()` reads each journal from where it stopped, up to its last newline, and
  re-reads from the store every key whose change is done, replacing that key's rows. A
  change that started but never finished (its writer was killed) keeps its key
  *pending*, re-read on every update, so a change that completed just before the kill
  is still indexed.
- `rebuild()` reads the journals first (so changes in flight stay pending), then every
  key in the store.

Rows always come from the store, never from journal lines, and a key's rows are
replaced whole, so the order in which journals are read does not matter, and updating
and rebuilding end with the same index.

Tables: `runs` (one row per run: key, run, time, program, version, calctype, method,
basis, formula, success, error kind, wall time, identities, withdrawn, preferred, the
whole input as JSON, and the environment: host, CPU model, devices, libc), `carries` (one
row per run carried, with the carry's id, identities and evidence), `tags` and `meta`.
JSON columns are queried with
SQLite's JSON functions: `json_extract(identities, '$.linus') = '8'`.

The index file lives in the store's directory by default. SQLite's locks need a local
disk: for a store on NFS, keep the index on a local disk (`store.index(path)`).

## Backends

The store reaches its files through a `Backend`: `get` (from an offset), `put`
(atomic; exclusive unless replacing), `append`, `exists`, `list` and `delete`. The
local backend serves local disks and NFS. Object stores fit the same interface, with
`put` exclusive through conditional writes; their journals would be written as
objects rather than appended to, which is why journals sit behind the interface too.

## Known costs

- A `put` reads every complete run of its key (to compute the pointer). Keys normally
  have a few runs; a key with many large runs makes its puts slower.
- `update()` opens every journal file; a store written by very many processes
  accumulates many small journals.
- Blob file names grow with the depth of a path and with `%XX` escapes; a file name
  over the filesystem's limit (usually 255 bytes) cannot be stored.
