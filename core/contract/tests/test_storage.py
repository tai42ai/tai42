"""Storage-contract behavior tests — the ``ObjectStat`` value type, the abstract
``stat`` / ``load_range`` members, the text-bridging binary defaults on ``Storage``,
the ``storage_module`` field on ``Manifest``, and the ``resource_manager`` facet on
``AppStorage``.

``load_bytes`` / ``upload_bytes`` ship concrete text-bridging defaults — these exercise
that bridge (including the STRICT UTF-8 decode that must raise on non-UTF-8 bytes rather
than corrupt the store). The suite is sync-only, so each async method is driven through
``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import builtins
import inspect
import mimetypes

import pydantic
import pytest

from tai42_contract.app import AppStorage
from tai42_contract.manifest import Manifest
from tai42_contract.storage import ObjectStat, Storage, StoredObject, directory_prefix


class _MemoryTextStorage(Storage):
    """A minimal text-only backend over an in-memory dict, inheriting the binary defaults."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}
        self.range_reads = 0

    async def load(self, path: str) -> str:
        if path not in self._store:
            raise FileNotFoundError(path)
        return self._store[path]

    async def list(self, directory: str | None = None) -> builtins.list[str]:
        if directory is None:
            return sorted(self._store)
        prefix = directory_prefix(directory)
        return sorted(key for key in self._store if key.startswith(prefix))

    async def upload(self, path: str, content: str) -> None:
        self._store[path] = content

    async def delete(self, path: str) -> None:
        self._store.pop(path, None)

    async def delete_dir(self, path: str) -> None:
        prefix = path.rstrip("/") + "/"
        for key in [k for k in self._store if k.startswith(prefix)]:
            del self._store[key]

    async def stat(self, path: str) -> ObjectStat:
        if path not in self._store:
            raise FileNotFoundError(path)
        content_type, _ = mimetypes.guess_type(path)
        return ObjectStat(content_type=content_type, size=len(self._store[path].encode("utf-8")))

    async def load_range(self, path: str, start: int, length: int) -> bytes:
        if length == 0:
            return b""
        self.range_reads += 1
        if path not in self._store:
            raise FileNotFoundError(path)
        return self._store[path].encode("utf-8")[start : start + length]


# -- ObjectStat -----------------------------------------------------------------


def test_object_stat_round_trips_content_type_and_size():
    stat = ObjectStat(content_type="image/png", size=12)
    assert (stat.content_type, stat.size) == ("image/png", 12)
    assert set(ObjectStat.model_fields) == {"content_type", "size"}
    assert ObjectStat.model_validate(stat.model_dump()) == stat


def test_object_stat_accepts_none_content_type():
    assert ObjectStat(content_type=None, size=0).content_type is None


def test_object_stat_requires_size():
    with pytest.raises(pydantic.ValidationError):
        ObjectStat(content_type="text/plain")  # pyright: ignore[reportCallIssue]


def test_object_stat_refuses_a_negative_size():
    with pytest.raises(pydantic.ValidationError):
        ObjectStat(content_type="text/plain", size=-1)


def test_object_stat_is_frozen():
    stat = ObjectStat(content_type="text/plain", size=1)
    with pytest.raises(pydantic.ValidationError):
        stat.content_type = "image/png"  # pyright: ignore[reportAttributeAccessIssue]


# -- Abstract stat / load_range -------------------------------------------------


def test_storage_without_stat_cannot_be_instantiated():
    no_stat = type("_NoStatStorage", (Storage,), _methods_except("stat"))
    assert inspect.isabstract(no_stat)
    assert getattr(no_stat.stat, "__isabstractmethod__", False)
    with pytest.raises(TypeError):
        no_stat()


def test_storage_without_load_range_cannot_be_instantiated():
    no_range = type("_NoRangeStorage", (Storage,), _methods_except("load_range"))
    assert inspect.isabstract(no_range)
    assert getattr(no_range.load_range, "__isabstractmethod__", False)
    with pytest.raises(TypeError):
        no_range()


def _methods_except(name: str) -> dict[str, object]:
    members = ("load", "list", "upload", "delete", "delete_dir", "stat", "load_range")
    return {m: _MemoryTextStorage.__dict__[m] for m in members if m != name}


def test_stat_answers_size_and_content_type_and_raises_on_a_miss():
    store = _MemoryTextStorage()
    asyncio.run(store.upload("cover.txt", "héllo"))
    assert asyncio.run(store.stat("cover.txt")) == ObjectStat(content_type="text/plain", size=6)
    with pytest.raises(FileNotFoundError):
        asyncio.run(store.stat("missing.txt"))


def test_load_range_zero_length_answers_empty_without_a_read():
    store = _MemoryTextStorage()
    asyncio.run(store.upload("a.txt", "abcdef"))
    assert asyncio.run(store.load_range("a.txt", 0, 0)) == b""
    assert store.range_reads == 0


# -- Storage binary text-bridge defaults ----------------------------------------


def test_load_bytes_bridges_through_load():
    store = _MemoryTextStorage()
    asyncio.run(store.upload("greeting.txt", "héllo"))
    assert asyncio.run(store.load_bytes("greeting.txt")) == "héllo".encode()


def test_load_bytes_missing_raises_file_not_found():
    store = _MemoryTextStorage()
    with pytest.raises(FileNotFoundError):
        asyncio.run(store.load_bytes("nope.txt"))


def test_load_object_answers_the_bytes_with_the_stat_content_type():
    store = _MemoryTextStorage()
    asyncio.run(store.upload("greeting.txt", "héllo"))
    assert asyncio.run(store.load_object("greeting.txt")) == StoredObject(
        data="héllo".encode(), content_type="text/plain"
    )


def test_load_object_missing_raises_file_not_found():
    store = _MemoryTextStorage()
    with pytest.raises(FileNotFoundError):
        asyncio.run(store.load_object("nope.txt"))


def test_stored_object_is_frozen():
    stored = StoredObject(data=b"x", content_type=None)
    with pytest.raises(pydantic.ValidationError):
        stored.data = b"y"  # pyright: ignore[reportAttributeAccessIssue]


@pytest.mark.parametrize(("directory", "prefix"), [("a/b", "a/b/"), ("a/b/", "a/b/"), ("/a", "a/")])
def test_directory_prefix_is_the_directory_and_one_slash(directory: str, prefix: str):
    assert directory_prefix(directory) == prefix


@pytest.mark.parametrize("directory", ["", "/", "//", " "])
def test_directory_prefix_refuses_a_blank_directory(directory: str):
    with pytest.raises(ValueError, match="A directory path is required"):
        directory_prefix(directory)


def test_upload_bytes_bridges_through_upload():
    store = _MemoryTextStorage()
    asyncio.run(store.upload_bytes("note.txt", "café".encode()))
    assert asyncio.run(store.load("note.txt")) == "café"


def test_upload_bytes_strict_decode_raises_on_non_utf8():
    store = _MemoryTextStorage()
    # 0xFF is not valid UTF-8 — the default bridge must raise, never silent-replace.
    with pytest.raises(UnicodeDecodeError):
        asyncio.run(store.upload_bytes("bad.bin", b"\xff\xfe"))
    assert asyncio.run(store.list()) == []


# -- Manifest storage module field ----------------------------------------------


def test_manifest_has_storage_module_field():
    manifest = Manifest(storage_module="tai42_storage_local")
    assert manifest.storage_module == "tai42_storage_local"
    assert "storage_module" in Manifest.model_fields


# -- Storage facet --------------------------------------------------------------


def test_app_storage_facet_exposes_resource_manager():
    assert hasattr(AppStorage, "resource_manager")
