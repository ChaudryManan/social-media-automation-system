"""Storage backend interface (``ARCHITECTURE.md`` 6.13).

Generated media (images now, video/audio later) is stored through a
:class:`StorageBackend`; nodes, services and publishers never touch the
filesystem or an object-store SDK directly.

The public methods validate their input once, here, and then call the
``_put``/``_get``/``_url``/``_delete`` hooks of the concrete backend, so every
backend (local today, S3-compatible later) rejects the same bad input in the
same way. Keys are relative, ``/``-separated and made of safe segments
(``ws/<id>/req/<id>/image.png``); absolute keys and path traversal are refused
before a backend sees them.

Errors derive from :class:`~app.core.errors.AppError` and carry ``retriable``
like the other provider layers. Key and data contents are never put in error
messages or logs.
"""

import re
from abc import ABC, abstractmethod

from app.core.errors import AppError

MAX_KEY_LENGTH = 512

# A segment starts with a letter or digit (no hidden files, no "." or ".."),
# then letters, digits, dot, underscore or hyphen. No backslash, colon, space.
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# Device names Windows refuses as file names (with or without an extension).
_WINDOWS_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.I)
_MIME = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$"
)


class StorageError(AppError):
    """Base class for storage layer failures."""

    def __init__(
        self,
        message: str = "Storage failed",
        code: str = "storage_error",
        http_status: int = 500,
        retriable: bool = True,
        details: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            code=code, message=message, http_status=http_status, details=details
        )
        self.retriable = retriable


class StorageConfigError(StorageError):
    """The storage backend is unknown or not usable (never retriable)."""

    def __init__(self, message: str = "Storage is not configured") -> None:
        super().__init__(
            message=message,
            code="storage_not_configured",
            http_status=500,
            retriable=False,
        )


class InvalidStorageInputError(StorageError):
    """A key, mime type or payload was rejected (a caller bug, never retriable).

    ``details`` holds the rejected ``field`` and a short ``reason``; it never
    echoes the value.
    """

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(
            message=f"Invalid storage {field}: {reason.replace('_', ' ')}",
            code="invalid_storage_input",
            http_status=400,
            retriable=False,
            details={"field": field, "reason": reason},
        )


class StorageNotFoundError(StorageError):
    """No object is stored under the key."""

    def __init__(self, message: str = "Stored object not found") -> None:
        super().__init__(
            message=message,
            code="storage_object_not_found",
            http_status=404,
            retriable=False,
        )


class StorageIOError(StorageError):
    """The backend could not read or write (disk full, permissions, network)."""

    def __init__(self, message: str = "Storage I/O failed") -> None:
        super().__init__(
            message=message, code="storage_io_error", http_status=500, retriable=True
        )


def validate_key(key: object) -> str:
    """Return ``key`` if it is a safe relative storage key, else raise."""
    if not isinstance(key, str):
        raise InvalidStorageInputError("key", "not_a_string")
    if not key:
        raise InvalidStorageInputError("key", "empty")
    if len(key) > MAX_KEY_LENGTH:
        raise InvalidStorageInputError("key", "too_long")
    if key.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", key):
        raise InvalidStorageInputError("key", "absolute")
    for segment in key.split("/"):
        if segment == "":
            raise InvalidStorageInputError("key", "empty_segment")
        if segment in (".", ".."):
            raise InvalidStorageInputError("key", "dot_segment")
        if not _SEGMENT.match(segment) or segment.endswith("."):
            raise InvalidStorageInputError("key", "invalid_segment")
        if _WINDOWS_RESERVED.match(segment):
            raise InvalidStorageInputError("key", "reserved_name")
    return key


def validate_mime(mime: object) -> str:
    """Return ``mime`` lower-cased if it looks like ``type/subtype``, else raise."""
    if not isinstance(mime, str):
        raise InvalidStorageInputError("mime", "not_a_string")
    normalized = mime.strip().lower()
    if not _MIME.match(normalized):
        raise InvalidStorageInputError("mime", "malformed")
    return normalized


def validate_data(data: object) -> bytes:
    """Return ``data`` as ``bytes``; it must be bytes-like and not empty."""
    if not isinstance(data, bytes | bytearray | memoryview):
        raise InvalidStorageInputError("data", "not_bytes")
    payload = bytes(data)
    if not payload:
        raise InvalidStorageInputError("data", "empty")
    return payload


class StorageBackend(ABC):
    """Interface every storage backend implements.

    All four operations are idempotent: ``put`` on an existing key replaces
    the object (a re-run job writes the same key again), ``delete`` of a
    missing key is a no-op.
    """

    name: str

    def put(self, key: str, data: bytes, mime: str) -> str:
        """Store ``data`` under ``key`` and return the key (for ``media_assets``)."""
        key = validate_key(key)
        mime = validate_mime(mime)
        payload = validate_data(data)
        self._put(key, payload, mime)
        return key

    def get(self, key: str) -> bytes:
        """Return the bytes stored under ``key`` (``StorageNotFoundError`` if none)."""
        return self._get(validate_key(key))

    def get_url(self, key: str) -> str:
        """Return a locator for the object (``StorageNotFoundError`` if none).

        The meaning depends on the backend: a ``file://`` URI for local
        storage (not served over HTTP), a time-limited signed URL for S3.
        """
        return self._url(validate_key(key))

    def delete(self, key: str) -> None:
        """Remove the object; a missing key is not an error."""
        self._delete(validate_key(key))

    @abstractmethod
    def _put(self, key: str, data: bytes, mime: str) -> None: ...

    @abstractmethod
    def _get(self, key: str) -> bytes: ...

    @abstractmethod
    def _url(self, key: str) -> str: ...

    @abstractmethod
    def _delete(self, key: str) -> None: ...
