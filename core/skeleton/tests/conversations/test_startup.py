"""The conversation recovery lifecycle wiring: the ``on_startup`` hook that re-drives every
unfinished record, the ``on_post_swap`` hook that (re)establishes the periodic sweep on
the serving loop, and the ``on_shutdown`` hook that stops it.

The recovery passes themselves are tested in ``test_turn`` and ``test_delivery_sweep``;
here only the wiring is asserted — that each hook runs its passes, in the order the
durability story depends on, and no-ops without a backend. The sweep lives on the
``on_post_swap`` hook (not ``on_startup``) so it is re-established on the real serving
loop after a reload rather than spawned on the throwaway build-thread loop.
"""

from __future__ import annotations

import pytest

import tai42_skeleton.conversations as conversations_package


def _router():
    from tai42_contract.app import tai42_app

    from tai42_skeleton.app.instance import app as skeleton_app

    with tai42_app.bound(skeleton_app):
        from tai42_skeleton.routers import conversations as router

    return router


@pytest.fixture
def order(monkeypatch) -> list[str]:
    """Record the recovery passes as they fire, so their presence AND order are asserted."""
    ran: list[str] = []

    async def _redrive_accepted() -> None:
        ran.append("redrive_accepted")

    async def _redrive_pending() -> None:
        ran.append("redrive_pending")

    def _start_delivery_sweep() -> None:
        ran.append("start_delivery_sweep")

    async def _stop_delivery_sweep() -> None:
        ran.append("stop_delivery_sweep")

    monkeypatch.setattr(conversations_package, "redrive_accepted", _redrive_accepted)
    monkeypatch.setattr(conversations_package, "redrive_pending", _redrive_pending)
    monkeypatch.setattr(conversations_package, "start_delivery_sweep", _start_delivery_sweep)
    monkeypatch.setattr(conversations_package, "stop_delivery_sweep", _stop_delivery_sweep)
    return ran


async def test_startup_registers_the_completion_tools(monkeypatch):
    # Both completion continuations are force-registered at startup so the tools the turn engine
    # binds always resolve: ``conversation_deliver`` (a parked AGENT turn's own final answer) and
    # ``deliver_tool_completion`` (a parked TOOL turn's terminal, mapped via reply_expr). Each
    # ``tool(...)`` call carries the exact name, hidden meta and force flag.
    from tai42_contract.app import tai42_app

    from tai42_skeleton.app.instance import app as skeleton_app
    from tai42_skeleton.conversations.turn import (
        COMPLETION_TOOL_NAME,
        DELIVER_TOOL_COMPLETION_NAME,
        deliver_agent_completion,
        deliver_tool_completion,
    )

    calls: list = []
    original_tool = skeleton_app.tools.tool

    def _record_tool(func, **kwargs):
        calls.append((func, kwargs))
        return func

    with tai42_app.bound(skeleton_app):
        from tai42_skeleton.routers import conversations as router

        monkeypatch.setattr(skeleton_app.tools, "tool", _record_tool)
        try:
            await router._register_conversation_completion_tool()
        finally:
            monkeypatch.setattr(skeleton_app.tools, "tool", original_tool)

    assert len(calls) == 2
    registered = {kwargs["name"]: (func, kwargs) for func, kwargs in calls}
    assert set(registered) == {COMPLETION_TOOL_NAME, DELIVER_TOOL_COMPLETION_NAME}
    agent_func, agent_kwargs = registered[COMPLETION_TOOL_NAME]
    assert agent_func is deliver_agent_completion
    tool_func, tool_kwargs = registered[DELIVER_TOOL_COMPLETION_NAME]
    assert tool_func is deliver_tool_completion
    for kwargs in (agent_kwargs, tool_kwargs):
        assert kwargs["force"] is True
        assert kwargs["meta"] == {"tai42/hidden": True}


async def test_startup_redrives_intake_then_delivery(monkeypatch, order):
    # Intake re-drive FIRST: it gives every stranded ``accepted`` record a terminal outcome
    # the delivery re-drive then picks up. The sweep is established separately (post-swap).
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")
    await _router()._redrive_pending_conversations()
    assert order == ["redrive_accepted", "redrive_pending"]


async def test_startup_is_a_no_op_without_a_backend(monkeypatch, order):
    monkeypatch.delenv("CONVERSATIONS_REDIS_URL", raising=False)
    await _router()._redrive_pending_conversations()
    assert order == []


async def test_post_swap_starts_the_sweep_with_a_backend(monkeypatch, order):
    # The sweep is (re)established on the serving loop at boot and after every epoch swap —
    # not by the on_startup re-drive, so it survives a reload rather than dying on the
    # throwaway build-thread loop.
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")
    _router()._start_conversations_delivery_sweep()
    assert order == ["start_delivery_sweep"]


async def test_post_swap_is_a_no_op_without_a_backend(monkeypatch, order):
    monkeypatch.delenv("CONVERSATIONS_REDIS_URL", raising=False)
    _router()._start_conversations_delivery_sweep()
    assert order == []


async def test_shutdown_stops_the_sweep_with_a_backend(monkeypatch, order):
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")
    await _router()._stop_conversations_delivery_sweep()
    assert order == ["stop_delivery_sweep"]


async def test_shutdown_is_a_no_op_without_a_backend(monkeypatch, order):
    monkeypatch.delenv("CONVERSATIONS_REDIS_URL", raising=False)
    await _router()._stop_conversations_delivery_sweep()
    assert order == []
