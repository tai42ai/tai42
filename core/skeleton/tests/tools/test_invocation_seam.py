"""The ambient invoked-tool seam armed by the two execution doors.

Skeleton legs of the invocation seam (the contract channel itself is pinned in
``core/contract/tests/test_tool_invocation_seam.py``):

* ``ToolBinding.run_tool`` deposits the invoked tool's name for the span of its
  execution, a probe reading ``current_tool_invocation()`` INSIDE the body sees it,
  it is ``None`` outside any tool, a nested re-dispatch restores the OUTER name after
  the inner returns (token discipline), and the deposit is unwound in ``finally`` even
  when the body raises.
* ``InvocationSeamMiddleware`` deposits the called tool's name at the MCP
  ``tools/call`` edge (which reaches ``Tool.run`` directly, never the ``run_tool``
  seam) and resets it in ``finally``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastmcp.server.middleware import MiddlewareContext
from tai42_contract.tools import current_tool_invocation

from tai42_skeleton.app.instance import app
from tai42_skeleton.app.sub_mcp_app import InvocationSeamMiddleware
from tai42_skeleton.manifest import Manifest


@pytest.fixture(autouse=True)
def _clean_server():
    # The app server is a process singleton; drop every tool before and after so
    # force-bound probe tools never collide across tests.
    async def _clear() -> None:
        provider = app._fast_mcp.local_provider
        for tool in list(await provider.list_tools()):
            provider.remove_tool(tool.name)

    asyncio.run(_clear())
    yield
    asyncio.run(_clear())


# -- run_tool leg -------------------------------------------------------------


def test_probe_reads_running_tool_name_inside_run_tool():
    seen: dict[str, str | None] = {}

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True)
            async def probe(x: int) -> int:
                """Records the in-flight invocation seen from inside its own body."""
                inv = current_tool_invocation()
                seen["name"] = inv.tool_name if inv is not None else None
                return x

            # No tool executing yet: the seam reads None outside any run_tool span.
            assert current_tool_invocation() is None

            result = await app.tools.run_tool("probe", {"x": 7})
            assert result == 7
            # The body saw its own registered name deposited for the run.
            assert seen["name"] == "probe"
            # The deposit is unwound once the run returns.
            assert current_tool_invocation() is None

    asyncio.run(run())


def test_nested_dispatch_restores_outer_after_inner_returns():
    seen: dict[str, str | None] = {}

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True)
            async def inner(x: int) -> int:
                """The inner tool a dispatch re-enters."""
                inv = current_tool_invocation()
                seen["inner"] = inv.tool_name if inv is not None else None
                return x

            @app.tools.tool(force=True)
            async def outer(x: int) -> int:
                """Dispatches ``inner`` by name mid-body and reads the seam around it."""
                before = current_tool_invocation()
                seen["outer_before"] = before.tool_name if before is not None else None
                await app.tools.run_tool("inner", {"x": x})
                after = current_tool_invocation()
                seen["outer_after"] = after.tool_name if after is not None else None
                return x

            await app.tools.run_tool("outer", {"x": 3})

    asyncio.run(run())

    # The inner re-dispatch re-set for ``inner`` and, on return, restored the OUTER
    # name — never leaving the deposit stuck on the inner tool (token discipline).
    assert seen == {"outer_before": "outer", "inner": "inner", "outer_after": "outer"}


def test_seam_is_unwound_when_the_tool_body_raises():
    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True)
            async def boom(x: int) -> int:
                """A tool whose body raises while doing its job."""
                raise ValueError("kaboom")

            with pytest.raises(ValueError, match="kaboom"):
                await app.tools.run_tool("boom", {"x": 1})

            # The reset lives in ``finally``, so a raising tool still unwinds the deposit.
            assert current_tool_invocation() is None

    asyncio.run(run())


# -- MCP middleware leg -------------------------------------------------------


def test_middleware_deposits_for_the_called_tool():
    seen: dict[str, str | None] = {}

    async def run() -> None:
        mw = InvocationSeamMiddleware()
        # The MCP call edge hands the middleware a context whose message names the
        # called tool; a minimal stand-in suffices to exercise the deposit.
        context = SimpleNamespace(message=SimpleNamespace(name="acme_echo"))

        async def call_next(_context: object) -> str:
            inv = current_tool_invocation()
            seen["name"] = inv.tool_name if inv is not None else None
            return "result"

        assert current_tool_invocation() is None
        result = await mw.on_call_tool(cast(MiddlewareContext[Any], context), call_next)
        assert result == "result"
        # The downstream call saw the called tool's name deposited by the door.
        assert seen["name"] == "acme_echo"
        # Reset in ``finally`` — the deposit never leaks past the call.
        assert current_tool_invocation() is None

    asyncio.run(run())


def test_middleware_resets_when_the_call_raises():
    async def run() -> None:
        mw = InvocationSeamMiddleware()
        context = SimpleNamespace(message=SimpleNamespace(name="acme_echo"))

        async def call_next(_context: object) -> str:
            raise RuntimeError("edge boom")

        with pytest.raises(RuntimeError, match="edge boom"):
            await mw.on_call_tool(cast(MiddlewareContext[Any], context), call_next)
        assert current_tool_invocation() is None

    asyncio.run(run())
