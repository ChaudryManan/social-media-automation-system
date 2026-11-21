"""Local filesystem storage backend (development and single-host deployments).

Objects are files below one root directory (``STORAGE_PATH``). Writes are
atomic (a temporary file in the target directory, then ``os.replace``) so a
crash never leaves a half-written image behind. After the key has been
validated, the resolved path is checked once more to be inside the root, which
also stops a symlink inside the root from leading out of it.
"""

import os
import tempfile
from pathlib import Path

from app.storage.base import (
    InvalidStorageInputError,
    StorageBackend,
    StorageConfigError,
    StorageIOError,
    StorageNotFoundError,
)


class LocalStorage(StorageBackend):
    """Store objects as files under ``root`` (created on the first ``put``)."""

    name = "local"

    def __init__(self, root: str | os.PathLike[str]) -> None:
        if not str(root).strip():
            raise StorageConfigError("STORAGE_PATH is empty")
        self._root = Path(root).expanduser().resolve()

    @property
    def root(self) -> Path:
        return self._root

    def _path(self, key: str) -> Path:
        path = (self._root / key).resolve()
        if not path.is_relative_to(self._root):
            raise InvalidStorageInputError("key", "outside_root")
        return path

    def _put(self, key: str, data: bytes, mime: str) -> None:
        # ``mime`` is not stored: the media_assets row keeps it (DATABASE 4.9).
        path = self._path(key)
        tmp_name: str | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp_name, path)
            tmp_name = None
        except OSError as exc:
            raise StorageIOError(
                f"Local storage write failed ({type(exc).__name__})"
            ) from None
        finally:
            if tmp_name is not None:
                Path(tmp_name).unlink(missing_ok=True)

    def _get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise StorageNotFoundError()
        try:
            return path.read_bytes()
        except FileNotFoundError:
            raise StorageNotFoundError() from None
        except OSError as exc:
            raise StorageIOError(
                f"Local storage read failed ({type(exc).__name__})"
            ) from None

    def _url(self, key: str) -> str:
        path = self._path(key)
        if not path.is_file():
            raise StorageNotFoundError()
        return path.as_uri()

    def _delete(self, key: str) -> None:
        path = self._path(key)
        if not path.is_file():
            return
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise StorageIOError(
                f"Local storage delete failed ({type(exc).__name__})"
            ) from None
