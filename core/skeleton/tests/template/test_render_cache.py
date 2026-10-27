"""The render cache, through its resource manager: the compiled-template cache enabled vs disabled,
its controls, the compile-once inline cache, the evict seams that invalidate it, the id registry kept
bounded by the cache's own evictions, and the engine's ``{% include %}``/``{% extends %}``
dependency cache freed by every eviction. All storage is an in-memory ``Storage`` double."""

from __future__ import annotations

from typing import Protocol, cast

import pytest
from tai42_contract.template import TemplatedText

from tai42_skeleton.template import resource_manager as rm_mod
from tai42_skeleton.template.settings import TemplateCacheSettings

from ._manager_fixtures import _manager


class _SupportsCacheInvalidate(Protocol):
    def cache_invalidate(self, *args: object) -> bool: ...


class _SupportsCacheContains(Protocol):
    def cache_contains(self, *args: object) -> bool: ...


# --- construction: cache enabled vs disabled --------------------------------


def test_cache_enabled_exposes_cache_info_and_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default (positive ttl + size) wraps fetch in an alru cache: info reports
    hits/misses and clear empties it."""
    monkeypatch.setattr(rm_mod, "template_cache_settings", lambda: TemplateCacheSettings(ttl=300, max_size=8))
    manager, _ = _manager({"a.j2": "{{ x }}"})

    assert manager.get_cache_info() is not None

    async def run() -> None:
        await manager.render_by_id("a.j2", {"x": 1})  # miss -> populate
        await manager.render_by_id("a.j2", {"x": 2})  # hit
        info = manager.get_cache_info()
        assert info is not None
        assert info.hits == 1
        assert info.misses == 1
        manager.clear_cache()
        cleared = manager.get_cache_info()
        assert cleared is not None
        assert cleared.currsize == 0

    import asyncio

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

    import asyncio

    assert asyncio.run(manager.render_by_id("a.j2", {"x": 5})) == "5"


# --- the public evict seams ---------------------------------------------------


async def test_evict_compiled_public_seam_drops_one_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # The public seam the fleet ``evict_template`` op applies on a sibling worker: it
    # drops one compiled key and is idempotent on an already-absent key.
    monkeypatch.setattr(rm_mod, "template_cache_settings", lambda: TemplateCacheSettings(ttl=300, max_size=8))
    manager, _ = _manager({"m.j2": "{{ x }}"})
    await manager.render_by_id("m.j2", {"x": 1})
    assert manager.get_cache_info().currsize == 1

    manager.evict_compiled("m.j2")
    assert manager.get_cache_info().currsize == 0
    manager.evict_compiled("m.j2")  # idempotent: no-op, must not raise
    manager.evict_compiled("never-cached.j2")  # unknown key: no-op


async def test_evict_dir_public_seam_drops_under_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    # The directory counterpart the fleet op applies for a dir delete: prefix semantics
    # (``dir`` -> ``dir/``), dropping only compilations under it.
    monkeypatch.setattr(rm_mod, "template_cache_settings", lambda: TemplateCacheSettings(ttl=300, max_size=8))
    manager, _ = _manager({"dir/a.j2": "{{ x }}", "other.j2": "{{ y }}"})
    await manager.render_by_id("dir/a.j2", {"x": 1})
    await manager.render_by_id("other.j2", {"y": 2})
    assert manager.get_cache_info().currsize == 2

    manager.evict_dir("dir")
    assert manager.get_cache_info().currsize == 1
    assert await manager.render_by_id("other.j2", {"y": 7}) == "7"


# --- tracking set pruned against the cache's own evictions ------------------


async def test_cached_ids_pruned_against_lru_eviction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under a bounded ``max_size`` the compile-time sweep drops tracking-set ids
    the LRU has evicted, so the set never outgrows the live cache."""
    monkeypatch.setattr(rm_mod, "template_cache_settings", lambda: TemplateCacheSettings(ttl=300, max_size=2))
    manager, _ = _manager({"a.j2": "{{ x }}", "b.j2": "{{ y }}", "c.j2": "{{ z }}"})

    await manager.render_by_id("a.j2", {"x": 1})
    await manager.render_by_id("b.j2", {"y": 1})
    await manager.render_by_id("c.j2", {"z": 1})  # evicts a.j2 (LRU), sweeps the set

    ids = manager._render_cache._tracked_ids
    assert len(ids) <= 2
    contains = cast(_SupportsCacheContains, manager._render_cache._compiled).cache_contains
    # Every tracked id is still genuinely in the cache — no stale ids linger.
    assert all(contains(tid) for tid in ids)


async def test_cached_ids_pruned_against_ttl_eviction(monkeypatch: pytest.MonkeyPatch) -> None:
    """With ``max_size=None`` + a ttl, ids the cache expires on its own are still
    swept from the tracking set on the next compile (the unbounded+ttl mode)."""
    import asyncio

    monkeypatch.setattr(rm_mod, "template_cache_settings", lambda: TemplateCacheSettings(ttl=1, max_size=None))
    manager, _ = _manager({"a.j2": "{{ x }}", "b.j2": "{{ y }}", "c.j2": "{{ z }}"})

    await manager.render_by_id("a.j2", {"x": 1})
    await manager.render_by_id("b.j2", {"y": 1})
    assert manager._render_cache._tracked_ids == {"a.j2", "b.j2"}

    # Let the ttl call_later fire (evicting a.j2/b.j2 from the underlying cache),
    # then a fresh compile runs the amortized sweep.
    await asyncio.sleep(1.3)
    await manager.render_by_id("c.j2", {"z": 1})

    ids = manager._render_cache._tracked_ids
    assert "a.j2" not in ids
    assert "b.j2" not in ids
    assert "c.j2" in ids
    contains = cast(_SupportsCacheContains, manager._render_cache._compiled).cache_contains
    assert all(contains(tid) for tid in ids)


async def test_delete_template_dir_prunes_ids_already_evicted_by_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """The id registry stays bounded by what is actually cached: an entry the
    cache dropped on its own (simulating TTL/LRU) is pruned during a dir delete
    even though it lies outside the deleted prefix."""
    monkeypatch.setattr(rm_mod, "template_cache_settings", lambda: TemplateCacheSettings(ttl=300, max_size=8))
    manager, _ = _manager({"keep.j2": "{{ x }}", "gone.j2": "{{ y }}"})
    await manager.render_by_id("keep.j2", {"x": 1})
    await manager.render_by_id("gone.j2", {"y": 2})
    assert manager._render_cache._tracked_ids == {"keep.j2", "gone.j2"}

    # Drop "gone.j2" straight from the underlying cache (as TTL/LRU would),
    # leaving it stale in the tracking registry.
    # With caching on, ``_compiled`` is the alru cache wrapper, which
    # exposes ``cache_invalidate``; its static type is a union with the bare
    # (uncached) method, so narrow to the cache-bearing shape here.
    cached = cast(_SupportsCacheInvalidate, manager._render_cache._compiled)
    assert cached.cache_invalidate("gone.j2") is True
    assert manager._render_cache._tracked_ids == {"keep.j2", "gone.j2"}

    # A dir delete that matches neither id still prunes the stale "gone.j2".
    await manager.delete_template_dir("unrelated")
    assert manager._render_cache._tracked_ids == {"keep.j2"}


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
