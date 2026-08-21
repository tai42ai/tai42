"""Tests for the driver-continuation context an async ``ask_user`` reads: the
default (no resuming driver bound), the set/get/reset round-trip, nested restore,
and isolation across concurrent asyncio tasks. The completion continuation (the
deferred-response delivery tool) is a generic twin with the same discipline.
"""

from __future__ import annotations

import asyncio

from tai42_contract.interactions import (
    get_park_completion_tool,
    get_resume_continuation_tool,
    reset_park_completion_tool,
    reset_resume_continuation_tool,
    set_park_completion_tool,
    set_resume_continuation_tool,
)


def test_default_is_none():
    # No resuming driver bound: an async ask raised here has no continuation tool.
    assert get_resume_continuation_tool() is None


def test_set_get_reset_round_trip():
    token = set_resume_continuation_tool("resume_tool")
    assert get_resume_continuation_tool() == "resume_tool"
    reset_resume_continuation_tool(token)
    assert get_resume_continuation_tool() is None


def test_reset_restores_previous_value():
    outer = set_resume_continuation_tool("outer_tool")
    inner = set_resume_continuation_tool("inner_tool")
    assert get_resume_continuation_tool() == "inner_tool"
    reset_resume_continuation_tool(inner)
    # Resetting the inner token restores the outer binding, not the default.
    assert get_resume_continuation_tool() == "outer_tool"
    reset_resume_continuation_tool(outer)
    assert get_resume_continuation_tool() is None


def test_completion_default_is_none():
    # No completion delivery bound: a resumed run's driver fires nothing.
    assert get_park_completion_tool() is None


def test_completion_set_get_reset_round_trip():
    token = set_park_completion_tool("conversation_deliver")
    assert get_park_completion_tool() == "conversation_deliver"
    reset_park_completion_tool(token)
    assert get_park_completion_tool() is None


def test_completion_is_independent_of_the_resume_continuation():
    # The two continuations are separate contextvars: binding one never affects the other.
    completion = set_park_completion_tool("conversation_deliver")
    assert get_resume_continuation_tool() is None
    resume = set_resume_continuation_tool("agent_resume")
    assert get_park_completion_tool() == "conversation_deliver"
    reset_resume_continuation_tool(resume)
    reset_park_completion_tool(completion)


def test_isolation_across_tasks():
    async def scenario() -> None:
        set_resume_continuation_tool("parent_tool")
        seen: dict[str, str | None] = {}

        async def worker(name: str) -> None:
            # A task starts from a copy of the parent context, then binds its own
            # driver continuation without disturbing siblings or the parent.
            assert get_resume_continuation_tool() == "parent_tool"
            set_resume_continuation_tool(name)
            await asyncio.sleep(0)
            seen[name] = get_resume_continuation_tool()

        await asyncio.gather(worker("a"), worker("b"))
        assert seen == {"a": "a", "b": "b"}
        assert get_resume_continuation_tool() == "parent_tool"

    asyncio.run(scenario())
