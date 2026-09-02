"""The in-process client-tool runnable's error semantics.

A tool body's OWN failure while doing its job becomes a typed, model-visible
``ToolException`` (the same message shape a FastMCP boundary uses), so an agent's
tool loop surfaces it instead of aborting. Platform-machinery failures — a vanished
registration re-resolved on the live-fire path, an identity-gate denial — stay raw
loud aborts, and cancellation passes through untouched.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from langchain_core.tools import ToolException

from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.tools.binding import UnknownToolError


@pytest.fixture(autouse=True)
def _clean_server():
    async def _clear() -> None:
        provider = app._fast_mcp.local_provider
        for tool in list(await provider.list_tools()):
            provider.remove_tool(tool.name)

    asyncio.run(_clear())
    yield
    asyncio.run(_clear())


def test_tool_body_exception_becomes_tool_exception(caplog):
    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True)
            async def boom(q: str) -> str:
                """A tool whose body raises while doing its job."""
                raise ValueError("kaboom")

            tool_obj = await app.tools.get_tool("boom")
            runnable = app._tool_binding._client_runnable(tool_obj)

            with (
                caplog.at_level(logging.WARNING, logger="tai42_skeleton.tools.binding"),
                pytest.raises(ToolException, match=r"Error calling tool 'boom': kaboom"),
            ):
                await runnable(q="x")

            # The server-side trace is not silenced: the binding logs the failure
            # naming the tool and the error.
            assert any(
                r.name == "tai42_skeleton.tools.binding" and "boom" in r.getMessage() and "kaboom" in r.getMessage()
                for r in caplog.records
            )

    asyncio.run(run())


def test_client_tool_ainvoke_surfaces_tool_exception():
    # End to end through the exposed StructuredTool: a body failure reaches a caller
    # (a ToolNode / middleware) as a ToolException, not a raw abort.
    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True)
            async def boom(q: str) -> str:
                """A tool whose body raises while doing its job."""
                raise RuntimeError("down")

            [client_tool] = await app.tools.get_client_tools(["boom"])
            with pytest.raises(ToolException, match=r"Error calling tool 'boom': down"):
                await client_tool.ainvoke({"q": "x"})

    asyncio.run(run())


def test_machinery_failure_on_live_fire_propagates_unchanged(monkeypatch):
    # The live-fire branch re-resolves the target; a vanished registration is
    # platform machinery, so its UnknownToolError propagates raw — never a tool error.
    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True)
            async def gone(q: str) -> str:
                """A tool that would succeed if it still existed."""
                return q

            tool_obj = await app.tools.get_tool("gone")
            runnable = app._tool_binding._client_runnable(tool_obj)

            # Enter the identity-bound branch, then vanish the registration so the
            # re-resolution fails as machinery before the body is ever reached.
            monkeypatch.setattr(app._tool_binding, "_bound_execution_identity", lambda: object())
            app._tool_binding.remove_tool("gone")

            with pytest.raises(UnknownToolError):
                await runnable(q="x")

    asyncio.run(run())


def test_identity_gate_denial_propagates_unchanged(monkeypatch):
    # An identity-gate denial is platform machinery: its own exception type propagates
    # raw, never wrapped as a tool error.
    class GateDenied(Exception):
        pass

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True)
            async def guarded(q: str) -> str:
                """A tool the identity gate refuses to dispatch."""
                return q

            tool_obj = await app.tools.get_tool("guarded")
            runnable = app._tool_binding._client_runnable(tool_obj)

            async def _deny(*args, **kwargs):
                raise GateDenied("not allowed")

            monkeypatch.setattr(app._tool_binding, "_bound_execution_identity", lambda: object())
            monkeypatch.setattr(app._tool_binding, "_authorize_execution_dispatch", _deny)

            with pytest.raises(GateDenied):
                await runnable(q="x")

    asyncio.run(run())


def test_cancelled_error_passes_through_untouched():
    # CancelledError is a BaseException, never converted: the ``except Exception`` wrap
    # leaves it untouched.
    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True)
            async def cancels(q: str) -> str:
                """A tool whose body is cancelled mid-run."""
                raise asyncio.CancelledError

            tool_obj = await app.tools.get_tool("cancels")
            runnable = app._tool_binding._client_runnable(tool_obj)

            with pytest.raises(asyncio.CancelledError):
                await runnable(q="x")

    asyncio.run(run())


def test_suspended_interaction_becomes_the_reserved_park_marker():
    # An async ask_user parks the caller and returns a SuspendedInteraction sentinel.
    # Inside a graph the tool task must COMPLETE (so ask_user runs exactly once, never
    # replayed on resume), so the in-process seam converts the sentinel to the reserved
    # contract marker the in-graph park middleware recognizes — never the raw sentinel.
    from datetime import UTC, datetime

    from tai42_contract.interactions import (
        SUSPENDED_INTERACTION_MARKER_KEY,
        SuspendedInteraction,
        get_resume_continuation_tool,
        read_suspended_interaction_marker,
        reset_resume_continuation_tool,
        set_resume_continuation_tool,
    )

    deadline = datetime(2030, 1, 1, tzinfo=UTC)

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True)
            async def parks(q: str):
                """A tool that async-parks and returns the suspension sentinel."""
                # Faithful to ``ask_user(mode="async")``: the park names the continuation
                # bound around this run as its owner, so this run may adopt it.
                return SuspendedInteraction(
                    interaction_id="i1", expiry_at=deadline, resume_owner=get_resume_continuation_tool()
                )

            tool_obj = await app.tools.get_tool("parks")
            runnable = app._tool_binding._client_runnable(tool_obj)

            token = set_resume_continuation_tool("agent_resume")
            try:
                result = await runnable(q="x")
            finally:
                reset_resume_continuation_tool(token)
            assert isinstance(result, dict)
            assert SUSPENDED_INTERACTION_MARKER_KEY in result
            marker = read_suspended_interaction_marker(result)
            assert marker == {
                "interaction_id": "i1",
                "expiry_at": deadline.isoformat(),
                # The owner rides the wire form the claim point reads.
                "resume_owner": "agent_resume",
            }

    asyncio.run(run())
