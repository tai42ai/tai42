"""ResourceManager ``{% include %}``: a dependency resolves through the sync-loader bridge, on
the loop that drives the render, and ``ignore missing`` still works. All storage is an
in-memory ``Storage`` double."""

from __future__ import annotations

import asyncio

from tai42_contract.template import TemplatedText

from ._manager_fixtures import _manager

# --- {% include %} sync-loader bridge ---------------------------------------


async def test_include_resolves_via_sync_loader() -> None:
    manager, _ = _manager({"child.j2": "CHILD", "parent.j2": 'P[{% include "child.j2" %}]'})
    assert await manager.render_by_id("parent.j2") == "P[CHILD]"


async def test_an_include_loads_on_the_loop_that_drives_the_render() -> None:
    """A dependency of a stored or an inline render is read on the loop that awaits the
    render, the loop whose pooled storage clients the app owns, never on a loop of its own."""
    manager, store = _manager(
        {"child.j2": "CHILD", "inline_child.j2": "INLINE", "parent.j2": 'P[{% include "child.j2" %}]'}
    )
    loaded_on: dict[str, asyncio.AbstractEventLoop] = {}
    read = store.load

    async def recording_load(path: str) -> str:
        loaded_on[path] = asyncio.get_running_loop()
        return await read(path)

    store.load = recording_load  # type: ignore[method-assign]
    assert await manager.render_by_id("parent.j2") == "P[CHILD]"
    rendered = await manager.render_templated_text(TemplatedText(content='I[{% include "inline_child.j2" %}]'))
    assert rendered == "I[INLINE]"
    loop = asyncio.get_running_loop()
    assert loaded_on == {"parent.j2": loop, "child.j2": loop, "inline_child.j2": loop}


async def test_include_missing_with_ignore_missing_renders_blank() -> None:
    """A genuinely missing include (FileNotFoundError -> None) keeps
    ``ignore missing`` working rather than masking other failures."""
    manager, _ = _manager({"m.j2": 'X[{% include "nope.j2" ignore missing %}]'})
    assert await manager.render_by_id("m.j2") == "X[]"
