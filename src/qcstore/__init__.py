"""A store for quantum chemistry results: every run of every calculation, as plain JSON.

```python
from qcstore import Store

store = Store("./store")
run = store.put(output, {"xtb": "6.7.1"}, tags=["screen"])
same = store.get(store.key(output.input_data), {"xtb": "6.7.1"})
```
"""

from .backend import Backend, LocalBackend
from .index import Index
from .store import Carry, Run, Store, Withdrawal
from .verify import Report, verify

__all__ = [
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
