"""ResourceManager ``{% include %}``: a dependency resolves through the sync-loader bridge, a
pooled-client cleanup failure is logged without failing the render, and ``ignore missing``
still works. All storage is an in-memory ``Storage`` double."""

from __future__ import annotations

import logging

import pytest

from tai42_skeleton.template import resource_manager as rm_mod

from ._manager_fixtures import _manager

# --- {% include %} sync-loader bridge ---------------------------------------


async def test_include_resolves_via_sync_loader() -> None:
    manager, _ = _manager({"child.j2": "CHILD", "parent.j2": 'P[{% include "child.j2" %}]'})
    assert await manager.render_by_id("parent.j2") == "P[CHILD]"


async def test_include_client_cleanup_failure_is_logged_not_fatal(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A pooled-client cleanup failure after a template load is logged loudly but
    must not replace the load's result — the include still renders."""

    async def boom() -> None:
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(rm_mod, "shutdown_all_clients", boom)
    manager, _ = _manager({"child.j2": "CHILD", "parent.j2": 'P[{% include "child.j2" %}]'})
    with caplog.at_level(logging.ERROR):
        assert await manager.render_by_id("parent.j2") == "P[CHILD]"
    # The cleanup failure is logged loudly, not silently swallowed.
    assert "Error closing pooled clients" in caplog.text


async def test_include_missing_with_ignore_missing_renders_blank() -> None:
    """A genuinely missing include (FileNotFoundError -> None) keeps
    ``ignore missing`` working rather than masking other failures."""
    manager, _ = _manager({"m.j2": 'X[{% include "nope.j2" ignore missing %}]'})
    assert await manager.render_by_id("m.j2") == "X[]"
