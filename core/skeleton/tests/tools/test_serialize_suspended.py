"""A ``SuspendedInteraction`` (the async-park sentinel) survives BOTH tool-dispatch
paths by TYPE — the direct FunctionTool run (``_serialize_result``) and the preset
(``TransformedTool``) run — never flattened to a plain dict.

If the preset path flattened it, the turn engine's type-based park recognition would
fail: the flow would proceed UN-parked while the delivered question's later answer
stranded (no park entry was ever written) — a silent lost-park.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from tai42_contract.interactions import SuspendedInteraction

from tai42_skeleton.app import instance
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.tools.binding import _serialize_result

_MOD = "tests.app._fixtures.suspend_tool"


def test_serialize_result_preserves_suspended_interaction():
    sentinel = SuspendedInteraction(interaction_id="i1")
    assert _serialize_result(sentinel) is sentinel


def test_serialize_result_still_serializes_plain_values():
    assert _serialize_result({"a": 1}) == {"a": 1}
    assert _serialize_result("hi") == "hi"


def _manifest() -> dict:
    return {"tools": [{"title": "suspend", "module": _MOD, "include": ["make_suspend"]}]}


async def _clear_server() -> None:
    provider = instance.app._fast_mcp.local_provider
    for tool in list(await provider.list_tools()):
        provider.remove_tool(tool.name)


@pytest.fixture(autouse=True)
def _clean_server():
    """Clear the singleton FastMCP server's tools around each test — it outlives one
    ``app_context``, so a tool bound in a prior test would otherwise linger."""
    asyncio.run(_clear_server())
    yield
    asyncio.run(_clear_server())


def test_preset_path_preserves_suspended_interaction():
    # A preset (a TransformedTool) over an async-parking base must return the
    # SuspendedInteraction sentinel BY TYPE, exactly as the direct run does — never the
    # flattened ``{"interaction_id": ..., "expiry_at": ...}`` dict FastMCP's ToolResult
    # serialization would otherwise produce.
    async def go() -> Any:
        await _clear_server()
        async with instance.app.app_context(Manifest.model_validate(_manifest())):
            await instance.app.preset_manager.register("suspend_preset", "make_suspend", {}, [], "d")
            return await instance.app.tools.run_tool("suspend_preset", {})

    result = asyncio.run(go())
    assert isinstance(result, SuspendedInteraction)
    assert result.interaction_id == "i-preset"


def test_direct_path_preserves_suspended_interaction():
    # The direct FunctionTool run preserves the sentinel too — the baseline the preset
    # path must match.
    async def go() -> Any:
        await _clear_server()
        async with instance.app.app_context(Manifest.model_validate(_manifest())):
            return await instance.app.tools.run_tool("make_suspend", {})

    result = asyncio.run(go())
    assert isinstance(result, SuspendedInteraction)
    assert result.interaction_id == "i-preset"


def test_output_schema_preset_path_preserves_suspended_interaction():
    # A preset carrying an ``output_schema`` runs output-schema validation on the tool
    # RESULT. An async ask_user's park sentinel is a SUSPEND signal, NOT the tool's
    # output: validation must NOT apply to it — the flattened
    # ``{"interaction_id": ...}`` would fail the wrapped answer schema
    # (``'answer' is a required property``). The park must pass through by TYPE, exactly
    # as the no-output-schema preset path preserves it.
    answer_schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }

    async def go() -> Any:
        await _clear_server()
        async with instance.app.app_context(Manifest.model_validate(_manifest())):
            await instance.app.preset_manager.register(
                "suspend_schema_preset", "make_suspend", {}, [], "d", output_schema=answer_schema
            )
            return await instance.app.tools.run_tool("suspend_schema_preset", {})

    result = asyncio.run(go())
    assert isinstance(result, SuspendedInteraction)
    assert result.interaction_id == "i-preset"
