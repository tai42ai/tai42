"""Storage-contract behavior tests — the ``ObjectStat`` value type, the
text-bridging binary defaults on ``Storage``, the ``storage_module`` field on
``Manifest``, and the ``resource_manager`` facet on ``AppStorage``.

The binary methods (``load_bytes`` / ``upload_bytes`` / ``stat``) ship concrete
text-bridging defaults, so a text-only backend satisfies the whole contract with
zero new code — these exercise that bridge (including the STRICT UTF-8 decode that
must raise on non-UTF-8 bytes rather than corrupt the store). The suite is
sync-only, so each async method is driven through ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import builtins

import pydantic
import pytest

from tai42_contract.app import AppStorage
from tai42_contract.manifest import Manifest
from tai42_contract.storage import ObjectStat, Storage


class _MemoryTextStorage(Storage):
    """A minimal text-only backend — implements only the five abstract text
    methods over an in-memory dict, inheriting the binary/media defaults."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def load(self, path: str) -> str:
        if path not in self._store:
            raise FileNotFoundError(path)
        return self._store[path]

    async def list(self) -> builtins.list[str]:
        return sorted(self._store)

    async def upload(self, path: str, content: str) -> None:
        self._store[path] = content

    async def delete(self, path: str) -> None:
        self._store.pop(path, None)

    async def delete_dir(self, path: str) -> None:
        prefix = path.rstrip("/") + "/"
        for key in [k for k in self._store if k.startswith(prefix)]:
            del self._store[key]


# -- ObjectStat -----------------------------------------------------------------


def test_object_stat_round_trips_and_only_content_type():
    stat = ObjectStat(content_type="image/png")
    assert stat.content_type == "image/png"
    assert set(ObjectStat.model_fields) == {"content_type"}
    assert ObjectStat.model_validate(stat.model_dump()) == stat


def test_object_stat_accepts_none():
    assert ObjectStat(content_type=None).content_type is None


def test_object_stat_is_frozen():
    stat = ObjectStat(content_type="text/plain")
    with pytest.raises(pydantic.ValidationError):
        stat.content_type = "image/png"  # pyright: ignore[reportAttributeAccessIssue]


# -- Storage binary text-bridge defaults ----------------------------------------


def test_load_bytes_bridges_through_load():
    store = _MemoryTextStorage()
    asyncio.run(store.upload("greeting.txt", "héllo"))
    assert asyncio.run(store.load_bytes("greeting.txt")) == "héllo".encode()


def test_load_bytes_missing_raises_file_not_found():
    store = _MemoryTextStorage()
    with pytest.raises(FileNotFoundError):
        asyncio.run(store.load_bytes("nope.txt"))


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


def test_stat_infers_content_type_from_path():
    store = _MemoryTextStorage()
    stat = asyncio.run(store.stat("cover.png"))
    assert isinstance(stat, ObjectStat)
    assert stat.content_type == "image/png"


def test_stat_unguessable_path_yields_none():
    store = _MemoryTextStorage()
    assert asyncio.run(store.stat("mystery")).content_type is None


# -- Manifest storage module field ----------------------------------------------


def test_manifest_has_storage_module_field():
    manifest = Manifest(storage_module="tai42_storage_local")
    assert manifest.storage_module == "tai42_storage_local"
    assert "storage_module" in Manifest.model_fields


# -- Storage facet --------------------------------------------------------------


def test_app_storage_facet_exposes_resource_manager():
    assert hasattr(AppStorage, "resource_manager")
