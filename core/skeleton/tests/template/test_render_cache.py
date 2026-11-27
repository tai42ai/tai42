"""The render cache, through its resource manager: the compiled-template cache enabled vs disabled,
its controls, what a cached template holds, the compile-once inline cache, the evict seams that
invalidate it, its own TTL/LRU evictions, and the engine's ``{% include %}``/``{% extends %}``
dependency cache freed by every eviction. All storage is an in-memory ``Storage`` double."""

from __future__ import annotations

import asyncio
import contextvars
import gc
import weakref

import pytest
from tai42_contract.template import TemplatedText

from tai42_skeleton.template import resource_manager as rm_mod
from tai42_skeleton.template.settings import TemplateCacheSettings

from ._manager_fixtures import _manager

# --- construction: cache enabled vs disabled --------------------------------


def test_cache_enabled_exposes_cache_info_and_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default (positive ttl + size) caches compiled templates: a second render reads nothing,
    info reports the live size and bound, and clear empties it."""
    monkeypatch.setattr(rm_mod, "template_cache_settings", lambda: TemplateCacheSettings(ttl=300, max_size=8))
    manager, store = _manager({"a.j2": "{{ x }}"})

    assert manager.get_cache_info() == (0, 8)

    async def run() -> None:
        await manager.render_by_id("a.j2", {"x": 1})  # miss -> populate
        assert await manager.render_by_id("a.j2", {"x": 2}) == "2"  # hit
        assert store.loads == ["a.j2"]
        assert manager.get_cache_info() == (1, 8)
        manager.clear_cache()
        assert manager.get_cache_info() == (0, 8)

    asyncio.run(run())


async def test_inline_template_is_compiled_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """An inline (content) template is compiled once and memoized by content, so
    a hook condition/expr rendered on every fire is not re-parsed each time."""
    monkeypatch.setattr(rm_mod, "template_cache_settings", lambda: TemplateCacheSettings(ttl=300, max_size=8))
    manager, _ = _manager()

    first = manager._render_cache.inline("Hi {{ x }}")
    second = manager._render_cache.inline("Hi {{ x }}")
    assert first is second  # same content -> one compiled template, reused

    # Rendering still works through the memoized inline template.
    assert await manager.render_templated_text(TemplatedText(content="Hi {{ x }}", kwargs={"x": 1})) == "Hi 1"


@pytest.mark.parametrize(
    "settings",
    [
        TemplateCacheSettings(ttl=0, max_size=8),
        TemplateCacheSettings(ttl=300, max_size=0),
    ],
)
def test_cache_disabled_has_no_cache_controls(monkeypatch: pytest.MonkeyPatch, settings: TemplateCacheSettings) -> None:
    """ttl=0 or max_size=0 bypasses the cache wrapper entirely, so the cache
    controls are inert (no info, clear is a no-op) but rendering still works."""
    monkeypatch.setattr(rm_mod, "template_cache_settings", lambda: settings)
    manager, _ = _manager({"a.j2": "{{ x }}"})

    assert manager.get_cache_info() is None
    manager.clear_cache()  # no-op, must not raise

    assert asyncio.run(manager.render_by_id("a.j2", {"x": 5})) == "5"


# --- what a cached template holds ---------------------------------------------


_render_scope: contextvars.ContextVar[object] = contextvars.ContextVar("render_scope")


class _Scope:
    """A value a caller binds in its context while it renders."""


async def test_a_cached_template_keeps_nothing_of_the_loading_task(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rm_mod, "template_cache_settings", lambda: TemplateCacheSettings(ttl=300, max_size=8))
    manager, store = _manager({"a.j2": "{{ x }}"})
    scope = _Scope()
    held = weakref.ref(scope)

    async def render_in_scope(value: _Scope) -> str:
        _render_scope.set(value)
        return await manager.render_by_id("a.j2", {"x": 1})

    assert await asyncio.create_task(render_in_scope(scope)) == "1"
    del scope
    await asyncio.sleep(0)
    gc.collect()

    assert held() is None
    assert _compiled_size(manager) == 1
    assert await manager.render_by_id("a.j2", {"x": 2}) == "2"
    assert store.loads == ["a.j2"]


# --- the public evict seams ---------------------------------------------------


async def test_evict_compiled_public_seam_drops_one_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # The public seam the fleet ``evict_template`` op applies on a sibling worker: it
    # drops one compiled key and is idempotent on an already-absent key.
    monkeypatch.setattr(rm_mod, "template_cache_settings", lambda: TemplateCacheSettings(ttl=300, max_size=8))
    manager, _ = _manager({"m.j2": "{{ x }}"})
    await manager.render_by_id("m.j2", {"x": 1})
    assert _compiled_size(manager) == 1

    manager.evict_compiled("m.j2")
    assert _compiled_size(manager) == 0
    manager.evict_compiled("m.j2")  # idempotent: no-op, must not raise
    manager.evict_compiled("never-cached.j2")  # unknown key: no-op


async def test_evict_dir_public_seam_drops_under_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    # The directory counterpart the fleet op applies for a dir delete: prefix semantics
    # (``dir`` -> ``dir/``), dropping only compilations under it.
    monkeypatch.setattr(rm_mod, "template_cache_settings", lambda: TemplateCacheSettings(ttl=300, max_size=8))
    manager, _ = _manager({"dir/a.j2": "{{ x }}", "other.j2": "{{ y }}"})
    await manager.render_by_id("dir/a.j2", {"x": 1})
    await manager.render_by_id("other.j2", {"y": 2})
    assert _compiled_size(manager) == 2

    manager.evict_dir("dir")
    assert _compiled_size(manager) == 1
    assert await manager.render_by_id("other.j2", {"y": 7}) == "7"


# --- the cache's own evictions ----------------------------------------------


async def test_an_id_the_lru_evicted_is_read_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under a bounded ``max_size`` the least recently used id leaves the cache and its next render reads it."""
    monkeypatch.setattr(rm_mod, "template_cache_settings", lambda: TemplateCacheSettings(ttl=300, max_size=2))
    manager, store = _manager({"a.j2": "{{ x }}", "b.j2": "{{ y }}", "c.j2": "{{ z }}"})

    await manager.render_by_id("a.j2", {"x": 1})
    await manager.render_by_id("b.j2", {"y": 1})
    await manager.render_by_id("c.j2", {"z": 1})  # evicts a.j2 (LRU)
    assert _compiled_size(manager) == 2

    await manager.render_by_id("b.j2", {"y": 2})
    await manager.render_by_id("c.j2", {"z": 2})
    assert store.loads == ["a.j2", "b.j2", "c.j2"]
    await manager.render_by_id("a.j2", {"x": 2})
    assert store.loads == ["a.j2", "b.j2", "c.j2", "a.j2"]


async def test_an_id_past_its_ttl_is_read_again_with_an_unbounded_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """With ``max_size=None`` + a ttl, an expired id is no longer counted and its next render reads it."""
    monkeypatch.setattr(rm_mod, "template_cache_settings", lambda: TemplateCacheSettings(ttl=1, max_size=None))
    manager, store = _manager({"a.j2": "{{ x }}", "b.j2": "{{ y }}"})

    await manager.render_by_id("a.j2", {"x": 1})
    await manager.render_by_id("b.j2", {"y": 1})
    assert manager.get_cache_info() == (2, None)

    await asyncio.sleep(1.3)
    assert _compiled_size(manager) == 0
    await manager.render_by_id("a.j2", {"x": 2})
    assert store.loads == ["a.j2", "b.j2", "a.j2"]
    assert _compiled_size(manager) == 1


async def test_a_dir_delete_drops_only_the_ids_under_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dir delete drops the compiled ids under its prefix and keeps every other one compiled."""
    monkeypatch.setattr(rm_mod, "template_cache_settings", lambda: TemplateCacheSettings(ttl=300, max_size=8))
    manager, store = _manager({"keep.j2": "{{ x }}", "dir/gone.j2": "{{ y }}"})
    await manager.render_by_id("keep.j2", {"x": 1})
    await manager.render_by_id("dir/gone.j2", {"y": 2})

    await manager.delete_template_dir("dir")
    assert _compiled_size(manager) == 1
    assert await manager.render_by_id("keep.j2", {"x": 3}) == "3"
    assert store.loads == ["keep.j2", "dir/gone.j2"]


def _compiled_size(manager: rm_mod.ResourceManager) -> int:
    info = manager.get_cache_info()
    assert info is not None
    return info.currsize


# --- eviction frees the engine-cached {% include %}/{% extends %} targets ----


async def test_reupload_of_included_resource_is_served_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-uploading an included resource must make the next render of its includer
    reflect the new content.

    ``{% include %}`` resolves its target through the loader into the engine's own
    template cache, separate from the compiled-template cache this manager keys by
    id. The upload seam evicts that engine entry too, so the includer no longer
    serves the stale compiled include."""
    monkeypatch.setattr(rm_mod, "template_cache_settings", lambda: TemplateCacheSettings(ttl=300, max_size=8))
    manager, _ = _manager({"a.j2": '{% include "b.j2" %}', "b.j2": "one"})
    assert await manager.render_by_id("a.j2") == "one"

    await manager.upload_template("b.j2", "two")
    assert await manager.render_by_id("a.j2") == "two"


async def test_reupload_of_extended_resource_is_served_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """``{% extends %}`` resolves its parent through the same loader/engine cache,
    so re-uploading the parent is served fresh on the child's next render."""
    monkeypatch.setattr(rm_mod, "template_cache_settings", lambda: TemplateCacheSettings(ttl=300, max_size=8))
    manager, _ = _manager(
        {
            "child.j2": '{% extends "base.j2" %}{% block body %}C{% endblock %}',
            "base.j2": "one[{% block body %}{% endblock %}]",
        }
    )
    assert await manager.render_by_id("child.j2") == "one[C]"

    await manager.upload_template("base.j2", "two[{% block body %}{% endblock %}]")
    assert await manager.render_by_id("child.j2") == "two[C]"


async def test_evicting_an_include_rereads_only_that_include(monkeypatch: pytest.MonkeyPatch) -> None:
    """An evicted include is read again on its includer's next render; the includer and its
    other includes stay compiled and are not read."""
    monkeypatch.setattr(rm_mod, "template_cache_settings", lambda: TemplateCacheSettings(ttl=300, max_size=8))
    manager, store = _manager(
        {"page.j2": '{% include "lib/a.j2" %}{% include "lib/b.j2" %}', "lib/a.j2": "a", "lib/b.j2": "b"}
    )
    assert await manager.render_by_id("page.j2") == "ab"

    store.items["lib/a.j2"] = "A"
    manager.evict_compiled("lib/a.j2")
    reads_before = len(store.loads)
    assert await manager.render_by_id("page.j2") == "Ab"
    assert store.loads[reads_before:] == ["lib/a.j2"]


async def test_evict_dir_frees_engine_cached_includes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dir evict drops engine-cached include targets under the prefix, even when
    the includer itself lies outside the prefix and stays compiled."""
    monkeypatch.setattr(rm_mod, "template_cache_settings", lambda: TemplateCacheSettings(ttl=300, max_size=8))
    manager, store = _manager({"a.j2": '{% include "lib/b.j2" %}', "lib/b.j2": "one"})
    assert await manager.render_by_id("a.j2") == "one"

    # Change the included resource directly in the store, then evict its dir. The
    # includer (a.j2) is outside the prefix, so it stays compiled in the manager
    # cache; only the engine-cached dependency is freed.
    store.items["lib/b.j2"] = "two"
    manager.evict_dir("lib")
    assert await manager.render_by_id("a.j2") == "two"


async def test_clear_cache_frees_engine_cached_includes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A full clear drops every engine-cached include target too, so a re-uploaded
    dependency is served fresh afterwards."""
    monkeypatch.setattr(rm_mod, "template_cache_settings", lambda: TemplateCacheSettings(ttl=300, max_size=8))
    manager, store = _manager({"a.j2": '{% include "b.j2" %}', "b.j2": "one"})
    assert await manager.render_by_id("a.j2") == "one"

    store.items["b.j2"] = "two"
    manager.clear_cache()
    assert await manager.render_by_id("a.j2") == "two"


async def test_clear_cache_frees_engine_cache_even_when_compiled_cache_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The engine's dependency cache is always present, independent of the
    compiled-template cache. A full clear frees it even when compiled caching is
    disabled, so a re-uploaded include is served fresh."""
    monkeypatch.setattr(rm_mod, "template_cache_settings", lambda: TemplateCacheSettings(ttl=0, max_size=8))
    manager, store = _manager({"a.j2": '{% include "b.j2" %}', "b.j2": "one"})
    assert manager.get_cache_info() is None  # compiled caching disabled
    assert await manager.render_by_id("a.j2") == "one"

    store.items["b.j2"] = "two"
    manager.clear_cache()
    assert await manager.render_by_id("a.j2") == "two"
