"""Full-surface tests for ``LocalStorage`` over a temp storage root."""

from __future__ import annotations

import os

import pytest
from tai42_contract.storage import ObjectStat, Storage, StoredObject

from tai42_storage_local.settings import storage_settings
from tai42_storage_local.storage import LocalStorage


@pytest.fixture
def storage(storage_root: object) -> LocalStorage:
    return LocalStorage()


# --- registration + settings wiring ----------------------------------------


def test_backend_registered_on_import(registered_provider: Storage | None):
    assert isinstance(registered_provider, LocalStorage)
    assert isinstance(registered_provider, Storage)


def test_settings_env_prefix(storage_root):
    settings = storage_settings()
    assert settings.root_path == str(storage_root)
    assert settings.create_dirs is True


# --- text surface -----------------------------------------------------------


async def test_upload_then_load(storage, storage_root):
    await storage.upload("greeting.txt", "hello")
    assert await storage.load("greeting.txt") == "hello"
    assert (storage_root / "greeting.txt").read_text() == "hello"


async def test_load_missing_raises_file_not_found(storage):
    with pytest.raises(FileNotFoundError, match="Object not found"):
        await storage.load("nope.txt")


async def test_list(storage):
    await storage.upload("a.txt", "a")
    await storage.upload("d/b.txt", "b")
    assert sorted(await storage.list()) == ["a.txt", os.path.join("d", "b.txt")]


async def test_list_a_directory_answers_only_the_objects_beneath_it(storage):
    await storage.upload("a.txt", "a")
    await storage.upload("d/b.txt", "b")
    await storage.upload("d/e/c.txt", "c")
    await storage.upload("dd/x.txt", "x")
    assert sorted(await storage.list("d")) == ["d/b.txt", "d/e/c.txt"]
    assert await storage.list("absent") == []


async def test_delete(storage, storage_root):
    await storage.upload("gone.txt", "x")
    await storage.delete("gone.txt")
    assert not (storage_root / "gone.txt").exists()


async def test_delete_missing_raises_file_not_found(storage):
    with pytest.raises(FileNotFoundError, match="Object not found"):
        await storage.delete("nope.txt")


async def test_delete_dir(storage, storage_root):
    await storage.upload("dir/a.txt", "a")
    await storage.delete_dir("dir")
    assert not (storage_root / "dir").exists()


@pytest.mark.parametrize("root", ["", ".", "/", "..", "../x", "/.."])
async def test_delete_dir_root_escape_refused(storage, root):
    with pytest.raises(ValueError, match="storage root"):
        await storage.delete_dir(root)


# --- traversal guard on read/write -----------------------------------------


async def test_load_traversal_rejected(storage):
    with pytest.raises(ValueError, match="outside the storage root"):
        await storage.load("../escape.txt")


async def test_upload_traversal_rejected(storage):
    with pytest.raises(ValueError, match="outside the storage root"):
        await storage.upload("../escape.txt", "x")


# --- binary surface ---------------------------------------------------------


async def test_upload_bytes_load_bytes_round_trip_identical(storage):
    payload = bytes(range(256)) * 8  # not valid UTF-8 — proves the binary path
    await storage.upload_bytes("blob.bin", payload)
    assert await storage.load_bytes("blob.bin") == payload


async def test_upload_bytes_ignores_content_type(storage, storage_root):
    await storage.upload_bytes("img.png", b"\x89PNG\r\n", content_type="image/png")
    assert (storage_root / "img.png").read_bytes() == b"\x89PNG\r\n"


async def test_load_object_answers_the_bytes_and_the_path_type_in_one_file_read(storage, monkeypatch):
    await storage.upload_bytes("img.png", b"\x89PNG\r\n")
    driver_reads: list[str] = []
    real_driver = storage._driver

    def _recording_driver():
        driver = real_driver()
        for name in ("read_bytes", "read_file", "stat_size", "read_range"):
            real = getattr(driver, name)

            async def _record(*args, _name=name, _real=real):
                driver_reads.append(_name)
                return await _real(*args)

            setattr(driver, name, _record)
        return driver

    monkeypatch.setattr(storage, "_driver", _recording_driver)
    assert await storage.load_object("img.png") == StoredObject(data=b"\x89PNG\r\n", content_type="image/png")
    assert driver_reads == ["read_bytes"]


async def test_load_object_missing_raises_file_not_found(storage):
    with pytest.raises(FileNotFoundError, match="Object not found"):
        await storage.load_object("nope.bin")


async def test_load_bytes_missing_raises_file_not_found(storage):
    with pytest.raises(FileNotFoundError, match="Object not found"):
        await storage.load_bytes("nope.bin")


# --- stat ---------------------------------------------------------------------


async def test_stat_answers_size_and_path_inferred_content_type(storage, storage_root):
    (storage_root / "photo.png").write_bytes(b"\x89PNG\r\n")
    assert await storage.stat("photo.png") == ObjectStat(content_type="image/png", size=6)


async def test_stat_unknown_suffix_returns_none(storage, storage_root):
    (storage_root / "data.unknownext").write_bytes(b"x")
    assert await storage.stat("data.unknownext") == ObjectStat(content_type=None, size=1)


async def test_stat_missing_raises_file_not_found(storage):
    with pytest.raises(FileNotFoundError, match=r"Object not found: never/written\.mp3"):
        await storage.stat("never/written.mp3")


async def test_a_directory_is_not_an_object(storage, storage_root):
    (storage_root / "jq").mkdir()
    (storage_root / "jq" / "fetch.jq").write_text(".items")
    with pytest.raises(FileNotFoundError, match="Object not found: jq"):
        await storage.stat("jq")
    assert await storage.stat("jq/fetch.jq") == ObjectStat(content_type=None, size=6)


# --- load_range ---------------------------------------------------------------


async def test_load_range_reads_the_head_and_the_tail(storage, storage_root):
    (storage_root / "blob.bin").write_bytes(b"0123456789")
    assert await storage.load_range("blob.bin", 0, 4) == b"0123"
    assert await storage.load_range("blob.bin", 6, 4) == b"6789"


async def test_load_range_running_past_the_end_answers_the_tail_only(storage, storage_root):
    (storage_root / "blob.bin").write_bytes(b"0123456789")
    assert await storage.load_range("blob.bin", 8, 10) == b"89"


async def test_load_range_past_the_end_answers_empty(storage, storage_root):
    (storage_root / "blob.bin").write_bytes(b"0123456789")
    assert await storage.load_range("blob.bin", 10, 4) == b""
    assert await storage.load_range("blob.bin", 50, 4) == b""


async def test_load_range_missing_raises_file_not_found(storage):
    with pytest.raises(FileNotFoundError, match=r"Object not found: nope\.bin"):
        await storage.load_range("nope.bin", 0, 4)


async def test_load_range_of_a_directory_raises_file_not_found(storage, storage_root):
    (storage_root / "jq").mkdir()
    (storage_root / "jq" / "fetch.jq").write_text(".items")
    with pytest.raises(FileNotFoundError, match="Object not found: jq"):
        await storage.load_range("jq", 0, 4)


async def test_load_range_zero_length_opens_no_file(storage, storage_root, monkeypatch):
    from tai42_storage_local.driver import AsyncLocalDriver

    (storage_root / "blob.bin").write_bytes(b"0123456789")

    async def _no_read(*args: object) -> bytes:
        raise AssertionError("read_range must not be called for a zero-length read")

    monkeypatch.setattr(AsyncLocalDriver, "read_range", _no_read)
    assert await storage.load_range("blob.bin", 0, 0) == b""


# --- file/directory id collision (propagated through the backend) -----------


async def test_upload_over_non_empty_directory_conflicts(storage, storage_root):
    from tai42_contract.storage import StoragePathConflictError

    await storage.upload("a/b/c.j2", "c")
    with pytest.raises(StoragePathConflictError) as exc:
        await storage.upload("a/b", "cannot")
    assert exc.value.conflicts == ["a/b/c.j2"]


async def test_delete_then_upload_at_freed_id(storage, storage_root):
    # The reported path: a template id occupied by a directory is freed by deleting
    # its last child, after which an upload at that id succeeds.
    await storage.upload("a/b/c.j2", "c")
    await storage.delete("a/b/c.j2")
    # "a/b" and "a" are pruned once empty, so the id "a/b" is free to hold a file.
    await storage.upload("a/b", "now a file")
    assert await storage.load("a/b") == "now a file"


# --- unconfigured root -------------------------------------------------------

_UNSET_ROOT_MESSAGE = "local storage is not configured: set STORAGE_LOCAL_ROOT_PATH to the storage root directory."


@pytest.fixture
def unconfigured(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """No root configured: the env var is absent, or set empty (the settings layer reads empty as absent)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STORAGE_LOCAL_ROOT_PATH", raising=False)
    storage_settings.cache_clear()  # type: ignore[attr-defined]
    yield LocalStorage()
    storage_settings.cache_clear()  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("load", ("a.txt",)),
        ("list", ()),
        ("upload", ("a.txt", "text")),
        ("delete", ("a.txt",)),
        ("delete_dir", ("dir",)),
        ("list", ("dir",)),
        ("load_bytes", ("a.txt",)),
        ("load_object", ("a.txt",)),
        ("upload_bytes", ("a.bin", b"\x00")),
        ("stat", ("a.png",)),
        ("load_range", ("a.bin", 0, 4)),
    ],
)
async def test_every_call_refuses_until_the_root_is_set(unconfigured, tmp_path, method, args):
    with pytest.raises(RuntimeError) as exc:
        await getattr(unconfigured, method)(*args)
    assert str(exc.value) == _UNSET_ROOT_MESSAGE
    # Nothing is created anywhere: the refusal precedes every filesystem touch.
    assert list(tmp_path.iterdir()) == []


async def test_an_empty_root_reads_as_unset(unconfigured, monkeypatch):
    monkeypatch.setenv("STORAGE_LOCAL_ROOT_PATH", "")
    storage_settings.cache_clear()  # type: ignore[attr-defined]
    assert storage_settings().root_path is None
    with pytest.raises(RuntimeError, match="STORAGE_LOCAL_ROOT_PATH"):
        await unconfigured.list()
