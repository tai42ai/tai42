"""Every platform door that writes the template store drops the stale render state of the ids it wrote.

A render remembers the ids storage answered "not found" for (a locale variant, an
``{% include %}`` target, a bare id) and keeps compiled entries by id. Each door below
writes the store the template manager reads, on this worker, through the platform; the
next render must see the write — a remembered-absent id is forgotten and a compiled entry
is dropped — on this worker and, through the ``evict_template`` / ``clear_template_cache``
fleet op the door broadcasts, on every other worker. Driven against a REAL
:class:`ResourceManager` over an in-memory provider, installed on the app the doors read.

The doors: the templates operations (upload, delete, directory delete), the storage
operations (text upload, base64 upload, delete, directory delete), the backup restore of
the templates section, and the fleet op a sibling worker applies.
"""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from types import SimpleNamespace

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.storage import Storage

import tai42_skeleton.operations.storage as storage_ops
import tai42_skeleton.operations.templates as templates_ops
from tai42_skeleton.app import instance
from tai42_skeleton.app.bus import LocalApplyResult, OpOutcome
from tai42_skeleton.backup.registry import BackupRegistry
from tai42_skeleton.backup.sections import register_core_sections
from tai42_skeleton.operations.backup import import_backup
from tai42_skeleton.template import ResourceManager
from tai42_skeleton.template.resource_manager import TemplateNotFoundError

from .._fakes.bus import FakeBus


class _Store(Storage):
    """An in-memory provider holding text and bytes, with directory semantics."""

    def __init__(self, items: dict[str, str]) -> None:
        self.items: dict[str, bytes] = {key: value.encode("utf-8") for key, value in items.items()}

    async def load(self, path: str) -> str:
        return (await self.load_bytes(path)).decode("utf-8")

    async def load_bytes(self, path: str) -> bytes:
        try:
            return self.items[path]
        except KeyError as exc:
            raise FileNotFoundError(path) from exc

    async def list(self) -> list[str]:
        return sorted(self.items)

    async def upload(self, path: str, content: str) -> None:
        self.items[path] = content.encode("utf-8")

    async def upload_bytes(self, path: str, data: bytes, content_type: str | None = None) -> None:
        self.items[path] = data

    async def delete(self, path: str) -> None:
        if path not in self.items:
            raise FileNotFoundError(path)
        del self.items[path]

    async def delete_dir(self, path: str) -> None:
        prefix = path.rstrip("/") + "/"
        doomed = [key for key in self.items if key.startswith(prefix)]
        if not doomed:
            raise FileNotFoundError(path)
        for key in doomed:
            del self.items[key]


def _install(monkeypatch: pytest.MonkeyPatch, items: dict[str, str], bus: FakeBus) -> tuple[ResourceManager, _Store]:
    """Install ``items`` as the app's storage provider and a real manager over it on the app the doors read."""
    store = _Store(items)
    manager = ResourceManager(store, on_evicted=instance.app._fire_template_evicted)
    monkeypatch.setattr(instance.app._storage_registry, "_provider", store)
    monkeypatch.setattr(instance.app, "_resource_manager_cache", manager)
    monkeypatch.setattr(instance.app, "_bus", bus)
    monkeypatch.setattr(tai42_app, "_impl", instance.app)
    return manager, store


async def _templates_upload(_monkeypatch: pytest.MonkeyPatch) -> None:
    await templates_ops.upload_template("welcome@he", "shalom")


async def _storage_upload_text(_monkeypatch: pytest.MonkeyPatch) -> None:
    await storage_ops.upload_resource("welcome@he", content_text="shalom")


async def _storage_upload_base64(_monkeypatch: pytest.MonkeyPatch) -> None:
    await storage_ops.upload_resource("welcome@he", content_base64=base64.b64encode(b"shalom").decode("ascii"))


async def _backup_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = BackupRegistry()
    register_core_sections(registry)
    monkeypatch.setattr(tai42_app, "_impl", SimpleNamespace(backup=registry, storage=instance.app.storage))
    result = await import_backup({"version": 1, "sections": {"templates": {"welcome@he": "shalom"}}}, ["templates"])
    monkeypatch.setattr(tai42_app, "_impl", instance.app)
    assert result.ok, result


_CREATING_DOORS: dict[str, tuple[Callable[[pytest.MonkeyPatch], Awaitable[None]], dict[str, object]]] = {
    "templates upload": (_templates_upload, {"op": "evict_template", "path": "welcome@he"}),
    "storage upload (text)": (_storage_upload_text, {"op": "evict_template", "path": "welcome@he"}),
    "storage upload (base64)": (_storage_upload_base64, {"op": "evict_template", "path": "welcome@he"}),
    "backup restore": (_backup_restore, {"op": "clear_template_cache"}),
}


@pytest.mark.parametrize("door", list(_CREATING_DOORS))
async def test_a_variant_created_through_the_door_is_rendered_next(monkeypatch: pytest.MonkeyPatch, door: str) -> None:
    bus = FakeBus(remotes=["serve-w1"])
    manager, _store = _install(monkeypatch, {"welcome": "default"}, bus)
    assert await manager.render_by_id("welcome", locale="he-IL") == "default"  # the variants are remembered absent

    write, fleet_op = _CREATING_DOORS[door]
    await write(monkeypatch)

    assert await manager.render_by_id("welcome", locale="he-IL") == "shalom"
    # Every other worker is told to drop the same state.
    assert [call[0] for call in bus.publish_calls] == [fleet_op]


@pytest.mark.parametrize("door", ["templates upload", "storage upload (text)", "storage upload (base64)"])
async def test_an_include_target_created_through_the_door_is_rendered_next(
    monkeypatch: pytest.MonkeyPatch, door: str
) -> None:
    manager, store = _install(monkeypatch, {"page": "[{% include 'welcome@he' ignore missing %}]"}, FakeBus())
    assert await manager.render_by_id("page") == "[]"  # the include target is remembered absent
    manager.evict_compiled("page")  # recompile the page so only the include target's state decides

    await _CREATING_DOORS[door][0](monkeypatch)

    assert store.items["welcome@he"] == b"shalom"
    assert await manager.render_by_id("page") == "[shalom]"


async def test_a_bare_id_created_through_the_storage_door_after_a_failed_render_is_rendered_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _store = _install(monkeypatch, {}, FakeBus())
    with pytest.raises(TemplateNotFoundError):
        await manager.render_by_id("welcome@he")

    await storage_ops.upload_resource("welcome@he", content_text="shalom")

    assert await manager.render_by_id("welcome@he") == "shalom"


async def _templates_delete(_monkeypatch: pytest.MonkeyPatch) -> None:
    await templates_ops.delete_template("dir/welcome")


async def _storage_delete(_monkeypatch: pytest.MonkeyPatch) -> None:
    await storage_ops.delete_resource("dir/welcome")


async def _templates_delete_dir(_monkeypatch: pytest.MonkeyPatch) -> None:
    await templates_ops.delete_template_dir("dir")


async def _storage_delete_dir(_monkeypatch: pytest.MonkeyPatch) -> None:
    await storage_ops.delete_dir("dir")


_DELETING_DOORS: dict[str, tuple[Callable[[pytest.MonkeyPatch], Awaitable[None]], dict[str, object]]] = {
    "templates delete": (_templates_delete, {"op": "evict_template", "path": "dir/welcome"}),
    "storage delete": (_storage_delete, {"op": "evict_template", "path": "dir/welcome"}),
    "templates directory delete": (_templates_delete_dir, {"op": "evict_template", "path": "dir", "prefix": True}),
    "storage directory delete": (_storage_delete_dir, {"op": "evict_template", "path": "dir", "prefix": True}),
}


@pytest.mark.parametrize("door", list(_DELETING_DOORS))
async def test_a_template_deleted_through_the_door_is_not_rendered_next(
    monkeypatch: pytest.MonkeyPatch, door: str
) -> None:
    bus = FakeBus(remotes=["serve-w1"])
    manager, _store = _install(monkeypatch, {"dir/welcome": "hello"}, bus)
    assert await manager.render_by_id("dir/welcome") == "hello"  # compiled and cached

    delete, fleet_op = _DELETING_DOORS[door]
    await delete(monkeypatch)

    with pytest.raises(TemplateNotFoundError):
        await manager.render_by_id("dir/welcome")
    assert [call[0] for call in bus.publish_calls] == [fleet_op]


async def test_a_storage_door_write_that_fails_publishes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = FakeBus(remotes=["serve-w1"])
    _install(monkeypatch, {}, bus)

    with pytest.raises(storage_ops.NotFoundError):
        await storage_ops.delete_resource("missing")
    with pytest.raises(storage_ops.NotFoundError):
        await storage_ops.delete_dir("missing")

    assert bus.publish_calls == []


async def test_the_storage_upload_door_applies_locally_then_broadcasts(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = FakeBus(remotes=["serve-w1"])
    _install(monkeypatch, {}, bus)

    assert await storage_ops.upload_resource("welcome@he", content_text="shalom") == {
        "id": "welcome@he",
        "stored": True,
    }

    assert bus.publish_calls == [
        (
            {"op": "evict_template", "path": "welcome@he"},
            None,
            LocalApplyResult(outcome=OpOutcome.applied, payload=None),
        )
    ]


async def test_a_sibling_applying_the_fleet_op_renders_the_created_variant(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, store = _install(monkeypatch, {"welcome": "default"}, FakeBus())
    assert await manager.render_by_id("welcome", locale="he") == "default"

    # Another worker wrote the store through a door and broadcast the op this worker applies.
    store.items["welcome@he"] = b"shalom"
    await instance.app._dispatch_bus_op({"op": "evict_template", "path": "welcome@he"})

    assert await manager.render_by_id("welcome", locale="he") == "shalom"
