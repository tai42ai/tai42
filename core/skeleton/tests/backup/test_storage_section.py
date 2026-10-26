"""The ``storage`` backup section carries every object of the content store byte for byte.

An object whose bytes are UTF-8 text travels as ``content_text``, any other object as
``content_base64``, each with the ``content_type`` the store reports. The restore writes
every entry back as bytes with that content type. A malformed entry, an entry a text-only
store cannot hold, and a path colliding with a directory are per-path rejections recorded
in the report; the rest of the section still restores. A prefix marked not backed up is
neither exported nor restored; the unlisted mark plays no part.
"""

from __future__ import annotations

import base64
import builtins
import mimetypes
from types import SimpleNamespace

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.storage import ObjectStat, Storage, StoragePathConflictError
from tai42_contract.template import TemplateEviction

from tai42_skeleton.backup import sections
from tai42_skeleton.backup.registry import import_mode
from tai42_skeleton.template import ResourceManager

from .._fakes.listing import keys_under
from .._fakes.recording_storage import RecordingStorage

_PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"


class _MemoryStorage(Storage):
    """A binary-native store keeping each object's bytes and the content type it was written with."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.types: dict[str, str | None] = {}

    async def load(self, path: str) -> str:
        return (await self.load_bytes(path)).decode("utf-8")

    async def load_bytes(self, path: str) -> bytes:
        try:
            return self.objects[path]
        except KeyError:
            raise FileNotFoundError(path) from None

    async def list(self, directory: str | None = None) -> builtins.list[str]:
        return keys_under(sorted(self.objects), directory)

    async def upload(self, path: str, content: str) -> None:
        await self.upload_bytes(path, content.encode("utf-8"), "text/plain; charset=utf-8")

    async def upload_bytes(self, path: str, data: bytes, content_type: str | None = None) -> None:
        self.objects[path] = data
        self.types[path] = content_type

    async def stat(self, path: str) -> ObjectStat:
        if path not in self.objects:
            raise FileNotFoundError(path)
        return ObjectStat(content_type=self.types[path], size=len(self.objects[path]))

    async def load_range(self, path: str, start: int, length: int) -> bytes:
        return (await self.load_bytes(path))[start : start + length]

    async def delete(self, path: str) -> None:
        del self.objects[path]
        del self.types[path]

    async def delete_dir(self, path: str) -> None:
        for key in [k for k in self.objects if k.startswith(path + "/")]:
            await self.delete(key)


class _TextOnlyStorage(Storage):
    """A store holding text only: it implements the text methods and inherits the contract's bytes bridges."""

    def __init__(self) -> None:
        self.texts: dict[str, str] = {}

    async def load(self, path: str) -> str:
        try:
            return self.texts[path]
        except KeyError:
            raise FileNotFoundError(path) from None

    async def list(self, directory: str | None = None) -> builtins.list[str]:
        return keys_under(sorted(self.texts), directory)

    async def upload(self, path: str, content: str) -> None:
        self.texts[path] = content

    async def delete(self, path: str) -> None:
        del self.texts[path]

    async def delete_dir(self, path: str) -> None:
        for key in [k for k in self.texts if k.startswith(path + "/")]:
            del self.texts[key]

    async def stat(self, path: str) -> ObjectStat:
        content_type, _ = mimetypes.guess_type(path)
        return ObjectStat(content_type=content_type, size=len(await self.load_bytes(path)))

    async def load_range(self, path: str, start: int, length: int) -> bytes:
        return (await self.load_bytes(path))[start : start + length]


class _FailingStorage(_MemoryStorage):
    """A store whose reads fail the way an unreachable backend does."""

    async def load_bytes(self, path: str) -> bytes:
        raise ConnectionError(f"store unreachable reading {path!r}")


def _bound(manager: object, *, not_backed_up: frozenset[str] = frozenset(), unlisted: frozenset[str] = frozenset()):
    storage = SimpleNamespace(
        resource_manager=manager,
        not_backed_up_prefixes=lambda: not_backed_up,
        unlisted_prefixes=lambda: unlisted,
    )
    return tai42_app.bound(SimpleNamespace(storage=storage))


def _manager(store: Storage, evictions: list[TemplateEviction] | None = None) -> ResourceManager:
    sink = evictions if evictions is not None else []
    return ResourceManager(store, on_evicted=sink.append)


async def test_export_reads_each_object_with_its_type_in_one_provider_read() -> None:
    store = RecordingStorage({"greeting.j2": (b"hello", "text/plain"), "assets/logo": (_PNG, "image/png")})

    with _bound(_manager(store)):
        await sections._export_storage()

    reads = [call for call in store.calls if call[0] != "list"]
    assert sorted(reads) == [("load_object", "assets/logo"), ("load_object", "greeting.j2")]


async def test_export_carries_text_as_text_and_other_bytes_as_base64_with_the_stored_type() -> None:
    store = _MemoryStorage()
    await store.upload_bytes("greeting.j2", b"hello {{ name }}", "text/plain; charset=utf-8")
    # No suffix to infer a type from: the exported type is the one the store reports.
    await store.upload_bytes("assets/logo", _PNG, "image/png")

    with _bound(_manager(store)):
        payload = await sections._export_storage()

    assert payload == {
        "greeting.j2": {"content_text": "hello {{ name }}", "content_type": "text/plain; charset=utf-8"},
        "assets/logo": {"content_base64": base64.b64encode(_PNG).decode("ascii"), "content_type": "image/png"},
    }


async def test_export_reads_a_stored_id_that_looks_like_a_url_scheme_as_a_stored_object() -> None:
    store = _MemoryStorage()
    await store.upload_bytes("notes:2026.txt", b"plain", "text/plain")

    with _bound(_manager(store)):
        payload = await sections._export_storage()

    assert payload == {"notes:2026.txt": {"content_text": "plain", "content_type": "text/plain"}}


async def test_export_storage_failure_propagates() -> None:
    store = _FailingStorage()
    await store.upload_bytes("greeting.j2", b"hello", "text/plain")

    with _bound(_manager(store)), pytest.raises(ConnectionError, match="store unreachable"):
        await sections._export_storage()


async def test_export_then_restore_reproduces_every_object_byte_equal_with_its_type() -> None:
    source = _MemoryStorage()
    await source.upload_bytes("greeting.j2", "hëllo {{ name }}".encode(), "text/x-jinja")
    await source.upload_bytes("assets/logo", _PNG, "image/png")
    with _bound(_manager(source)):
        payload = await sections._export_storage()

    target = _MemoryStorage()
    evictions: list[TemplateEviction] = []
    with _bound(_manager(target, evictions)), import_mode("overwrite"):
        report = await sections._import_storage(payload)

    assert (report.created, report.updated, report.skipped, report.errors) == (2, 0, 0, [])
    assert target.objects == source.objects
    assert target.types == source.types
    # Every write evicts that path's compiled entry.
    assert [e.path for e in evictions] == ["assets/logo", "greeting.j2"]


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        ("plain text", "entry must be an object carrying content_text or content_base64"),
        ({"content_type": "text/plain"}, "entry must carry exactly one of content_text or content_base64"),
        (
            {"content_text": "a", "content_base64": "YQ==", "content_type": None},
            "entry must carry exactly one of content_text or content_base64",
        ),
        ({"content_text": "a", "stray": 1}, "stray: Extra inputs are not permitted"),
        ({"content_text": 3}, "content_text: Input should be a valid string"),
        ({"content_base64": "not base64!", "content_type": None}, "content_base64 is not valid base64"),
    ],
)
async def test_malformed_entry_is_a_per_path_rejection_and_the_rest_restores(entry: object, reason: str) -> None:
    target = _MemoryStorage()
    payload = {"broken.bin": entry, "clean.j2": {"content_text": "fine", "content_type": "text/plain"}}

    with _bound(_manager(target)), import_mode("overwrite"):
        report = await sections._import_storage(payload)

    assert (report.created, report.skipped) == (1, 1)
    assert len(report.errors) == 1
    assert report.errors[0].startswith("template 'broken.bin': ")
    assert reason in report.errors[0]
    assert target.objects == {"clean.j2": b"fine"}


async def test_binary_entry_onto_a_text_only_store_is_a_per_path_rejection() -> None:
    target = _TextOnlyStorage()
    payload = {
        "assets/logo": {"content_base64": base64.b64encode(_PNG).decode("ascii"), "content_type": "image/png"},
        "greeting.j2": {"content_text": "hello", "content_type": "text/plain"},
    }

    with _bound(_manager(target)), import_mode("overwrite"):
        report = await sections._import_storage(payload)

    assert (report.created, report.skipped) == (1, 1)
    assert report.errors == [
        "template 'assets/logo': the storage provider holds text only and cannot store this object"
    ]
    assert target.texts == {"greeting.j2": "hello"}


class _ConflictingRenderer:
    """A resource manager whose write of the ``conflict`` id raises the store's
    typed conflict; every other id is stored."""

    def __init__(self, conflict_id: str) -> None:
        self.conflict_id = conflict_id
        self.stored: dict[str, bytes] = {}

    async def list_resources(self) -> list[str]:
        return []

    async def upload_bytes(self, path: str, data: bytes, content_type: str | None = None) -> None:
        if path == self.conflict_id:
            raise StoragePathConflictError(path, [f"{path}/child.j2"])
        self.stored[path] = data


async def test_restore_conflict_is_recorded_and_restore_continues() -> None:
    renderer = _ConflictingRenderer("a/b")
    payload = {
        "a/b": {"content_text": "would clobber a directory", "content_type": None},
        "clean.j2": {"content_text": "fine", "content_type": None},
    }

    with _bound(renderer), import_mode("overwrite"):
        report = await sections._import_storage(payload)

    # The colliding id is a per-path skip with a loud error; the clean id still restores.
    assert report.skipped == 1
    assert report.created == 1
    assert any("a/b" in e and "a/b/child.j2" in e for e in report.errors)
    assert renderer.stored == {"clean.j2": b"fine"}


class _CountingStorage(_MemoryStorage):
    """A memory store counting every read of each object."""

    def __init__(self) -> None:
        super().__init__()
        self.reads: list[str] = []

    async def load_bytes(self, path: str) -> bytes:
        self.reads.append(path)
        return await super().load_bytes(path)


_MEDIA = frozenset({"inbound-media/"})


def _marked_refusal(path: str, prefix: str) -> str:
    return f"template {path!r}: path is under {prefix!r}, which is marked not backed up"


async def test_export_skips_a_not_backed_up_prefix_without_reading_it() -> None:
    store = _CountingStorage()
    await store.upload_bytes("inbound-media/a", _PNG, "image/png")
    await store.upload_bytes("notes/b.txt", b"kept", "text/plain")
    store.reads.clear()

    with _bound(_manager(store), not_backed_up=_MEDIA):
        payload = await sections._export_storage()

    assert payload == {"notes/b.txt": {"content_text": "kept", "content_type": "text/plain"}}
    assert "inbound-media/a" not in store.reads


async def test_restore_refuses_a_row_under_a_not_backed_up_prefix_and_restores_the_rest() -> None:
    target = _MemoryStorage()
    await target.upload_bytes("inbound-media/old", b"untouched", "image/png")
    payload = {
        "inbound-media/x": {"content_base64": base64.b64encode(_PNG).decode("ascii"), "content_type": "image/png"},
        "notes/b.txt": {"content_text": "fine", "content_type": "text/plain"},
    }

    with _bound(_manager(target), not_backed_up=_MEDIA), import_mode("overwrite"):
        report = await sections._import_storage(payload)

    assert report.skipped == 1
    assert report.created == 1
    assert report.errors == [_marked_refusal("inbound-media/x", "inbound-media/")]
    assert target.objects == {"inbound-media/old": b"untouched", "notes/b.txt": b"fine"}


async def test_restore_refuses_a_marked_row_before_reading_its_value() -> None:
    target = _MemoryStorage()
    with _bound(_manager(target), not_backed_up=_MEDIA), import_mode("overwrite"):
        report = await sections._import_storage({"inbound-media/x": "not an entry"})

    assert report.errors == [_marked_refusal("inbound-media/x", "inbound-media/")]
    assert target.objects == {}


@pytest.mark.parametrize("path", ["./inbound-media/x", "a/../inbound-media/x"])
async def test_a_non_canonical_row_naming_a_marked_prefix_is_refused_by_the_key_guard(path: str) -> None:
    target = _MemoryStorage()
    payload = {path: {"content_text": "sneaky", "content_type": None}}

    with _bound(_manager(target), not_backed_up=_MEDIA), import_mode("overwrite"):
        report = await sections._import_storage(payload)

    message = f"non-canonical store key {path!r}: a key is '/'-separated names with no empty, '.' or '..' segment"
    assert report.errors == [f"template {path!r}: {message}"]
    assert report.skipped == 1
    assert target.objects == {}


async def test_a_plugin_declared_prefix_is_left_out_of_export_and_restore() -> None:
    source = _MemoryStorage()
    await source.upload_bytes("plugin-cache/blob", b"volatile", "text/plain")
    await source.upload_bytes("notes/b.txt", b"kept", "text/plain")
    marks = frozenset({"plugin-cache/"})

    with _bound(_manager(source), not_backed_up=marks):
        payload = await sections._export_storage()
    assert list(payload) == ["notes/b.txt"]

    target = _MemoryStorage()
    rows = {**payload, "plugin-cache/blob": {"content_text": "volatile", "content_type": None}}
    with _bound(_manager(target), not_backed_up=marks), import_mode("overwrite"):
        report = await sections._import_storage(rows)
    assert report.errors == [_marked_refusal("plugin-cache/blob", "plugin-cache/")]
    assert target.objects == {"notes/b.txt": b"kept"}


async def test_a_prefix_marked_only_unlisted_is_exported_and_restored() -> None:
    source = _MemoryStorage()
    await source.upload_bytes("hidden/a.txt", b"hidden", "text/plain")

    with _bound(_manager(source), unlisted=frozenset({"hidden/"})):
        payload = await sections._export_storage()
    assert payload == {"hidden/a.txt": {"content_text": "hidden", "content_type": "text/plain"}}

    target = _MemoryStorage()
    with _bound(_manager(target), unlisted=frozenset({"hidden/"})), import_mode("overwrite"):
        report = await sections._import_storage(payload)
    assert (report.created, report.errors) == (1, [])
    assert target.objects == {"hidden/a.txt": b"hidden"}
