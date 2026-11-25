"""ResourceManager loads a render's literal include closure before the render begins: each level's
loads in flight together, each distinct id read once, a warm render reading nothing. A dependency's
load failure raises only when the render reaches it, and an eviction during the render makes it read
the dependency fresh. All storage is an in-memory ``Storage`` double."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from tai42_contract.template import TemplateBodyError, TemplatedText, TemplateNotFoundError, TemplateNotTextError

from tai42_skeleton.template import ResourceManager
from tai42_skeleton.template import resource_manager as rm_mod
from tai42_skeleton.template.dependency_preload import DependencyPreload
from tai42_skeleton.template.reads import template_reads
from tai42_skeleton.template.settings import TemplateCacheSettings

from ._manager_fixtures import _InMemoryStorage, _manager


@pytest.fixture(autouse=True)
def _compiled_cache_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rm_mod, "template_cache_settings", lambda: TemplateCacheSettings(ttl=300, max_size=8))


def _record_render_starts(manager: ResourceManager, store: _InMemoryStorage) -> list[str]:
    """The ids read from storage before each render began, its root excepted, one entry per render."""
    started: list[str] = []
    scope = manager._render_scope

    @contextmanager
    def recording_scope(loop: asyncio.AbstractEventLoop, preload: DependencyPreload) -> Iterator[None]:
        started.append(",".join(sorted(set(store.loads) - {"page.j2"})))
        with scope(loop, preload):
            yield

    manager._render_scope = recording_scope  # type: ignore[method-assign]
    return started


async def test_the_includes_of_a_template_load_in_one_round() -> None:
    manager, store = _manager(
        {
            "page.j2": '{% include "lib/a.j2" %}{% include "lib/b.j2" %}{% include "lib/c.j2" %}',
            "lib/a.j2": "A",
            "lib/b.j2": "B",
            "lib/c.j2": "C",
        }
    )
    release = store.hold("lib/a.j2", "lib/b.j2", "lib/c.j2")
    render = asyncio.create_task(manager.render_by_id("page.j2"))
    try:
        await store.until_in_flight(3)
    finally:
        release.set()
    assert await render == "ABC"
    assert store.max_in_flight == 3
    assert Counter(store.loads) == {"page.j2": 1, "lib/a.j2": 1, "lib/b.j2": 1, "lib/c.j2": 1}


async def test_nested_includes_resolve_and_each_distinct_id_is_read_once() -> None:
    manager, store = _manager(
        {
            "page.j2": '[{% include "lib/a.j2" %}|{% include "lib/b.j2" %}]',
            "lib/a.j2": 'a({% include "lib/c.j2" %})',
            "lib/b.j2": 'b({% include "lib/c.j2" %})',
            "lib/c.j2": 'c({% include "lib/d.j2" %})',
            "lib/d.j2": "d",
        }
    )
    assert await manager.render_by_id("page.j2") == "[a(c(d))|b(c(d))]"
    assert Counter(store.loads) == {"page.j2": 1, "lib/a.j2": 1, "lib/b.j2": 1, "lib/c.j2": 1, "lib/d.j2": 1}


async def test_extends_and_import_targets_are_loaded_before_the_render() -> None:
    manager, store = _manager(
        {
            "page.j2": (
                '{% extends "lib/base.j2" %}{% import "lib/macros.j2" as m %}{% block body %}{{ m.hi() }}{% endblock %}'
            ),
            "lib/base.j2": "<{% block body %}{% endblock %}>",
            "lib/macros.j2": "{% macro hi() %}hi{% endmacro %}",
        }
    )
    started = _record_render_starts(manager, store)
    assert await manager.render_by_id("page.j2") == "<hi>"
    assert started == ["lib/base.j2,lib/macros.j2"]


async def test_concurrent_renders_share_one_read_of_a_common_include() -> None:
    manager, store = _manager(
        {
            "page.j2": 'P{% include "lib/a.j2" %}',
            "other.j2": 'O{% include "lib/a.j2" %}',
            "lib/a.j2": "a",
        }
    )
    release = store.hold("lib/a.j2")
    renders = [
        asyncio.create_task(manager.render_by_id("page.j2")),
        asyncio.create_task(manager.render_by_id("other.j2")),
    ]
    try:
        async with asyncio.timeout(1):
            while "lib/a.j2" not in manager._loads_in_flight._flights or (
                manager._loads_in_flight._flights["lib/a.j2"].waiters < 2
            ):
                await asyncio.sleep(0)
    finally:
        release.set()
    assert await asyncio.gather(*renders) == ["Pa", "Oa"]
    assert store.loads.count("lib/a.j2") == 1


async def test_a_warm_render_reads_nothing() -> None:
    manager, store = _manager(
        {
            "page.j2": '{% include "lib/a.j2" %}{% include "lib/b.j2" ignore missing %}',
            "lib/a.j2": 'a{% include "lib/c.j2" %}',
            "lib/c.j2": "c",
        }
    )
    assert await manager.render_by_id("page.j2") == "ac"
    reads_after_the_first_render = list(store.loads)
    assert await manager.render_by_id("page.j2") == "ac"
    assert store.loads == reads_after_the_first_render


async def test_a_required_missing_include_raises_naming_it_and_is_remembered_absent() -> None:
    manager, store = _manager({"page.j2": 'X{% include "lib/none.j2" %}'})
    with pytest.raises(TemplateNotFoundError, match=r"its include 'lib/none.j2' is not stored"):
        await manager.render_by_id("page.j2")
    with pytest.raises(TemplateNotFoundError):
        await manager.render_by_id("page.j2")
    assert store.loads.count("lib/none.j2") == 1


async def test_ignore_missing_renders_blank_under_the_closure_load() -> None:
    manager, store = _manager(
        {"page.j2": 'X[{% include ["lib/none.j2", "lib/a.j2"] ignore missing %}]', "lib/a.j2": "a"}
    )
    assert await manager.render_by_id("page.j2") == "X[a]"
    assert Counter(store.loads) == {"page.j2": 1, "lib/none.j2": 1, "lib/a.j2": 1}


_UNTAKEN = '{% if false %}{% include "lib/bad.j2" %}{% endif %}{% include "lib/ok.j2" %}'
_TAKEN = '{% include "lib/bad.j2" %}{% include "lib/ok.j2" %}'


def _not_text() -> UnicodeDecodeError:
    return UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")


@pytest.mark.parametrize(
    ("bad_source", "failure"),
    [
        pytest.param("{% if %}", None, id="does-not-parse"),
        pytest.param("x", _not_text(), id="not-text"),
        pytest.param("x", ConnectionError("store down"), id="storage-error"),
    ],
)
async def test_a_dependency_the_render_never_reaches_does_not_fail_it(
    bad_source: str, failure: Exception | None
) -> None:
    manager, store = _manager({"page.j2": _UNTAKEN, "lib/bad.j2": bad_source, "lib/ok.j2": "ok"})
    if failure is not None:
        store.failures["lib/bad.j2"] = failure
    assert await manager.render_by_id("page.j2") == "ok"


async def test_a_storage_error_on_a_reached_dependency_raises_as_itself_without_a_second_read() -> None:
    manager, store = _manager({"page.j2": _TAKEN, "lib/bad.j2": "x", "lib/ok.j2": "ok"})
    store.failures["lib/bad.j2"] = ConnectionError("store down")
    with pytest.raises(ConnectionError, match="store down"):
        await manager.render_by_id("page.j2")
    assert store.loads.count("lib/bad.j2") == 1


async def test_a_reached_dependency_that_is_not_text_is_refused_naming_its_parent() -> None:
    manager, store = _manager({"page.j2": _TAKEN, "lib/bad.j2": "x", "lib/ok.j2": "ok"})
    store.failures["lib/bad.j2"] = _not_text()
    with pytest.raises(
        TemplateNotTextError, match=r"^references stored id 'page.j2', which could not be rendered: .*lib/bad.j2"
    ):
        await manager.render_by_id("page.j2")
    assert store.loads.count("lib/bad.j2") == 1


async def test_a_dependency_that_does_not_parse_is_not_descended_into() -> None:
    manager, store = _manager(
        {"page.j2": _UNTAKEN, "lib/bad.j2": '{% include "lib/c.j2" %}{% if %}', "lib/ok.j2": "ok"}
    )
    assert await manager.render_by_id("page.j2") == "ok"
    assert "lib/c.j2" not in store.loads


async def test_cancelling_a_render_cancels_the_reads_of_its_dependencies() -> None:
    manager, store = _manager({"page.j2": '{% include "lib/a.j2" %}', "lib/a.j2": "a"})
    release = store.hold("lib/a.j2")
    render = asyncio.create_task(manager.render_by_id("page.j2"))
    try:
        await store.until_in_flight(1)
        render.cancel()
        async with asyncio.timeout(1):
            while "lib/a.j2" not in store.cancelled_loads:
                await asyncio.sleep(0)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await render


@pytest.mark.parametrize(
    "evict",
    [
        pytest.param(lambda manager: manager.evict_compiled("lib/a.j2"), id="evict_compiled"),
        pytest.param(lambda manager: manager.evict_dir("lib"), id="evict_dir"),
        pytest.param(lambda manager: manager.clear_cache(), id="clear_cache"),
    ],
)
async def test_an_eviction_after_the_preload_makes_the_render_read_the_dependency_fresh(
    monkeypatch: pytest.MonkeyPatch, evict: Callable[[ResourceManager], None]
) -> None:
    manager, store = _manager({"page.j2": '{% include "lib/a.j2" %}', "lib/a.j2": "old"})
    preload = DependencyPreload.load
    writes = iter([True])

    async def preload_then_write(self: DependencyPreload, dependencies: frozenset[str]) -> None:
        await preload(self, dependencies)
        if next(writes, False):
            store.items["lib/a.j2"] = "new"
            evict(manager)

    monkeypatch.setattr(DependencyPreload, "load", preload_then_write)
    assert await manager.render_by_id("page.j2") == "new"
    assert await manager.render_by_id("page.j2") == "new"


async def test_an_include_that_does_not_parse_is_refused_naming_its_parent() -> None:
    manager, _ = _manager({"page.j2": '{% include "lib/a.j2" %}', "lib/a.j2": "{% if %}"})
    with pytest.raises(TemplateBodyError, match=r"^references stored id 'page.j2', which could not be rendered: "):
        await manager.render_by_id("page.j2")


async def test_an_inline_render_loads_its_includes_before_it_begins() -> None:
    manager, store = _manager({"lib/a.j2": "a", "lib/b.j2": "b"})
    started = _record_render_starts(manager, store)
    text = TemplatedText(content='{% include "lib/a.j2" %}{% include "lib/b.j2" %}')
    assert await manager.render_templated_text(text) == "ab"
    assert started == ["lib/a.j2,lib/b.j2"]
    assert Counter(store.loads) == {"lib/a.j2": 1, "lib/b.j2": 1}


async def test_template_reads_records_every_include_and_probe_under_concurrent_renders() -> None:
    manager, _ = _manager(
        {
            "page.j2": '{% include "lib/a.j2" %}',
            "other.j2": '{% include "lib/b.j2" %}',
            "lib/a.j2": 'a{% include "lib/c.j2" %}',
            "lib/b.j2": "b",
            "lib/c.j2": "c",
        }
    )
    with template_reads() as reads:
        rendered = await asyncio.gather(
            manager.render_by_id("page.j2", locale="he"),
            manager.render_by_id("other.j2"),
        )
    assert rendered == ["ac", "b"]
    assert reads == {"page.j2@he", "page.j2", "other.j2", "lib/a.j2", "lib/b.j2", "lib/c.j2"}


async def _until_reading(manager: ResourceManager, store: _InMemoryStorage, template_id: str) -> None:
    """Return once a second render reads ``template_id``, by joining the read in flight or starting its own."""
    async with asyncio.timeout(1):
        while store.loads.count(template_id) < 2:
            flight = manager._loads_in_flight._flights.get(template_id)
            if flight is not None and flight.waiters >= 2:
                return
            await asyncio.sleep(0)


@pytest.mark.parametrize(
    ("held", "evict"),
    [
        pytest.param("page.j2", lambda manager: manager.evict_compiled("page.j2"), id="template-evict_compiled"),
        pytest.param("lib/a.j2", lambda manager: manager.evict_compiled("lib/a.j2"), id="include-evict_compiled"),
        pytest.param("lib/a.j2", lambda manager: manager.evict_dir("lib"), id="include-evict_dir"),
        pytest.param("lib/a.j2", lambda manager: manager.clear_cache(), id="include-clear_cache"),
    ],
)
async def test_a_render_after_an_eviction_does_not_join_the_read_in_flight_before_it(
    held: str, evict: Callable[[ResourceManager], None]
) -> None:
    manager, store = _manager({"page.j2": '{% include "lib/a.j2" %}', "lib/a.j2": "old"})
    if held == "page.j2":
        store.items["page.j2"] = "old"
    release = store.hold(held)
    before = asyncio.create_task(manager.render_by_id("page.j2"))
    try:
        await store.until_in_flight(1)
        store.items[held] = "new"
        evict(manager)
        del store._held[held]  # only the read begun before the write stays held
        after = asyncio.create_task(manager.render_by_id("page.j2"))
        await _until_reading(manager, store, held)
        await asyncio.wait({after}, timeout=1)
    finally:
        release.set()
    await before
    assert await after == "new"
    assert await manager.render_by_id("page.j2") == "new"


async def test_an_inline_template_that_cannot_be_compiled_is_refused_as_its_body_fault() -> None:
    manager, _ = _manager()
    deep = "{{ " + "(" * 200 + "1" + ")" * 200 + " }}"
    with pytest.raises(TemplateBodyError):
        await manager.render_templated_text(TemplatedText(content=deep))


async def test_a_missing_dependency_is_read_once_per_render_with_the_cache_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rm_mod, "template_cache_settings", lambda: TemplateCacheSettings(ttl=0, max_size=0))
    manager, store = _manager({"page.j2": '{% include "lib/x.j2" ignore missing %}ok'})
    assert await manager.render_by_id("page.j2") == "ok"
    assert store.loads == ["page.j2", "lib/x.j2"]


async def test_renders_outnumbering_the_default_pool_finish_when_their_loads_need_it() -> None:
    manager, store = _manager({"page.j2": "{% include name %}", "lib/a.j2": "a"})
    stored_load = store.load

    async def load_on_the_default_pool(path: str) -> str:
        await asyncio.to_thread(lambda: None)
        return await stored_load(path)

    store.load = load_on_the_default_pool  # type: ignore[method-assign]
    loop = asyncio.get_running_loop()
    small = ThreadPoolExecutor(max_workers=2)
    loop.set_default_executor(small)
    renders = [manager.render_by_id("page.j2", {"name": "lib/a.j2"}) for _ in range(4)]
    try:
        assert await asyncio.wait_for(asyncio.gather(*renders), timeout=3) == ["a"] * 4
    finally:
        # Cancelling the queued loads releases any render still waiting on one.
        small.shutdown(wait=False, cancel_futures=True)
        loop.set_default_executor(ThreadPoolExecutor())
        await asyncio.sleep(0.1)


async def test_an_eviction_after_a_render_is_cancelled_still_reaches_its_running_thread() -> None:
    manager, store = _manager(
        {"page.j2": '{% include name %}{% include "lib/a.j2" %}', "lib/a.j2": "old", "lib/b.j2": "b"}
    )
    release = store.hold("lib/b.j2")
    render = asyncio.create_task(manager.render_by_id("page.j2", {"name": "lib/b.j2"}))
    try:
        await store.until_in_flight(1)
        render.cancel()
        with pytest.raises(asyncio.CancelledError):
            await render
        store.items["lib/a.j2"] = "new"
        manager.evict_compiled("lib/a.j2")
    finally:
        release.set()
    async with asyncio.timeout(1):
        while not manager._render_cache.has_dependency("lib/a.j2"):
            await asyncio.sleep(0.01)
    assert await manager.render_by_id("page.j2", {"name": "lib/b.j2"}) == "bnew"
