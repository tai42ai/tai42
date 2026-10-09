"""The template store's eviction signal and its reads recorder, through a test-only consumer.

Every seam that drops cached template content fires one ``TemplateEviction`` to the handlers
registered through ``app.storage.on_template_evicted``; ``app.storage.template_reads()`` records
the id of every stored template a render resolves or probes.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.storage import Storage
from tai42_contract.template import TemplatedText, TemplateEviction

from tai42_skeleton.app.instance import app
from tai42_skeleton.storage import StorageRegistry
from tai42_skeleton.template import ResourceManager
from tai42_skeleton.template import resource_manager as rm_mod
from tai42_skeleton.template.settings import TemplateCacheSettings

tai42_app.bind(app)


class _Store(Storage):
    def __init__(self, items: dict[str, str] | None = None) -> None:
        self.items: dict[str, str] = dict(items or {})

    async def load(self, path: str) -> str:
        try:
            return self.items[path]
        except KeyError as exc:
            raise FileNotFoundError(path) from exc

    async def list(self) -> list[str]:
        return sorted(self.items)

    async def upload(self, path: str, content: str) -> None:
        self.items[path] = content

    async def delete(self, path: str) -> None:
        self.items.pop(path, None)

    async def delete_dir(self, path: str) -> None:
        prefix = path.rstrip("/") + "/"
        for key in [k for k in self.items if k.startswith(prefix)]:
            del self.items[key]


@pytest.fixture
def evictions() -> Iterator[list[TemplateEviction]]:
    seen: list[TemplateEviction] = []

    @app.storage.on_template_evicted
    def _consumer(eviction: TemplateEviction) -> None:
        seen.append(eviction)

    yield seen
    app._template_evicted_handlers.pop(f"{_consumer.__module__}.{_consumer.__qualname__}")


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> ResourceManager:
    """The app's own resource manager, built by the app over an in-memory store."""
    monkeypatch.setattr(rm_mod, "template_cache_settings", lambda: TemplateCacheSettings(ttl=300, max_size=8))
    registry = StorageRegistry()

    @registry.register_storage
    class _Provider(_Store):
        def __init__(self) -> None:
            super().__init__(
                {
                    "a.j2": "A {{ x }}",
                    "dir/b.j2": "B",
                    "greet": "hi",
                    "greet@he": "shalom",
                    "base.j2": "<{% block body %}{% endblock %}>",
                    "child.j2": "{% extends 'base.j2' %}{% block body %}c{% endblock %}",
                    "macros.j2": "{% macro m() %}M{% endmacro %}",
                    "uses_import.j2": "{% import 'macros.j2' as mm %}{{ mm.m() }}",
                    "outer.j2": "[{% include 'inner.j2' %}]",
                    "inner.j2": "({% include 'leaf.j2' %})",
                    "leaf.j2": "leaf",
                    "pick.j2": "{% include ['missing.j2', 'leaf.j2'] %}",
                }
            )

    monkeypatch.setattr(app, "_storage_registry", registry)
    monkeypatch.setattr(app, "_resource_manager_cache", None)
    return app.storage.resource_manager


async def test_an_upload_fires_one_eviction_for_its_id(manager, evictions):
    await manager.upload_template("a.j2", "A2")
    assert evictions == [TemplateEviction(path="a.j2")]


async def test_a_delete_fires_one_eviction_for_its_id(manager, evictions):
    await manager.delete_template("a.j2")
    assert evictions == [TemplateEviction(path="a.j2")]


async def test_a_directory_delete_fires_one_prefix_eviction(manager, evictions):
    await manager.delete_template_dir("dir/")
    assert evictions == [TemplateEviction(path="dir", prefix=True)]


async def test_a_whole_clear_fires_one_eviction_for_every_template(manager, evictions):
    manager.clear_cache()
    assert evictions == [TemplateEviction(path=None)]


@pytest.mark.parametrize(
    ("op", "expected"),
    [
        ({"op": "evict_template", "path": "a.j2"}, TemplateEviction(path="a.j2")),
        ({"op": "evict_template", "path": "dir", "prefix": True}, TemplateEviction(path="dir", prefix=True)),
        ({"op": "clear_template_cache"}, TemplateEviction(path=None)),
    ],
)
async def test_the_bus_apply_fires_one_eviction(manager, evictions, op, expected):
    app._apply_template_op(op)
    assert evictions == [expected]


async def test_a_handler_sees_the_dropped_cache(manager, evictions):
    assert await manager.render_by_id("a.j2", {"x": 1}) == "A 1"
    sizes: list[int] = []

    @app.storage.on_template_evicted
    def _reads_cache(eviction: TemplateEviction) -> None:
        sizes.append(manager.get_cache_info().currsize)

    try:
        manager.evict_compiled("a.j2")
    finally:
        app._template_evicted_handlers.pop(f"{_reads_cache.__module__}.{_reads_cache.__qualname__}")
    assert sizes == [0]


async def test_a_raising_handler_propagates_to_the_dropping_call(manager):
    @app.storage.on_template_evicted
    def _fails(eviction: TemplateEviction) -> None:
        raise RuntimeError("consumer refused")

    try:
        with pytest.raises(RuntimeError, match="consumer refused"):
            await manager.upload_template("a.j2", "A3")
    finally:
        app._template_evicted_handlers.pop(f"{_fails.__module__}.{_fails.__qualname__}")


def test_a_coroutine_handler_is_refused_at_registration():
    async def _async_consumer(eviction: TemplateEviction) -> None:  # pragma: no cover - never registered
        return None

    with pytest.raises(TypeError, match="synchronous"):
        app.storage.on_template_evicted(_async_consumer)


async def test_a_by_id_render_records_its_whole_locale_chain(manager):
    with app.storage.template_reads() as reads:
        assert await manager.render_by_id("greet", locale="he-IL") == "shalom"
    assert reads == {"greet@he-IL", "greet@he", "greet"}


async def test_a_by_id_render_without_a_locale_records_its_id(manager):
    with app.storage.template_reads() as reads:
        await manager.render_templated_text(TemplatedText(id="a.j2", kwargs={"x": 1}))
    assert reads == {"a.j2"}


@pytest.mark.parametrize(
    ("template_id", "expected"),
    [
        ("child.j2", {"child.j2", "base.j2"}),
        ("uses_import.j2", {"uses_import.j2", "macros.j2"}),
        ("outer.j2", {"outer.j2", "inner.j2", "leaf.j2"}),
        ("pick.j2", {"pick.j2", "missing.j2", "leaf.j2"}),
    ],
    ids=["extends", "import", "nested_include", "select_template"],
)
async def test_a_render_records_every_template_it_resolves(manager, template_id, expected):
    with app.storage.template_reads() as reads:
        await manager.render_by_id(template_id)
    assert reads == expected


async def test_an_inline_render_records_its_includes(manager):
    with app.storage.template_reads() as reads:
        assert await manager.render_templated_text(TemplatedText(content="x{% include 'leaf.j2' %}")) == "xleaf"
    assert reads == {"leaf.j2"}


async def test_nested_blocks_both_record(manager):
    with app.storage.template_reads() as outer:
        await manager.render_by_id("a.j2", {"x": 1})
        with app.storage.template_reads() as inner:
            await manager.render_by_id("outer.j2")
    assert inner == {"outer.j2", "inner.j2", "leaf.j2"}
    assert outer == {"a.j2", "outer.j2", "inner.j2", "leaf.j2"}


async def test_a_render_outside_a_block_records_nothing(manager):
    await manager.render_by_id("outer.j2")
    with app.storage.template_reads() as reads:
        pass
    assert reads == set()
