"""Lifecycle handler registration/running and the tool-reloader dispatch."""

from __future__ import annotations

import asyncio
import logging

import pytest

from ._doubles import _Mixin


def test_handlers_register_and_run_sync_and_async():
    m = _Mixin()
    order: list[str] = []

    @m._on_startup
    def s1():
        order.append("s1")

    @m._on_shutdown
    def d1():
        order.append("d1")

    @m._on_reload
    def r1():
        order.append("r1")

    assert s1 in m._startup_handlers.values()
    assert d1 in m._shutdown_handlers.values()
    assert r1 in m._reload_handlers.values()

    async def a():
        order.append("async")

    asyncio.run(m._run_handlers([s1, a]))
    assert order == ["s1", "async"]


def test_run_handlers_swallows_for_shutdown_but_raises_when_asked(caplog):
    m = _Mixin()

    async def boom():
        raise RuntimeError("handler boom")

    # Default (the shutdown path): recover, but log loudly — never a
    # truly-silent drop.
    with caplog.at_level(logging.ERROR):
        asyncio.run(m._run_handlers([boom]))
    assert "handler boom" in caplog.text
    # Startup/reload paths: surface loudly.
    with pytest.raises(RuntimeError, match=r"lifecycle handlers failed.*boom"):
        asyncio.run(m._run_handlers([boom], raise_on_error=True))


def test_tool_reloader_sync_and_default_result():
    m = _Mixin()

    @m._tool_reloader("flow")
    def _reload(action, name):
        return None  # falsy -> default result dict synthesized

    out = asyncio.run(m._run_tool_reload("flow", "reload", "f1"))
    assert out == {"kind": "flow", "action": "reload", "name": "f1", "status": "ok"}


def test_tool_reloader_async_passthrough_result():
    m = _Mixin()

    @m._tool_reloader("flow")
    async def _reload(action, name):
        return {"custom": True}

    assert asyncio.run(m._run_tool_reload("flow", "remove", "f1")) == {"custom": True}


def test_run_tool_reload_unknown_kind_and_bad_action_raise():
    m = _Mixin()
    with pytest.raises(ValueError, match="delete"):
        asyncio.run(m._run_tool_reload("flow", "delete", "x"))
    with pytest.raises(RuntimeError, match="no_such_kind"):
        asyncio.run(m._run_tool_reload("no_such_kind", "reload", "x"))
