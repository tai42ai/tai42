"""Storage prefix marks across epochs: declared once per epoch, read from the live epoch."""

from __future__ import annotations

import asyncio

import pytest
from tai42_contract.storage import ObjectStat, Storage

from tai42_skeleton.app import instance
from tai42_skeleton.app.epoch import Epoch, building, epoch_being_built
from tai42_skeleton.app.lifecycle import serving_core_access
from tai42_skeleton.backup.sections import _export_storage
from tai42_skeleton.conversations.media_meta import INBOUND_MEDIA_BLOB_PREFIX
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.operations.storage import list_resources
from tai42_skeleton.operations.templates import list_templates
from tai42_skeleton.template import ResourceManager

from .._fakes.listing import keys_under
from ..app._fixtures.reload import reload_with


class _MemoryStorage(Storage):
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = dict(objects)

    async def load(self, path: str) -> str:
        return self.objects[path].decode("utf-8")

    async def list(self, directory: str | None = None) -> list[str]:
        return keys_under(sorted(self.objects), directory)

    async def upload(self, path: str, content: str) -> None:
        self.objects[path] = content.encode("utf-8")

    async def delete(self, path: str) -> None:
        del self.objects[path]

    async def delete_dir(self, path: str) -> None:
        raise FileNotFoundError(path)

    async def stat(self, path: str) -> ObjectStat:
        return ObjectStat(content_type=None, size=len(self.objects[path]))

    async def load_range(self, path: str, start: int, length: int) -> bytes:
        return self.objects[path][start : start + length]


def _media_marks() -> tuple[list[str], list[str]]:
    storage = instance.app.storage
    return sorted(storage.not_backed_up_prefixes()), sorted(storage.unlisted_prefixes())


def test_the_media_prefix_is_marked_once_after_start_and_once_after_each_reload() -> None:
    manifest = Manifest.model_validate({})
    expected = ([INBOUND_MEDIA_BLOB_PREFIX], [INBOUND_MEDIA_BLOB_PREFIX])

    async def run() -> None:
        async with instance.app.app_context(manifest):
            assert _media_marks() == expected
            for _ in range(2):
                await reload_with(instance.app, manifest)
                assert _media_marks() == expected

    asyncio.run(run())


async def test_a_list_or_an_export_during_a_reload_build_reads_the_live_epochs_marks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = instance.app
    provider = _MemoryStorage({f"{INBOUND_MEDIA_BLOB_PREFIX}x": b"m", "notes/b.txt": b"n"})
    monkeypatch.setattr(app._storage_registry, "_provider", provider)
    live_core = app._build_serving_core()
    live_core._prefix_mark_registry.not_backed_up.declare(INBOUND_MEDIA_BLOB_PREFIX)
    live_core._prefix_mark_registry.unlisted.declare(INBOUND_MEDIA_BLOB_PREFIX)
    live_core._resource_manager_cache = ResourceManager(provider, on_evicted=app._fire_template_evicted)
    monkeypatch.setattr(serving_core_access, "current_epoch_or_none", lambda: Epoch(number=0, core=live_core))
    # The core being built has not run its handlers yet: its registry is empty.
    built = Epoch(number=1, core=app._build_serving_core())

    with building(built):
        # The prefix marks are read on the live generation whoever asks, so even a read in the
        # build's own work (which resolves the core under construction) applies the live marks.
        assert epoch_being_built() is built
        assert await list_resources() == {"resources": ["notes/b.txt"]}
        assert await list_templates() == ["notes/b.txt"]
        assert sorted(await _export_storage()) == ["notes/b.txt"]
