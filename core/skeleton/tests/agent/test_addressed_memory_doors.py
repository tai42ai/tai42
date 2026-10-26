"""The platform reserves thread namespaces from the agent's OWN answer about its memory.

A neutral synthetic agent (``tests.agent._addressed_memory_fixtures``) carries its thread id nested
inside an option blob only it understands; ``Agent.addressed_memory`` is the one way the platform
sees it, at every door that maps caller input to run kwargs.
"""

from __future__ import annotations

import asyncio

import pytest

from tai42_skeleton.agent.thread_reservation import ReservedThreadNamespaceError
from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest

from .conftest import _fixture_flag

_MODULE = "tests.agent._addressed_memory_fixtures"


def _manifest() -> Manifest:
    return Manifest.model_validate({"agents": [{"title": "agents", "module": _MODULE, "include": ["scoped_memory"]}]})


def _run_seen() -> list[dict]:
    return _fixture_flag(_MODULE, "run_seen")


def test_the_run_tool_refuses_a_reserved_thread_the_agent_reports():
    async def run() -> None:
        async with app.app_context(_manifest()):
            _run_seen().clear()
            with pytest.raises(ReservedThreadNamespaceError, match="thread_id"):
                await app.tools.run_tool(
                    "scoped_memory", {"text": "hi", "engine_options": {"scope": {"key": "bridge:chat:x"}}}
                )
            with pytest.raises(ReservedThreadNamespaceError, match="checkpoint_id"):
                await app.tools.run_tool(
                    "scoped_memory", {"text": "hi", "engine_options": {"scope": {"mark": "bridge:chat:x"}}}
                )
            assert _run_seen() == []

    asyncio.run(run())


def _reserved_thread() -> dict:
    return {"text": "hi", "engine_options": {"scope": {"key": "bridge:chat:x"}}}


def test_the_mcp_edge_refuses_a_reserved_thread_the_agent_reports():
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    async def run() -> None:
        async with app.app_context(_manifest()):
            _run_seen().clear()
            async with Client(app._fast_mcp) as client:
                with pytest.raises(ToolError, match="thread_id may not use the reserved 'bridge:' namespace"):
                    await client.call_tool("scoped_memory", _reserved_thread())
            assert _run_seen() == []

    asyncio.run(run())


def test_a_preset_over_the_agent_refuses_a_reserved_thread_the_agent_reports(preset_manager_restored):
    async def run() -> None:
        async with app.app_context(_manifest()):
            _run_seen().clear()
            await app.preset_manager.register(
                "scoped_preset", "scoped_memory", {"engine_options": {"scope": {"key": "bridge:chat:x"}}}, [], "p"
            )
            with pytest.raises(ReservedThreadNamespaceError, match="thread_id"):
                await app.tools.run_tool("scoped_preset", {"text": "hi"})
            assert _run_seen() == []

    asyncio.run(run())


def test_a_nested_client_tool_handle_refuses_a_reserved_thread_the_agent_reports():
    from tai42_contract.tools import ClientToolError

    async def run() -> None:
        async with app.app_context(_manifest()):
            _run_seen().clear()
            [handle] = await app.tools.get_client_tools(["scoped_memory"])
            # The refusal is the run tool body's own failure, so the engine reads it as the tool's error.
            with pytest.raises(ClientToolError, match="thread_id may not use the reserved 'bridge:' namespace"):
                await handle.invoke(_reserved_thread())
            assert _run_seen() == []

    asyncio.run(run())


def test_the_sse_run_door_refuses_a_reserved_thread_the_agent_reports():
    import json

    from tai42_skeleton.routers import agents as router

    from ..routers.test_agents import _make_run_request

    async def run() -> None:
        async with app.app_context(_manifest()):
            _run_seen().clear()
            response = await router.run_agent(
                _make_run_request("scoped_memory", json.dumps(_reserved_thread()).encode())
            )
            assert response.status_code == 400
            assert "thread_id may not use the reserved 'bridge:' namespace" in json.loads(bytes(response.body))["error"]
            assert _run_seen() == []

    asyncio.run(run())


def test_the_deposited_session_thread_yields_to_a_thread_the_agent_reports():
    from tai42_skeleton.agent.session_thread import agent_session_thread

    async def run() -> None:
        async with app.app_context(_manifest()):
            _run_seen().clear()
            with agent_session_thread("consumer:run-1:step-a"):
                answer = await app.tools.run_tool(
                    "scoped_memory", {"text": "hi", "engine_options": {"scope": {"key": "own-thread"}}}
                )
            assert answer == "own-thread"
            assert "thread_id" not in _run_seen()[-1]

    asyncio.run(run())


def test_the_conversation_door_asks_the_agent_about_the_raw_start_before_validating():
    from tai42_skeleton.conversations.turn.agent_turn import _agent_run_kwargs

    async def run() -> None:
        async with app.app_context(_manifest()):
            agent = app.agents.get_agent("scoped_memory")
            seen = _fixture_flag(_MODULE, "addressed_memory_seen")
            seen.clear()
            # ``resume_from`` steers the run's own continuity, so the route owns it.
            start = {"text": "hi", "resume_from": "cp-1", "not_a_field": True}
            with pytest.raises(ValueError, match=r"must not set the route-owned key\(s\) \['resume_from'\]"):
                _agent_run_kwargs(agent, "turn text", start)
            assert seen == [start]
            # The contract thread id is the route's own, refused before any validation too.
            with pytest.raises(ValueError, match=r"route-owned key\(s\) \['thread_id'\]"):
                _agent_run_kwargs(agent, "turn text", {"text": "hi", "thread_id": "t"})

    asyncio.run(run())


def test_a_conversation_start_that_fails_the_agent_input_is_a_value_error():
    from pydantic import ValidationError

    from tai42_skeleton.conversations.turn.agent_turn import _agent_run_kwargs

    async def run() -> None:
        async with app.app_context(_manifest()):
            agent = app.agents.get_agent("scoped_memory")
            with pytest.raises(ValidationError):
                _agent_run_kwargs(agent, "turn text", {"engine_options": {}})
            run_kwargs, start = _agent_run_kwargs(agent, "turn text", {"text": "hi"})
            assert (run_kwargs, start) == ({"text": "hi"}, True)

    asyncio.run(run())


def test_an_agent_class_without_addressed_memory_fails_registration():
    # Instantiating the class at registration raises the ABC's TypeError, which aborts boot
    # naming the missing method.
    from tai42_skeleton.marketplace.compat import CorePluginBootError

    manifest = Manifest.model_validate(
        {
            "agents": [
                {"title": "bad", "module": "tests.agent._no_memory_answer_fixtures", "include": ["no_memory_answer"]}
            ]
        }
    )

    async def run() -> None:
        async with app.app_context(manifest):
            pass  # pragma: no cover — start() aborts before the body runs

    with pytest.raises(CorePluginBootError, match="addressed_memory"):
        asyncio.run(run())
