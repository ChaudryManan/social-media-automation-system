"""Tests for ``app.storage`` (B2-09): validation, LocalStorage, registry."""

import os
from pathlib import Path
from typing import Any

import pytest
from app.core.config import get_settings
from app.storage import (
    MAX_KEY_LENGTH,
    InvalidStorageInputError,
    LocalStorage,
    StorageBackend,
    StorageConfigError,
    StorageIOError,
    StorageNotFoundError,
    get_storage_backend,
)

PNG = b"\x89PNG\r\n\x1a\nfake-bytes"


@pytest.fixture
def store(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "media")


def reason(caught: pytest.ExceptionInfo[InvalidStorageInputError]) -> str:
    return caught.value.details["reason"]  # type: ignore[call-overload,no-any-return]


# --------------------------------------------------------------------------
# put / get / get_url / delete
# --------------------------------------------------------------------------


def test_put_get_delete_roundtrip(store: LocalStorage) -> None:
    key = "ws/1/req/2/image.png"

    assert store.put(key, PNG, "image/png") == key
    assert store.get(key) == PNG

    store.delete(key)

    with pytest.raises(StorageNotFoundError):
        store.get(key)


def test_put_creates_missing_directories_lazily(tmp_path: Path) -> None:
    root = tmp_path / "does" / "not" / "exist"
    store = LocalStorage(root)
    assert not root.exists()  # constructing touches nothing

    store.put("a/b/c.png", PNG, "image/png")

    assert (root / "a" / "b" / "c.png").read_bytes() == PNG


def test_put_on_an_existing_key_replaces_the_object(store: LocalStorage) -> None:
    store.put("x.png", b"old", "image/png")
    store.put("x.png", b"new", "image/png")

    assert store.get("x.png") == b"new"


def test_put_leaves_no_temporary_files(store: LocalStorage) -> None:
    store.put("d/x.png", PNG, "image/png")
    store.put("d/x.png", PNG, "image/png")

    assert sorted(p.name for p in (store.root / "d").iterdir()) == ["x.png"]


def test_delete_of_a_missing_key_is_a_no_op(store: LocalStorage) -> None:
    store.delete("never/stored.png")
    store.put("keep.png", PNG, "image/png")
    store.delete("never/stored.png")

    assert store.get("keep.png") == PNG


def test_delete_removes_only_that_object(store: LocalStorage) -> None:
    store.put("d/a.png", b"a", "image/png")
    store.put("d/b.png", b"b", "image/png")

    store.delete("d/a.png")

    assert store.get("d/b.png") == b"b"
    with pytest.raises(StorageNotFoundError):
        store.get("d/a.png")


def test_get_of_a_missing_key_is_not_found(store: LocalStorage) -> None:
    with pytest.raises(StorageNotFoundError) as caught:
        store.get("nope.png")

    assert caught.value.http_status == 404
    assert caught.value.code == "storage_object_not_found"
    assert caught.value.retriable is False


def test_get_url_is_a_file_uri_of_the_stored_object(store: LocalStorage) -> None:
    store.put("d/a.png", PNG, "image/png")

    url = store.get_url("d/a.png")

    assert url.startswith("file://")
    assert url.endswith("/d/a.png")
    assert url == (store.root / "d" / "a.png").as_uri()


def test_get_url_of_a_missing_key_is_not_found(store: LocalStorage) -> None:
    with pytest.raises(StorageNotFoundError):
        store.get_url("nope.png")


def test_a_directory_is_not_an_object(store: LocalStorage) -> None:
    store.put("d/a.png", PNG, "image/png")

    with pytest.raises(StorageNotFoundError):
        store.get("d")
    with pytest.raises(StorageNotFoundError):
        store.get_url("d")
    store.delete("d")  # no-op, the directory and its content survive
    assert store.get("d/a.png") == PNG


# --------------------------------------------------------------------------
# Key validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("", "empty"),
        ("/etc/passwd", "absolute"),
        ("\\windows\\x", "absolute"),
        ("C:/x.png", "absolute"),
        ("c:\\x.png", "absolute"),
        ("../x.png", "dot_segment"),
        ("a/../b.png", "dot_segment"),
        ("a/./b.png", "dot_segment"),
        ("a//b.png", "empty_segment"),
        ("a/b/", "empty_segment"),
        ("a\\b.png", "invalid_segment"),
        (".hidden", "invalid_segment"),
        ("a/.tmp-1", "invalid_segment"),
        ("a b.png", "invalid_segment"),
        ("a:b.png", "absolute"),  # a Windows drive-relative path
        ("ab:c.png", "invalid_segment"),
        ("a\x00b.png", "invalid_segment"),
        ("a/b.", "invalid_segment"),
        ("caf\u00e9.png", "invalid_segment"),
        ("con", "reserved_name"),
        ("a/NUL.txt", "reserved_name"),
        ("Com1.png", "reserved_name"),
        ("x" * (MAX_KEY_LENGTH + 1), "too_long"),
    ],
)
def test_unsafe_keys_are_rejected_by_every_operation(
    store: LocalStorage, key: str, expected: str
) -> None:
    for call in (
        lambda: store.put(key, PNG, "image/png"),
        lambda: store.get(key),
        lambda: store.get_url(key),
        lambda: store.delete(key),
    ):
        with pytest.raises(InvalidStorageInputError) as caught:
            call()
        assert reason(caught) == expected
        assert caught.value.http_status == 400
        assert caught.value.code == "invalid_storage_input"
        assert caught.value.retriable is False


@pytest.mark.parametrize("key", [None, 5, b"a.png", ["a.png"]])
def test_a_key_that_is_not_a_string_is_rejected(store: LocalStorage, key: Any) -> None:
    with pytest.raises(InvalidStorageInputError) as caught:
        store.get(key)

    assert reason(caught) == "not_a_string"


@pytest.mark.parametrize(
    "key",
    [
        "a.png",
        "ws/0b9e3a5e-7f0c-4c0a-9d8e-2f1f0a7d1c11/req/1/image-1.v2.png",
        "A_b-C.9",
        "a..b.png",  # ".." inside a name is not a traversal
    ],
)
def test_normal_keys_are_accepted(store: LocalStorage, key: str) -> None:
    assert store.put(key, PNG, "image/png") == key
    assert store.get(key) == PNG


def test_a_key_of_exactly_the_maximum_length_is_accepted(store: LocalStorage) -> None:
    # Four segments (file names stay under the 255 limit), 512 characters in all.
    key = "/".join(["x" * 127] * 3 + ["x" * 128])
    assert len(key) == MAX_KEY_LENGTH

    assert store.put(key, PNG, "image/png") == key
    assert store.get(key) == PNG


def test_a_rejected_traversal_writes_nothing_outside_the_root(
    tmp_path: Path, store: LocalStorage
) -> None:
    with pytest.raises(InvalidStorageInputError):
        store.put("../outside.png", PNG, "image/png")

    assert not (tmp_path / "outside.png").exists()
    assert not store.root.exists()  # not even the root was created


def test_the_error_never_echoes_the_key(store: LocalStorage) -> None:
    with pytest.raises(InvalidStorageInputError) as caught:
        store.put("../secret-customer-name.png", PNG, "image/png")

    assert "secret-customer-name" not in caught.value.message
    assert "secret-customer-name" not in str(caught.value.details)


def test_a_symlink_inside_the_root_cannot_lead_out_of_it(
    tmp_path: Path, store: LocalStorage
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.png").write_bytes(b"secret")
    store.root.mkdir()
    try:
        os.symlink(outside, store.root / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")

    for call in (
        lambda: store.put("link/new.png", PNG, "image/png"),
        lambda: store.get("link/secret.png"),
        lambda: store.get_url("link/secret.png"),
        lambda: store.delete("link/secret.png"),
    ):
        with pytest.raises(InvalidStorageInputError) as caught:
            call()
        assert reason(caught) == "outside_root"

    assert (outside / "secret.png").read_bytes() == b"secret"
    assert not (outside / "new.png").exists()


# --------------------------------------------------------------------------
# Mime and data validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mime", ["", "png", "image/", "/png", "image/png/x", "image png", "image/p ng"]
)
def test_a_malformed_mime_type_is_rejected(store: LocalStorage, mime: str) -> None:
    with pytest.raises(InvalidStorageInputError) as caught:
        store.put("a.png", PNG, mime)

    assert reason(caught) == "malformed"
    assert not store.root.exists()


def test_a_mime_type_that_is_not_a_string_is_rejected(store: LocalStorage) -> None:
    with pytest.raises(InvalidStorageInputError) as caught:
        store.put("a.png", PNG, None)  # type: ignore[arg-type]

    assert reason(caught) == "not_a_string"


@pytest.mark.parametrize(
    "mime", ["image/png", "IMAGE/PNG", " image/jpeg ", "video/mp4"]
)
def test_ordinary_mime_types_are_accepted(store: LocalStorage, mime: str) -> None:
    assert store.put("a.bin", PNG, mime) == "a.bin"


@pytest.mark.parametrize("data", ["text", None, 5, ["a"]])
def test_data_that_is_not_bytes_is_rejected(store: LocalStorage, data: Any) -> None:
    with pytest.raises(InvalidStorageInputError) as caught:
        store.put("a.png", data, "image/png")

    assert reason(caught) == "not_bytes"


def test_empty_data_is_rejected(store: LocalStorage) -> None:
    with pytest.raises(InvalidStorageInputError) as caught:
        store.put("a.png", b"", "image/png")

    assert reason(caught) == "empty"


def test_bytearray_and_memoryview_are_stored_as_bytes(store: LocalStorage) -> None:
    store.put("a.png", bytearray(b"abc"), "image/png")
    store.put("b.png", memoryview(b"xyz"), "image/png")

    assert store.get("a.png") == b"abc"
    assert store.get("b.png") == b"xyz"
    assert isinstance(store.get("a.png"), bytes)


# --------------------------------------------------------------------------
# Failures of the file system
# --------------------------------------------------------------------------


def test_a_file_in_the_way_of_a_directory_is_an_io_error(store: LocalStorage) -> None:
    store.put("a", PNG, "image/png")

    with pytest.raises(StorageIOError) as caught:
        store.put("a/b.png", PNG, "image/png")

    assert caught.value.retriable is True
    assert caught.value.code == "storage_io_error"
    assert store.get("a") == PNG


def test_putting_over_a_directory_is_an_io_error_and_cleans_up(
    store: LocalStorage,
) -> None:
    store.put("d/a.png", PNG, "image/png")

    with pytest.raises(StorageIOError):
        store.put("d", PNG, "image/png")

    assert sorted(p.name for p in store.root.iterdir()) == ["d"]  # no .tmp-* left
    assert store.get("d/a.png") == PNG


def test_a_failed_replace_keeps_the_old_object_and_removes_the_temp_file(
    store: LocalStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.put("x.png", b"old", "image/png")

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr("app.storage.local.os.replace", boom)

    with pytest.raises(StorageIOError) as caught:
        store.put("x.png", b"new", "image/png")

    monkeypatch.undo()
    assert "PermissionError" in caught.value.message
    assert store.get("x.png") == b"old"
    assert sorted(p.name for p in store.root.iterdir()) == ["x.png"]


def test_a_read_error_is_an_io_error(
    store: LocalStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.put("x.png", PNG, "image/png")

    def boom(_self: Path) -> bytes:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_bytes", boom)

    with pytest.raises(StorageIOError) as caught:
        store.get("x.png")

    assert caught.value.retriable is True


def test_an_object_deleted_during_a_read_is_not_found(
    store: LocalStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.put("x.png", PNG, "image/png")

    def vanished(_self: Path) -> bytes:
        raise FileNotFoundError

    monkeypatch.setattr(Path, "read_bytes", vanished)

    with pytest.raises(StorageNotFoundError):
        store.get("x.png")


def test_a_delete_error_is_an_io_error(
    store: LocalStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.put("x.png", PNG, "image/png")

    def boom(_self: Path, missing_ok: bool = False) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "unlink", boom)

    with pytest.raises(StorageIOError):
        store.delete("x.png")


def test_io_error_messages_carry_no_path(
    store: LocalStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.put("x.png", PNG, "image/png")
    monkeypatch.setattr(
        Path, "read_bytes", lambda _s: (_ for _ in ()).throw(OSError(str(store.root)))
    )

    with pytest.raises(StorageIOError) as caught:
        store.get("x.png")

    assert str(store.root) not in caught.value.message


# --------------------------------------------------------------------------
# LocalStorage construction and the shared base class
# --------------------------------------------------------------------------


@pytest.mark.parametrize("root", ["", "   "])
def test_an_empty_root_is_a_config_error(root: str) -> None:
    with pytest.raises(StorageConfigError) as caught:
        LocalStorage(root)

    assert caught.value.code == "storage_not_configured"
    assert caught.value.retriable is False


def test_a_relative_root_is_resolved_when_the_backend_is_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    store = LocalStorage("./storage")
    monkeypatch.chdir("/")

    store.put("a.png", PNG, "image/png")

    assert (tmp_path / "storage" / "a.png").read_bytes() == PNG


def test_the_interface_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        StorageBackend()  # type: ignore[abstract]


def test_a_new_backend_gets_the_same_validation_for_free() -> None:
    seen: list[tuple[str, bytes, str]] = []

    class Memory(StorageBackend):
        name = "memory"

        def _put(self, key: str, data: bytes, mime: str) -> None:
            seen.append((key, data, mime))

        def _get(self, key: str) -> bytes:
            return b""

        def _url(self, key: str) -> str:
            return key

        def _delete(self, key: str) -> None:
            seen.append((key, b"", "deleted"))

    backend = Memory()

    assert backend.put("a/b.png", bytearray(b"x"), " Image/PNG ") == "a/b.png"
    assert seen == [("a/b.png", b"x", "image/png")]
    for bad in (
        lambda: backend.put("../a", b"x", "image/png"),
        lambda: backend.put("a", b"x", "nope"),
        lambda: backend.put("a", b"", "image/png"),
        lambda: backend.delete("/a"),
    ):
        with pytest.raises(InvalidStorageInputError):
            bad()
    assert len(seen) == 1  # the hooks never saw the bad input


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    def set_env(**values: str) -> None:
        for key, value in values.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()

    yield set_env
    get_settings.cache_clear()


def test_the_registry_builds_local_storage_from_the_settings(
    env: Any, tmp_path: Path
) -> None:
    env(STORAGE_BACKEND="local", STORAGE_PATH=str(tmp_path / "m"))

    backend = get_storage_backend()

    assert isinstance(backend, LocalStorage)
    assert backend.root == (tmp_path / "m").resolve()


def test_the_registry_is_case_insensitive(env: Any, tmp_path: Path) -> None:
    env(STORAGE_BACKEND=" LOCAL ", STORAGE_PATH=str(tmp_path))

    assert isinstance(get_storage_backend(), LocalStorage)


def test_the_registry_default_is_local(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    monkeypatch.delenv("STORAGE_PATH", raising=False)
    monkeypatch.chdir(tmp_path)  # a developer's .env must not leak in
    get_settings.cache_clear()
    try:
        assert isinstance(get_storage_backend(), LocalStorage)
    finally:
        get_settings.cache_clear()


def test_s3_is_a_config_error_until_it_exists(env: Any) -> None:
    env(STORAGE_BACKEND="s3")

    with pytest.raises(StorageConfigError) as caught:
        get_storage_backend()

    assert "not implemented" in caught.value.message
    assert caught.value.retriable is False


def test_an_unknown_backend_is_a_config_error(env: Any) -> None:
    env(STORAGE_BACKEND="ftp")

    with pytest.raises(StorageConfigError) as caught:
        get_storage_backend()

    assert "ftp" in caught.value.message


def test_an_empty_storage_path_in_settings_is_a_config_error(env: Any) -> None:
    env(STORAGE_BACKEND="local", STORAGE_PATH="")

    with pytest.raises(StorageConfigError):
        get_storage_backend()
