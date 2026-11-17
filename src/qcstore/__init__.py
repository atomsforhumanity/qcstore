"""A store for quantum chemistry results: every run of every calculation, as plain JSON.

```python
from qcstore import Store

store = Store("./store")
run = store.put(output, {"tblite": "tblite 0.8.0"}, tags=["screen"])
same = store.get(store.key(output.input_data), {"tblite": "tblite 0.8.0"})
```
"""

from importlib.metadata import version as _version

from .backend import Backend, LocalBackend
from .index import Index
from .store import Carry, Run, Store, Withdrawal
from .verify import Report, verify

__version__ = _version("qcstore")

__all__ = [
    "__version__",
    "Backend",
    "Carry",
    "Index",
    "LocalBackend",
    "Report",
    "Run",
    "Store",
    "Withdrawal",
    "verify",
]
