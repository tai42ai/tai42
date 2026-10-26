"""The provider reads the resource manager makes: how many, and how much of the store they cover.

A stored object read with its type is one ``load_object``; a storage-id file load reads at
most one byte past the ``FILE_LOADING_MAX_BYTES`` cap; listing or deleting a directory lists
only that directory from the provider. The store is a recording in-memory double.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from tai42_contract.app import tai42_app

from tai42_skeleton.app.instance import app
from tai42_skeleton.storage import StorageRegistry
from tai42_skeleton.template import ResourceManager, file_loading
from tai42_skeleton.template.errors import ContentNotLoadableError

from .._fakes.recording_storage import RecordingStorage

tai42_app.bind(app)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> RecordingStorage:
    """The app's provider: a recording store holding a directory, a sibling and a root object."""
    recording = RecordingStorage(
        {
            "dir/a.j2": (b"A", "text/plain"),
            "dir/sub/b.j2": (b"B", "text/plain"),
            "dirx/c.j2": (b"C", "text/plain"),
            "top.j2": (b"T", "text/plain"),
        }
    )
    registry = StorageRegistry()
    registry.register_storage(lambda: recording)  # type: ignore[arg-type]
    monkeypatch.setattr(app, "_storage_registry", registry)
    monkeypatch.setattr(app, "_resource_manager_cache", None)
    return recording


@pytest.fixture
def manager(store: RecordingStorage) -> ResourceManager:
    return app.storage.resource_manager


async def test_a_stored_read_with_its_type_is_one_provider_read(manager, store) -> None:
    assert await manager.load_stored("dir/a.j2") == (b"A", "text/plain")
    assert store.calls == [("load_object", "dir/a.j2")]


async def test_a_stored_read_without_its_type_reads_only_the_bytes(manager, store) -> None:
    assert await manager.load_stored("dir/a.j2", with_mime=False) == (b"A", None)
    assert store.calls == [("load_bytes", "dir/a.j2")]


async def test_a_storage_id_file_load_reads_one_byte_past_the_cap(manager, store, monkeypatch) -> None:
    monkeypatch.setattr(file_loading, "file_loading_settings", lambda: SimpleNamespace(max_bytes=64))
    store.put("notes/small.txt", b"the quick brown fox\n")
    assert await manager.load_file("notes/small.txt") == "the quick brown fox\n"
    assert store.calls == [("load_range", "notes/small.txt", 0, 65)]


async def test_a_storage_id_file_load_over_the_cap_is_refused_without_a_whole_read(manager, store, monkeypatch) -> None:
    monkeypatch.setattr(file_loading, "file_loading_settings", lambda: SimpleNamespace(max_bytes=64))
    store.put("notes/big.txt", b"x" * 10_000)
    with pytest.raises(ContentNotLoadableError, match="over the 64-byte FILE_LOADING_MAX_BYTES cap"):
        await manager.load_file("notes/big.txt")
    assert store.calls == [("load_range", "notes/big.txt", 0, 65)]


async def test_listing_a_directory_lists_only_that_directory(manager, store) -> None:
    assert sorted(await manager.list_resources("dir")) == ["dir/a.j2", "dir/sub/b.j2"]
    assert store.calls == [("list", "dir")]


async def test_listing_with_no_directory_lists_the_whole_store(manager, store) -> None:
    assert len(await manager.list_resources()) == 4
    assert store.calls == [("list", None)]


async def test_a_directory_delete_lists_only_that_directory(manager, store) -> None:
    await manager.delete_template_dir("dir")
    assert ("list", "dir") in store.calls
    assert ("list", None) not in store.calls
    assert sorted(store.objects) == ["dirx/c.j2", "top.j2"]


@pytest.mark.parametrize("root", ["", "/", ".", "a/.."])
async def test_a_directory_delete_of_the_root_is_refused_before_any_read(manager, store, root: str) -> None:
    with pytest.raises(ValueError, match="Refusing to delete the storage root"):
        await manager.delete_template_dir(root)
    assert store.calls == []
    assert len(store.objects) == 4
