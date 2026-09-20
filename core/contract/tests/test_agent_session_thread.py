"""Contract tests for the ambient in-process agent session-thread door.

Pin the ContextVar deposit's default (``None``), the set/reset round-trip, and the
:func:`agent_session_thread` context manager's deposit + ``finally`` restore (including
nesting), since both the skeleton run-tool binding (reader) and a contract-facing plugin
(writer) rely on this one shared channel.
"""

from __future__ import annotations

import asyncio


def test_default_is_none():
    from tai42_contract.agent import get_agent_session_thread

    assert get_agent_session_thread() is None


def test_set_then_reset_round_trips():
    from tai42_contract.agent import (
        get_agent_session_thread,
        reset_agent_session_thread,
        set_agent_session_thread,
    )

    token = set_agent_session_thread("flow:run-1:node-a")
    try:
        assert get_agent_session_thread() == "flow:run-1:node-a"
    finally:
        reset_agent_session_thread(token)
    assert get_agent_session_thread() is None


def test_context_manager_deposits_and_restores():
    from tai42_contract.agent import agent_session_thread, get_agent_session_thread

    assert get_agent_session_thread() is None
    with agent_session_thread("flow:run-1:node-a"):
        assert get_agent_session_thread() == "flow:run-1:node-a"
        # Nesting restores the OUTER deposit on inner exit, not None.
        with agent_session_thread("flow:run-1:node-b"):
            assert get_agent_session_thread() == "flow:run-1:node-b"
        assert get_agent_session_thread() == "flow:run-1:node-a"
    assert get_agent_session_thread() is None


def test_deposit_inherited_by_a_task_on_a_copy():
    from tai42_contract.agent import agent_session_thread, get_agent_session_thread

    async def run() -> str | None:
        # A task created INSIDE the block runs on a context copy carrying the deposit, so a
        # detached run stays threaded even after the depositing block exits.
        with agent_session_thread("flow:run-1:node-a"):
            task = asyncio.ensure_future(_read_after_yield())
        return await task

    async def _read_after_yield() -> str | None:
        await asyncio.sleep(0)
        return get_agent_session_thread()

    assert asyncio.run(run()) == "flow:run-1:node-a"
