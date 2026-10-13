"""The one LangChain adapter: client-tool handles and presets as the ``StructuredTool``s a graph calls.

Covers the adapted surface (name, description, argument schema), the call's error contract (only a
``ClientToolError`` becomes a model-visible ``ToolException``; every other error leaves the call
raw), the park conversion (this run's own park, a chained park on the CALL, a refused nested park),
preset binding (fixed keys hidden and immutable, ``required`` filtered, the base tool run through
``run_tool``), the duplicate-name and tool-name-length guards, and the nested-dispatch scope every
adapted call runs inside. A fake tool facet stands in for ``tai42_app.tools`` — no LLM.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastmcp.tools import Tool
from langchain_core.tools import ToolException
from tai42_contract.agent import PresetSpec
from tai42_contract.interactions import (
    ChainedResume,
    DeliveryAddress,
    ResumeBuffered,
    SuspendedInteraction,
    get_chained_resume,
    get_park_completion,
    get_resume_continuation_tool,
    reset_park_completion,
    reset_resume_continuation_tool,
    set_park_completion,
    set_resume_continuation_tool,
)
from tai42_contract.secrets import SECRET_PLACEHOLDER, SecretValue
from tai42_contract.tools import AppTools, ClientToolError, UndeclaredPauseError
from tai42_kit.interactions.park_adoption import is_chained_park_key
from tai42_kit.interactions.park_marker import read_suspended_interaction_marker
from tai42_kit.llm import ToolNameTooLongError
from tests._client_tool import FakeClientTool, client_tool

from tai42_agents._internal.lc_tools import (
    adapt_tool_result,
    preset_structured_tool,
    resolve_tools,
    to_structured_tool,
)
from tai42_agents._internal.park import AGENT_RESUME_TOOL_NAME

_PROPS = {"example_config": {"type": "object"}, "example_config_kwargs": {"type": "object"}}


class FakeTools:
    """A stand-in for the app's ``tools`` facet: the members the adapter reaches."""

    def __init__(self, handles: dict[str, FakeClientTool] | None = None) -> None:
        self.handles = handles or {
            "example_tool": client_tool("example_tool", properties=_PROPS),
            "example_required": client_tool(
                "example_required", properties=_PROPS, required=["example_config", "example_config_kwargs"]
            ),
        }
        self.run_tool_calls: list[tuple[str, dict[str, Any]]] = []
        self.chain_seen: list[ChainedResume | None] = []

    async def get_client_tools(self, names: list[str] | None = None) -> list[FakeClientTool]:
        return [self.handles[name] for name in (names or list(self.handles))]

    async def get_tool(self, key: str) -> Tool:
        handle = self.handles[key]
        return Tool(name=handle.name, description=handle.description, parameters=handle.input_schema)

    async def run_tool(self, key: str, arguments: dict[str, Any]) -> Any:
        self.run_tool_calls.append((key, arguments))
        self.chain_seen.append(get_chained_resume())
        return await self.result(key, arguments)

    async def result(self, key: str, arguments: dict[str, Any]) -> Any:
        return {"ok": True, "args": arguments}


def _app(fake: FakeTools) -> AppTools:
    return cast(AppTools, fake)


@pytest.fixture
def parkable_run() -> Iterator[None]:
    """Bind the resume continuation a park-capable drive binds, so a chained dispatch is honored."""
    token = set_resume_continuation_tool(AGENT_RESUME_TOOL_NAME)
    yield
    reset_resume_continuation_tool(token)


def _preset(**overrides: Any) -> PresetSpec:
    fields: dict[str, Any] = {
        "name": "my_preset",
        "description": "run a preset",
        "base_tool": "example_tool",
        "fixed_kwargs": {"example_config": {"nodes": []}},
    }
    fields.update(overrides)
    return PresetSpec(**fields)


# -- to_structured_tool -----------------------------------------------------------------------


def test_a_handle_advertises_its_name_description_and_schema() -> None:
    handle = client_tool("search", properties={"q": {"type": "string"}}, required=["q"], description="Search.")
    adapted = to_structured_tool(handle)
    assert adapted.name == "search"
    assert adapted.description == "Search."
    assert adapted.args_schema == handle.input_schema
    assert asyncio.run(adapted.ainvoke({"q": "x"})) == {"q": "x"}
    assert handle.calls == [{"q": "x"}]


def test_a_client_tool_error_becomes_a_tool_exception() -> None:
    def fails() -> None:
        raise ClientToolError("fails", "Error calling tool 'fails': boom")

    with pytest.raises(ToolException, match=r"^Error calling tool 'fails': boom$") as caught:
        asyncio.run(to_structured_tool(client_tool("fails", fails)).ainvoke({}))
    assert isinstance(caught.value.__cause__, ClientToolError)


def test_an_undeclared_pause_leaves_the_call_raw() -> None:
    def parks() -> None:
        raise UndeclaredPauseError("parks")

    with pytest.raises(UndeclaredPauseError):
        asyncio.run(to_structured_tool(client_tool("parks", parks)).ainvoke({}))


def test_any_other_error_leaves_the_call_raw() -> None:
    class _MachineryError(Exception):
        pass

    def fails() -> None:
        raise _MachineryError("denied")

    with pytest.raises(_MachineryError):
        asyncio.run(to_structured_tool(client_tool("fails", fails)).ainvoke({}))


def test_a_park_this_run_owns_becomes_the_marker_with_both_id_lists(parkable_run: None) -> None:
    def parks() -> SuspendedInteraction:
        return SuspendedInteraction(
            interaction_id="i1",
            resume_owner=get_resume_continuation_tool(),
            interaction_ids=["i1", "i2"],
            caller_interaction_ids=["i2"],
        )

    result = asyncio.run(to_structured_tool(client_tool("parks", parks)).ainvoke({}))
    marker = read_suspended_interaction_marker(result)
    assert marker == {
        "interaction_id": "i1",
        "expiry_at": None,
        "resume_owner": AGENT_RESUME_TOOL_NAME,
        "interaction_ids": ["i1", "i2"],
        "caller_interaction_ids": ["i2"],
    }


def test_a_nested_park_under_a_chained_dispatch_parks_on_the_call(parkable_run: None) -> None:
    # The nested run bound its own continuation; this run is park-capable, so the dispatch was
    # chained and this run parks on the chained KEY, owned by its own continuation.
    seen: list[ChainedResume | None] = []

    def nested() -> SuspendedInteraction:
        seen.append(get_chained_resume())
        return SuspendedInteraction(interaction_id="i-nested", resume_owner="nested_driver_resume")

    result = asyncio.run(to_structured_tool(client_tool("nested", nested)).ainvoke({}))
    marker = read_suspended_interaction_marker(result)
    assert marker is not None
    (routing,) = seen
    assert isinstance(routing, ChainedResume)
    assert marker["interaction_id"] == routing.chain_key
    assert is_chained_park_key(marker["interaction_id"])
    assert marker["resume_owner"] == AGENT_RESUME_TOOL_NAME


def test_a_nested_park_with_nothing_chained_is_refused_to_the_model() -> None:
    def nested() -> SuspendedInteraction:
        return SuspendedInteraction(interaction_id="i-nested", resume_owner="nested_driver_resume")

    with pytest.raises(ToolException) as caught:
        asyncio.run(to_structured_tool(client_tool("nested", nested)).ainvoke({}))
    message = str(caught.value)
    assert message.startswith("Tool 'nested' cannot be used inside this run")
    assert "different run's resume binding" in message
    assert "'nested_driver_resume'" not in message
    assert "Error calling tool" not in message


def test_a_resume_buffered_signal_passes_through_unadapted() -> None:
    buffered = ResumeBuffered(remaining_ids=["i2"])
    assert adapt_tool_result("t", buffered) is buffered


# -- presets -----------------------------------------------------------------------------------


def test_a_preset_hides_its_fixed_keys_and_runs_the_base_tool() -> None:
    app = FakeTools()
    adapted = asyncio.run(preset_structured_tool(_app(app), _preset()))
    assert adapted.name == "my_preset"
    assert adapted.description == "run a preset"
    assert adapted.args_schema == {
        "type": "object",
        "properties": {"example_config_kwargs": {"type": "object"}},
        "required": [],
    }
    asyncio.run(adapted.ainvoke({"example_config_kwargs": {"x": 1}}))
    assert app.run_tool_calls == [
        ("example_tool", {"example_config": {"nodes": []}, "example_config_kwargs": {"x": 1}})
    ]


def test_a_preset_cannot_override_a_fixed_key() -> None:
    app = FakeTools()
    adapted = asyncio.run(preset_structured_tool(_app(app), _preset()))
    asyncio.run(adapted.ainvoke({"example_config": {"nodes": ["evil"]}, "example_config_kwargs": {"x": 1}}))
    assert app.run_tool_calls[-1][1]["example_config"] == {"nodes": []}


def test_a_preset_keeps_a_required_runtime_key_and_drops_a_fixed_one() -> None:
    adapted = asyncio.run(preset_structured_tool(_app(FakeTools()), _preset(base_tool="example_required")))
    assert isinstance(adapted.args_schema, dict)
    assert adapted.args_schema["required"] == ["example_config_kwargs"]


@pytest.mark.parametrize("malformed", ["session_id", None, ["ok", 123]], ids=["bare-string", "none", "non-string"])
def test_a_preset_over_a_malformed_required_list_raises(malformed: Any) -> None:
    base = FakeClientTool(
        name="wobbly_tool",
        description="",
        input_schema={"type": "object", "properties": {"keep": {"type": "string"}}, "required": malformed},
        body=lambda **_: None,
    )
    app = FakeTools({"wobbly_tool": base})
    with pytest.raises(TypeError, match="malformed input schema 'required'") as caught:
        asyncio.run(preset_structured_tool(_app(app), _preset(base_tool="wobbly_tool", fixed_kwargs={})))
    assert "wobbly_tool" in str(caught.value)


def test_a_preset_result_masks_secrets_before_the_model() -> None:
    class _SecretTools(FakeTools):
        async def result(self, key: str, arguments: dict[str, Any]) -> Any:
            return {"endpoint_id": "we_1", "secret": SecretValue("whsec_REAL")}

    adapted = asyncio.run(preset_structured_tool(_app(_SecretTools()), _preset(fixed_kwargs={})))
    result = asyncio.run(adapted.ainvoke({}))
    assert result == {"endpoint_id": "we_1", "secret": SECRET_PLACEHOLDER}


def test_a_preset_park_becomes_the_marker_with_both_id_lists(parkable_run: None) -> None:
    class _ParkingTools(FakeTools):
        async def result(self, key: str, arguments: dict[str, Any]) -> Any:
            return SuspendedInteraction(
                interaction_id="i-caller",
                resume_owner=get_resume_continuation_tool(),
                caller_interaction_ids=["i-caller"],
            )

    adapted = asyncio.run(preset_structured_tool(_app(_ParkingTools()), _preset()))
    marker = read_suspended_interaction_marker(asyncio.run(adapted.ainvoke({})))
    assert marker is not None
    assert marker["interaction_ids"] == ["i-caller"]
    assert marker["caller_interaction_ids"] == ["i-caller"]
    assert marker["resume_owner"] == AGENT_RESUME_TOOL_NAME


def test_a_preset_base_failure_is_a_model_visible_tool_exception(caplog: pytest.LogCaptureFixture) -> None:
    class _FailingTools(FakeTools):
        async def result(self, key: str, arguments: dict[str, Any]) -> Any:
            raise ValueError("base tool down")

    adapted = asyncio.run(preset_structured_tool(_app(_FailingTools()), _preset()))
    with (
        caplog.at_level(logging.WARNING, logger="tai42_agents._internal.lc_tools"),
        pytest.raises(ToolException, match=r"Error calling tool 'my_preset': base tool down"),
    ):
        asyncio.run(adapted.ainvoke({}))
    assert any(
        "my_preset" in r.getMessage() and "example_tool" in r.getMessage() and "base tool down" in r.getMessage()
        for r in caplog.records
    )


def test_a_preset_base_cancellation_passes_through() -> None:
    class _CancelledTools(FakeTools):
        async def result(self, key: str, arguments: dict[str, Any]) -> Any:
            raise asyncio.CancelledError

    adapted = asyncio.run(preset_structured_tool(_app(_CancelledTools()), _preset()))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(adapted.ainvoke({}))


def test_a_preset_undeclared_pause_leaves_the_call_raw() -> None:
    class _UndeclaredTools(FakeTools):
        async def result(self, key: str, arguments: dict[str, Any]) -> Any:
            raise UndeclaredPauseError(key)

    adapted = asyncio.run(preset_structured_tool(_app(_UndeclaredTools()), _preset()))
    with pytest.raises(UndeclaredPauseError):
        asyncio.run(adapted.ainvoke({}))


def test_a_preset_nested_park_with_nothing_chained_is_refused_and_the_door_flows_through() -> None:
    class _NestedTools(FakeTools):
        async def result(self, key: str, arguments: dict[str, Any]) -> Any:
            return SuspendedInteraction(interaction_id="i-nested", resume_owner="nested_driver_resume")

    app = _NestedTools()
    adapted = asyncio.run(preset_structured_tool(_app(app), _preset()))
    bound = DeliveryAddress(tool="conversation_deliver", context=None, thread_id="bridge:acme:alice")
    token = set_park_completion(bound.tool, thread_id=bound.thread_id)
    try:
        with pytest.raises(ToolException, match=r"^Tool 'my_preset' cannot be used inside this run"):
            asyncio.run(adapted.ainvoke({}))
        assert get_park_completion() == bound
    finally:
        reset_park_completion(token)
    # The run cannot park, so the base tool ran with no chain bound.
    assert app.chain_seen == [None]


def test_a_preset_over_a_nested_driver_runs_in_the_chained_scope(parkable_run: None) -> None:
    # A preset's base that drives a nested run which parks: inside a park-capable drive the base
    # body sees a ChainedResume, and the returned park is adopted as chained.
    class _NestedTools(FakeTools):
        async def result(self, key: str, arguments: dict[str, Any]) -> Any:
            return SuspendedInteraction(interaction_id="i-nested", resume_owner="nested_driver_resume")

    app = _NestedTools()
    adapted = asyncio.run(preset_structured_tool(_app(app), _preset()))
    marker = read_suspended_interaction_marker(asyncio.run(adapted.ainvoke({})))
    assert marker is not None
    (routing,) = app.chain_seen
    assert isinstance(routing, ChainedResume)
    assert marker["interaction_id"] == routing.chain_key
    assert is_chained_park_key(marker["interaction_id"])


# -- resolve_tools -----------------------------------------------------------------------------


def test_resolve_orders_live_tools_then_names_then_presets() -> None:
    app = FakeTools()
    out = asyncio.run(resolve_tools(_app(app), ["example_tool"], [client_tool("live")], [_preset()]))
    assert [tool.name for tool in out] == ["live", "example_tool", "my_preset"]


def test_resolve_rejects_duplicate_names() -> None:
    app = FakeTools()
    with pytest.raises(ValueError, match="duplicate tool names"):
        asyncio.run(resolve_tools(_app(app), [], [client_tool("dup"), client_tool("dup")], []))
    with pytest.raises(ValueError, match="duplicate tool names"):
        asyncio.run(resolve_tools(_app(app), [], [client_tool("dup")], [_preset(name="dup", fixed_kwargs={})]))


def test_resolve_refuses_a_live_tool_name_past_the_bound() -> None:
    name = "t" * 65
    with pytest.raises(ToolNameTooLongError, match=name):
        asyncio.run(resolve_tools(_app(FakeTools()), [], [client_tool(name)], []))


def test_resolve_refuses_a_preset_name_past_the_bound() -> None:
    # The preset-name law refuses a 65-character name at validation; a spec built past it is
    # still refused at the bind, never truncated.
    name = "p" * 65
    with pytest.raises(ValueError, match="name"):
        PresetSpec(name=name, base_tool="example_tool")
    unchecked = PresetSpec.model_construct(name=name, description="", base_tool="example_tool", fixed_kwargs={})
    with pytest.raises(ToolNameTooLongError, match=name):
        asyncio.run(resolve_tools(_app(FakeTools()), [], [], [unchecked]))
