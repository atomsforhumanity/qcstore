"""Where a store's files live: a small interface, and the local filesystem behind it.

A store reads and writes whole files by relative path (``records/3f/k1:3f.../current.json``)
through a `Backend`, so the same store logic can run on another medium. Every write is
atomic: a reader sees a file whole or not at all. Nothing but the files a store asks to
replace is ever overwritten, so an immutable file stays immutable even if two writers
race to create it.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path, PurePosixPath
from typing import Protocol, runtime_checkable

__all__ = ["Backend", "LocalBackend"]


@runtime_checkable
class Backend(Protocol):
    """The operations a store needs of the medium holding its files.

    Paths are relative POSIX paths. Names starting with ``.`` are the backend's own
    (temporary files) and never listed.
    """

    def get(self, path: str, start: int = 0) -> bytes:
        """Return a file's bytes from offset ``start`` to its end.

        Raises:
            FileNotFoundError: No such file.
        """
        ...

    def put(self, path: str, data: bytes, *, replace: bool = False) -> None:
        """Write a file atomically, creating its directories.

        Args:
            path: The file.
            data: Its contents.
            replace: Replace the file if it exists. Otherwise an existing file is left
                as it is and `FileExistsError` raised: of two writers creating the
                same file, exactly one succeeds.

        Raises:
            FileExistsError: The file exists and ``replace`` is False.
        """
        ...

    def append(self, path: str, data: bytes) -> None:
        """Append bytes to a file, creating it; each call's bytes stay contiguous."""
        ...

    def exists(self, path: str) -> bool:
        """Whether a file or directory exists."""
        ...

    def list(self, path: str) -> list[str]:
        """The names directly inside a directory, sorted; empty if it does not exist."""
        ...

    def delete(self, path: str) -> None:
        """Remove a file if it exists."""
        ...


class LocalBackend:
    """Files in a directory on a local disk or a network filesystem (NFS).

    Writes go to a hidden temporary file beside the target, are flushed to disk, and
    are then renamed into place (to replace) or hard-linked into place (to create
    exclusively). Both are atomic on POSIX filesystems and on NFS, so concurrent
    writers on several machines need no locks.

    Attributes:
        root: The store's directory.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        """Use the directory ``root``, created on the first write if needed."""
        self.root: Path = Path(root).expanduser().absolute()

    def __repr__(self) -> str:
        """Show the directory."""
        return f"LocalBackend({str(self.root)!r})"

    def _path(self, path: str) -> Path:
        parts = PurePosixPath(path).parts
        if not parts or parts[0] == "/" or ".." in parts:
            raise ValueError(f"A store path is relative and stays inside: {path!r}.")
        return self.root.joinpath(*parts)

    def get(self, path: str, start: int = 0) -> bytes:
        """Return a file's bytes from offset ``start`` to its end."""
        with self._path(path).open("rb") as f:
            f.seek(start)
            return f.read()

    def put(self, path: str, data: bytes, *, replace: bool = False) -> None:
        """Write a file atomically: a flushed temporary file, renamed or linked in."""
        target = self._path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp")
        with temporary.open("xb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            if replace:
                os.replace(temporary, target)
            else:
                os.link(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def append(self, path: str, data: bytes) -> None:
        """Append bytes with one write to a file opened for appending."""
        target = self._path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            written = os.write(fd, data)
            if written != len(data):  # pragma: no cover - a full disk
                raise OSError(f"Short write appending to {target}.")
        finally:
            os.close(fd)

    def exists(self, path: str) -> bool:
        """Whether a file or directory exists."""
        return self._path(path).exists()

    def list(self, path: str) -> list[str]:
        """The visible names directly inside a directory, sorted."""
        try:
            names = os.listdir(self._path(path))
        except (FileNotFoundError, NotADirectoryError):
            return []
        return sorted(n for n in names if not n.startswith("."))

    def delete(self, path: str) -> None:
        """Remove a file if it exists."""
        self._path(path).unlink(missing_ok=True)
