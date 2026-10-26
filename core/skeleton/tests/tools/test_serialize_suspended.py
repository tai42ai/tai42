"""A ``SuspendedInteraction`` (the async-park sentinel) survives BOTH tool-dispatch
paths by TYPE — the direct FunctionTool run (``_serialize_result``) and the preset
(``TransformedTool``) run — never flattened to a plain dict.

If the preset path flattened it, the turn engine's type-based park recognition would
fail: the run would proceed UN-parked while the delivered question's later answer
stranded (no park entry was ever written) — a silent lost-park.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from tai42_contract.interactions import RESUME_BUFFERED_MARKER_KEY, ResumeBuffered, SuspendedInteraction
from tai42_contract.tools import UndeclaredPauseError
from tai42_kit.interactions.park_marker import read_suspended_interaction_marker, suspended_interaction_marker

from tai42_skeleton.app import instance
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.tools.binding.result import _serialize_result

_MOD = "tests.app._fixtures.suspend_tool"


def test_serialize_result_preserves_suspended_interaction():
    sentinel = SuspendedInteraction(interaction_id="i1")
    assert _serialize_result(sentinel) is sentinel


def test_serialize_result_preserves_resume_buffered():
    # The other non-terminal park signal (a resume that left sibling asks open) is kept BY TYPE too,
    # so the delivery chokepoint and the visit recognise a still-parked step, never a terminal.
    buffered = ResumeBuffered(remaining_ids=["r1"])
    assert _serialize_result(buffered) is buffered


def test_preset_path_preserves_resume_buffered():
    # A preset (TransformedTool) over a base returning a ``ResumeBuffered`` must return it BY TYPE,
    # exactly as the direct run does — never the flattened ``{"remaining_ids": [...]}`` dict.
    async def go() -> Any:
        await _clear_server()
        async with instance.app.app_context(Manifest.model_validate(_manifest())):
            await instance.app.preset_manager.register("buffered_preset", "make_buffered", {}, [], "d")
            return await instance.app.tools.run_tool("buffered_preset", {})

    result = asyncio.run(go())
    assert isinstance(result, ResumeBuffered)
    assert result.remaining_ids == ["r1", "r2"]


def test_serialize_result_still_serializes_plain_values():
    assert _serialize_result({"a": 1}) == {"a": 1}
    assert _serialize_result("hi") == "hi"


def _manifest() -> dict:
    return {
        "tools": [
            {
                "title": "suspend",
                "module": _MOD,
                "include": ["make_suspend", "make_buffered", "make_suspend_payload", "echo_payload", "relay_payload"],
            }
        ]
    }


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


_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"q": {"type": "string"}},
    "required": ["q"],
}
_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}


def test_input_schema_preset_path_preserves_suspended_interaction():
    # The input-schema routing path (``_route``) also validates the result against an
    # ``output_schema``. A suspending call's park sentinel is a SUSPEND signal, NOT the
    # tool's output: validation must NOT apply to it — the flattened
    # ``{"interaction_id": ...}`` would fail the result schema
    # (``'answer' is a required property``). The park must pass through by TYPE, exactly
    # as the plain output-schema path preserves it.
    async def go() -> Any:
        await _clear_server()
        async with instance.app.app_context(Manifest.model_validate(_manifest())):
            await instance.app.preset_manager.register(
                "suspend_input_preset",
                "make_suspend_payload",
                {},
                [],
                "d",
                output_schema=_ANSWER_SCHEMA,
                input_schema=_INPUT_SCHEMA,
            )
            return await instance.app.tools.run_tool("suspend_input_preset", {"q": "hi"})

    result = asyncio.run(go())
    assert isinstance(result, SuspendedInteraction)
    assert result.interaction_id == "i-preset"


def test_input_schema_preset_path_still_validates_non_parking_result():
    # The park exemption must not be over-broad: a NON-parking result on the input-schema
    # path is still validated against the ``output_schema``. A conforming result passes
    # through; a violating one is rejected loudly.
    async def conforming() -> Any:
        await _clear_server()
        async with instance.app.app_context(Manifest.model_validate(_manifest())):
            await instance.app.preset_manager.register(
                "echo_ok_preset",
                "echo_payload",
                {},
                [],
                "d",
                output_schema=_ANSWER_SCHEMA,
                input_schema=_INPUT_SCHEMA,
            )
            return await instance.app.tools.run_tool("echo_ok_preset", {"q": "hi"})

    assert asyncio.run(conforming()) == {"answer": "ok"}

    violating_schema = {
        "type": "object",
        "properties": {"answer": {"type": "integer"}},
        "required": ["answer"],
    }

    async def violating() -> Any:
        await _clear_server()
        async with instance.app.app_context(Manifest.model_validate(_manifest())):
            await instance.app.preset_manager.register(
                "echo_bad_preset",
                "echo_payload",
                {},
                [],
                "d",
                output_schema=violating_schema,
                input_schema=_INPUT_SCHEMA,
            )
            return await instance.app.tools.run_tool("echo_bad_preset", {"q": "hi"})

    with pytest.raises(ToolError, match=r"^preset 'echo_bad_preset' output does not match its output_schema: "):
        asyncio.run(violating())


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
    # RESULT. An async ask's park sentinel is a SUSPEND signal, NOT the tool's
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


def _spy_runs_index(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Route the runs-index chokepoint to an in-memory spy; return the recorded terminals."""
    import tai42_skeleton.runs.chokepoint as chokepoint

    terminals: list[dict[str, Any]] = []

    class _SpyStore:
        async def insert_start(
            self, run_id, preset_name, preset_version, *, trace_id, user_id, session_id, interaction_id, started_at
        ):
            pass

        async def update_outcome(
            self, run_id, outcome, ended_at, *, trace_id=None, interaction_id=None, resumed_interactions=None
        ):
            terminals.append({"outcome": outcome, "interaction_id": interaction_id})

    monkeypatch.setattr(chokepoint, "component_store_configured", lambda _c: True)
    monkeypatch.setattr(chokepoint, "get_run_index_store", lambda: _SpyStore())
    return terminals


def _assert_park_marker(result: Any) -> None:
    assert result.is_error is False, result.content
    marker = read_suspended_interaction_marker(result.structured_content)
    assert marker is not None
    assert marker["interaction_id"] == "i-preset"


def test_output_schema_preset_park_crosses_the_mcp_edge_as_the_park_marker(
    preset_manager_restored: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Over the MCP edge a preset's park is the reserved park marker, a SUSPEND signal and not the
    # tool's output: the output-schema guard passes it and the advertised schema admits it, so the
    # caller reads the park, not a schema violation, and the run records ``parked``.
    terminals = _spy_runs_index(monkeypatch)

    async def go() -> Any:
        await _clear_server()
        async with instance.app.app_context(Manifest.model_validate(_manifest())):
            await instance.app.preset_manager.register(
                "suspend_schema_preset", "make_suspend", {}, [], "d", output_schema=_ANSWER_SCHEMA
            )
            async with Client(instance.app._fast_mcp) as client:
                return await client.call_tool("suspend_schema_preset", {}, raise_on_error=False)

    _assert_park_marker(asyncio.run(go()))
    assert terminals == [{"outcome": "parked", "interaction_id": "i-preset"}]


def test_input_schema_preset_park_crosses_the_mcp_edge_as_the_park_marker(preset_manager_restored: Any) -> None:
    # The input-schema routing path holds the same: its park crosses the MCP edge as the marker,
    # never validated against the preset's output schema.
    async def go() -> Any:
        await _clear_server()
        async with instance.app.app_context(Manifest.model_validate(_manifest())):
            await instance.app.preset_manager.register(
                "suspend_input_preset",
                "make_suspend_payload",
                {},
                [],
                "d",
                output_schema=_ANSWER_SCHEMA,
                input_schema=_INPUT_SCHEMA,
            )
            async with Client(instance.app._fast_mcp) as client:
                return await client.call_tool("suspend_input_preset", {"q": "hi"}, raise_on_error=False)

    _assert_park_marker(asyncio.run(go()))


def test_a_tool_declared_to_return_the_sentinel_parks_over_the_mcp_edge_as_the_park_marker() -> None:
    # ``make_suspend`` declares ``-> SuspendedInteraction``, so its declared output schema is the
    # sentinel model's; its park still crosses the MCP edge as the marker, which the advertised
    # schema admits beside the declared one.
    async def go() -> Any:
        await _clear_server()
        async with (
            instance.app.app_context(Manifest.model_validate(_manifest())),
            Client(instance.app._fast_mcp) as client,
        ):
            return await client.call_tool("make_suspend", {}, raise_on_error=False)

    _assert_park_marker(asyncio.run(go()))


def test_a_preset_without_an_output_schema_parks_over_the_mcp_edge_as_the_park_marker(
    preset_manager_restored: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A preset with no output schema inherits its base's declared schema; its park crosses the MCP
    # edge as the marker and the run records ``parked``.
    terminals = _spy_runs_index(monkeypatch)

    async def go() -> Any:
        await _clear_server()
        async with instance.app.app_context(Manifest.model_validate(_manifest())):
            await instance.app.preset_manager.register("plain_suspend_preset", "make_suspend", {}, [], "d")
            async with Client(instance.app._fast_mcp) as client:
                return await client.call_tool("plain_suspend_preset", {}, raise_on_error=False)

    _assert_park_marker(asyncio.run(go()))
    assert terminals == [{"outcome": "parked", "interaction_id": "i-preset"}]


def test_output_schema_preset_path_preserves_resume_buffered(preset_manager_restored: Any) -> None:
    # A ``ResumeBuffered`` is the other non-terminal park signal: a preset carrying an
    # ``output_schema`` over a base returning one hands it back BY TYPE, never validating its
    # flattened ``{"remaining_ids": [...]}`` against the authored schema.
    async def go() -> Any:
        await _clear_server()
        async with instance.app.app_context(Manifest.model_validate(_manifest())):
            await instance.app.preset_manager.register(
                "buffered_schema_preset", "make_buffered", {}, [], "d", output_schema=_ANSWER_SCHEMA
            )
            return await instance.app.tools.run_tool("buffered_schema_preset", {})

    result = asyncio.run(go())
    assert isinstance(result, ResumeBuffered)
    assert result.remaining_ids == ["r1", "r2"]


# Data shaped like the two park markers, returned as the RESULT of a tool that cannot pause.
_MARKER_SHAPED = [
    suspended_interaction_marker("not-a-park", None, None, interaction_ids=["not-a-park"], caller_interaction_ids=[]),
    {RESUME_BUFFERED_MARKER_KEY: {"remaining_ids": ["x"]}},
]


async def _register_relay_presets() -> None:
    """Register two output-schema presets over the non-pausing ``relay_payload``: plain and input-schema."""
    await instance.app.preset_manager.register(
        "relay_strict", "relay_payload", {}, [], "d", output_schema=_ANSWER_SCHEMA
    )
    await instance.app.preset_manager.register(
        "relay_strict_routed",
        "relay_payload",
        {},
        [],
        "d",
        input_schema={"type": "object"},
        output_schema=_ANSWER_SCHEMA,
    )


@pytest.mark.parametrize("data", _MARKER_SHAPED, ids=["suspended-marker-shape", "resume-buffered-marker-shape"])
def test_marker_shaped_data_from_a_tool_that_cannot_pause_is_validated_in_process(
    data: dict[str, Any], preset_manager_restored: Any
) -> None:
    # In process a park is recognised by TYPE off the armed gate only, so a non-pausing tool's
    # marker-shaped result is the tool's output and the preset's output schema refuses it.
    async def go() -> list[str]:
        await _clear_server()
        refusals: list[str] = []
        async with instance.app.app_context(Manifest.model_validate(_manifest())):
            await _register_relay_presets()
            for name, arguments in (("relay_strict", {"payload": data}), ("relay_strict_routed", data)):
                with pytest.raises(ToolError) as caught:
                    await instance.app.tools.run_tool(name, arguments)
                refusals.append(str(caught.value))
        return refusals

    refusals = asyncio.run(go())
    assert len(refusals) == 2
    assert all("'answer' is a required property" in text for text in refusals)


@pytest.mark.parametrize("data", _MARKER_SHAPED, ids=["suspended-marker-shape", "resume-buffered-marker-shape"])
def test_marker_shaped_data_from_a_tool_that_cannot_pause_is_refused_over_the_mcp_edge(
    data: dict[str, Any], preset_manager_restored: Any
) -> None:
    # On the MCP edge the marker is read off the result, and a tool that does not declare it can
    # pause is refused for returning one.
    async def go() -> list[Any]:
        await _clear_server()
        async with instance.app.app_context(Manifest.model_validate(_manifest())):
            await _register_relay_presets()
            async with Client(instance.app._fast_mcp) as client:
                return [
                    await client.call_tool("relay_strict", {"payload": data}, raise_on_error=False),
                    await client.call_tool("relay_strict_routed", data, raise_on_error=False),
                ]

    for name, result in zip(("relay_strict", "relay_strict_routed"), asyncio.run(go()), strict=True):
        assert result.is_error is True
        assert [block.text for block in result.content] == [str(UndeclaredPauseError(name))]
