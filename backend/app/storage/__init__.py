"""Storage backends (``ARCHITECTURE.md`` 6.13).

:func:`get_storage_backend` picks the implementation from ``STORAGE_BACKEND``:

* ``local`` (default): files under ``STORAGE_PATH`` (:class:`LocalStorage`).
* ``s3``: planned (production), not implemented yet; selecting it is a
  configuration error rather than a silent fallback to local files.
"""

from app.core.config import get_settings
from app.storage.base import (
    MAX_KEY_LENGTH,
    InvalidStorageInputError,
    StorageBackend,
    StorageConfigError,
    StorageError,
    StorageIOError,
    StorageNotFoundError,
)
from app.storage.local import LocalStorage

__all__ = [
    "MAX_KEY_LENGTH",
    "InvalidStorageInputError",
    "LocalStorage",
    "StorageBackend",
    "StorageConfigError",
    "StorageError",
    "StorageIOError",
    "StorageNotFoundError",
    "get_storage_backend",
]


def get_storage_backend() -> StorageBackend:
    """The backend selected by ``STORAGE_BACKEND`` (settings are read per call)."""
    settings = get_settings()
    name = (settings.STORAGE_BACKEND or "").strip().casefold()
    if name == "local":
        return LocalStorage(settings.STORAGE_PATH)
    if name == "s3":
        raise StorageConfigError("STORAGE_BACKEND 's3' is not implemented yet")
    raise StorageConfigError(f"Unknown STORAGE_BACKEND '{settings.STORAGE_BACKEND}'")
