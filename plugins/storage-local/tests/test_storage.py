"""Full-surface tests for ``LocalStorage`` over a temp storage root."""

from __future__ import annotations

import os

import pytest
from tai42_contract.storage import ObjectStat, Storage

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


async def test_load_bytes_missing_raises_file_not_found(storage):
    with pytest.raises(FileNotFoundError, match="Object not found"):
        await storage.load_bytes("nope.bin")


# --- stat (inherited mimetypes default) ------------------------------------


async def test_stat_infers_mime_from_suffix(storage):
    stat = await storage.stat("photo.png")
    assert isinstance(stat, ObjectStat)
    assert stat.content_type == "image/png"


async def test_stat_unknown_suffix_returns_none(storage):
    stat = await storage.stat("data.unknownext")
    assert stat.content_type is None


async def test_stat_does_not_verify_existence(storage):
    # The default stat answers from the path string, not the filesystem.
    stat = await storage.stat("never/written.mp3")
    assert stat.content_type == "audio/mpeg"
