"""The preset bind kernel's input map and output stage.

An input map reshapes the caller's validated object into the base tool's arguments (reading the
stored fixed kwargs as ``$fixed``) and never overrides a fixed value; the output stage maps and
then validates the base's result, refusing loudly with text that never quotes a secret.
"""

from __future__ import annotations

from typing import Any, cast

import mcp.types
import pytest
from fastmcp.exceptions import ToolError
from fastmcp.exceptions import ValidationError as FastMCPValidationError
from fastmcp.tools import Tool
from tai42_contract.app import TaiApp
from tai42_contract.presets import PresetInputSchemaSupport
from tai42_contract.secrets import SecretValue

from tai42_skeleton.presets import bind as bind_module
from tai42_skeleton.presets.bind import merge_fixed, preset_bind
from tai42_skeleton.presets.maps import PresetMapError
from tai42_skeleton.tools.binding.result import _serialize_result, _tool_result_value
from tai42_skeleton.tools.binding.secret_tool import _SecretRevealingTool
from tai42_skeleton.tools.reveal_gate import InprocessRevealGate, inprocess_reveal_gate


class _FakeTools:
    def __init__(self, tool: Tool) -> None:
        self._tool = tool

    async def get_tool(self, key: str) -> Tool:
        assert key == self._tool.name
        return self._tool


class _FakeAgents:
    def __init__(self, names: tuple[str, ...]) -> None:
        self._names = names

    def all_agents(self) -> dict[str, Any]:
        return {name: object() for name in self._names}


class _FakePresets:
    def __init__(self, support: dict[str, PresetInputSchemaSupport]) -> None:
        self._support = support

    def input_schema_support(self, base_tool: str) -> PresetInputSchemaSupport | None:
        return self._support.get(base_tool)


class _FakeApp:
    def __init__(self, tool: Tool, agent_names: tuple[str, ...], support: dict[str, PresetInputSchemaSupport]) -> None:
        self.tools = _FakeTools(tool)
        self.agents = _FakeAgents(agent_names)
        self.presets = _FakePresets(support)


def _app(
    tool: Tool, agent_names: tuple[str, ...] = (), support: dict[str, PresetInputSchemaSupport] | None = None
) -> TaiApp:
    return cast("TaiApp", _FakeApp(tool, agent_names, support or {}))


def _runner() -> Tool:
    def runner(payload: dict, image: str = "x") -> dict:
        """Echo what it was handed."""
        return {"payload": payload, "image": image}

    return Tool.from_function(runner, name="runner")


def _report() -> Tool:
    def report(city: str, units: str = "metric") -> dict:
        """Report the weather as an object."""
        return {"city": city, "units": units, "temp": 20}

    return Tool.from_function(report, name="report")


def _agent_base() -> Tool:
    def agent_run(city: str, response_format: dict[str, Any] | None = None) -> dict:
        """A stand-in agent run tool that echoes the response_format it was handed."""
        return {"city": city, "response_format": response_format}

    return Tool.from_function(agent_run, name="agent_run")


def _payload_agent_base() -> Tool:
    def agent_run(payload: dict, response_format: dict[str, Any] | None = None) -> dict:
        """A stand-in agent run tool taking its input under one payload argument."""
        return {"payload": payload, "response_format": response_format}

    return Tool.from_function(agent_run, name="agent_run")


def _vault() -> Tool:
    def vault(account: str) -> dict:
        """Return a secret-bearing value."""
        return {"account": account, "token": SecretValue("tok-x")}

    return _SecretRevealingTool.from_function(vault, name="vault")


def _failing_connector() -> Tool:
    def connector(query: str) -> mcp.types.CallToolResult:
        """A mounted connector whose upstream call failed."""
        return mcp.types.CallToolResult(
            content=[mcp.types.TextContent(type="text", text="hub-err:upstream down")], isError=True
        )

    return _SecretRevealingTool.from_function(connector, name="connector")


async def _in_process(tool: Tool, arguments: dict[str, Any]) -> Any:
    """Run ``tool`` the way the in-process dispatch does: armed gate, then the stowed payload or the reduction."""
    gate = InprocessRevealGate()
    token = inprocess_reveal_gate.set(gate)
    try:
        result = await tool.run(arguments)
    finally:
        inprocess_reveal_gate.reset(token)
    if gate.has_payload:
        return gate.payload
    return _tool_result_value(result)


async def _mcp_edge(tool: Tool, arguments: dict[str, Any]) -> mcp.types.CallToolResult:
    """Run ``tool`` the way the MCP edge does: no gate, the result turned into the wire ``CallToolResult``."""
    reply = (await tool.run(arguments)).to_mcp_result()
    if isinstance(reply, mcp.types.CallToolResult):
        return reply
    if isinstance(reply, tuple):
        content, structured = reply
        return mcp.types.CallToolResult(content=content, structuredContent=structured)
    return mcp.types.CallToolResult(content=reply)


_PLACE_SCHEMA = {
    "type": "object",
    "properties": {"place": {"type": "string"}},
    "required": ["place"],
    "additionalProperties": False,
}


# -- input map -----------------------------------------------------------------------------------


async def test_input_map_reshapes_the_caller_object_and_reads_fixed():
    tool = await preset_bind(
        _app(_runner()),
        "runner",
        {"image": "pinned"},
        name="p",
        input_schema=_PLACE_SCHEMA,
        input_map="{payload: {where: .place, img: $fixed.image}}",
    )
    assert tool.parameters == _PLACE_SCHEMA
    out = await tool.run({"place": "xy"})
    assert out.structured_content == {"payload": {"where": "xy", "img": "pinned"}, "image": "pinned"}


async def test_input_map_reads_fixed_in_its_stored_form(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("P_IMAGE", "resolved-image")
    tool = await preset_bind(
        _app(_runner()),
        "runner",
        {"image": "!ENV ${P_IMAGE}"},
        name="p",
        input_schema=_PLACE_SCHEMA,
        input_map="{payload: {img: $fixed.image}}",
    )
    out = await tool.run({"place": "xy"})
    assert out.structured_content == {"payload": {"img": "!ENV ${P_IMAGE}"}, "image": "resolved-image"}


async def test_input_map_validates_the_caller_object_first():
    tool = await preset_bind(
        _app(_runner()), "runner", {}, name="p", input_schema=_PLACE_SCHEMA, input_map="{payload: .}"
    )
    with pytest.raises(FastMCPValidationError, match="caller input does not match the preset's input_schema"):
        await tool.run({"place": 3})


async def test_input_map_must_produce_an_object():
    tool = await preset_bind(_app(_runner()), "runner", {}, name="p", input_schema=_PLACE_SCHEMA, input_map=".place")
    with pytest.raises(ToolError) as caught:
        await tool.run({"place": "xy"})
    assert str(caught.value) == "preset 'p' input map must produce an object of the base tool's arguments, got str"


async def test_input_map_cannot_set_a_fixed_key():
    tool = await preset_bind(
        _app(_runner()),
        "runner",
        {"image": "pinned"},
        name="p",
        input_schema=_PLACE_SCHEMA,
        input_map='{payload: {}, image: "other"}',
    )
    with pytest.raises(ToolError) as caught:
        await tool.run({"place": "xy"})
    assert str(caught.value) == (
        "preset 'p' input map sets image, which the preset fixes; a fixed value cannot be overridden"
    )


async def test_input_map_deep_merges_nested_fixed_objects_and_refuses_a_nested_collision():
    fixed = {"payload": {"opts": {"a": 1}}}
    merged = await preset_bind(
        _app(_runner()),
        "runner",
        fixed,
        name="p",
        input_schema=_PLACE_SCHEMA,
        input_map="{payload: {opts: {b: .place}, where: .place}}",
    )
    out = await merged.run({"place": "xy"})
    assert out.structured_content == {"payload": {"opts": {"a": 1, "b": "xy"}, "where": "xy"}, "image": "x"}

    clash = await preset_bind(
        _app(_runner()),
        "runner",
        fixed,
        name="p",
        input_schema=_PLACE_SCHEMA,
        input_map="{payload: {opts: {a: .place}}}",
    )
    with pytest.raises(ToolError) as caught:
        await clash.run({"place": "xy"})
    assert str(caught.value) == (
        "preset 'p' input map sets payload.opts.a, which the preset fixes; a fixed value cannot be overridden"
    )


def test_merge_fixed_leaves_both_inputs_unchanged():
    mapped = {"payload": {"b": 2}}
    fixed = {"payload": {"a": 1}, "image": "i"}
    assert merge_fixed(mapped, fixed, preset="p") == {"payload": {"a": 1, "b": 2}, "image": "i"}
    assert mapped == {"payload": {"b": 2}}
    assert fixed == {"payload": {"a": 1}, "image": "i"}


@pytest.mark.parametrize("expression", ['error("boom " + $fixed.image)', "empty"])
async def test_input_map_failure_names_the_preset_and_quotes_nothing(expression: str):
    tool = await preset_bind(
        _app(_runner()),
        "runner",
        {"image": "pinned-secret-ish"},
        name="p",
        input_schema=_PLACE_SCHEMA,
        input_map=expression,
    )
    with pytest.raises(ToolError) as caught:
        await tool.run({"place": "xy"})
    assert str(caught.value) == "preset 'p' input map failed"
    assert "pinned" not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


async def test_input_map_timeout_is_a_tool_error(monkeypatch: pytest.MonkeyPatch):
    async def _slow(*args: Any, **kwargs: Any) -> Any:
        raise TimeoutError("jq evaluation exceeded 10s")

    monkeypatch.setattr(bind_module, "run_jq_first", _slow)
    tool = await preset_bind(
        _app(_runner()), "runner", {}, name="p", input_schema=_PLACE_SCHEMA, input_map="{payload: .}"
    )
    with pytest.raises(ToolError, match=r"^preset 'p' input map failed$"):
        await tool.run({"place": "xy"})


async def test_an_input_map_without_an_input_schema_is_refused_at_bind():
    with pytest.raises(PresetMapError, match="an input map needs an input schema"):
        await preset_bind(_app(_runner()), "runner", {}, name="p", input_map="{payload: .}")


async def test_an_input_map_needs_no_payload_support():
    # The fake app registers no input-schema support: the map decides where the caller's data goes.
    tool = await preset_bind(
        _app(_runner()), "runner", {}, name="p", input_schema=_PLACE_SCHEMA, input_map="{payload: ., image: .place}"
    )
    out = await tool.run({"place": "xy"})
    assert out.structured_content == {"payload": {"place": "xy"}, "image": "xy"}


# -- output stage ----------------------------------------------------------------------------------


async def test_output_map_object_on_both_doors_and_no_advertised_schema():
    tool = await preset_bind(_app(_report()), "report", {}, name="r", output_map="{place: .city, t: .temp}")
    assert tool.output_schema is None
    assert await _in_process(tool, {"city": "paris"}) == {"place": "paris", "t": 20}
    edge = await _mcp_edge(tool, {"city": "paris"})
    assert edge.isError is False
    assert edge.structuredContent == {"place": "paris", "t": 20}


async def test_output_map_scalar_rides_the_wrap_form_on_both_doors():
    tool = await preset_bind(_app(_report()), "report", {}, name="r", output_map=".temp")
    assert await _in_process(tool, {"city": "paris"}) == 20
    edge = await _mcp_edge(tool, {"city": "paris"})
    assert edge.structuredContent == {"result": 20}
    assert [block.text for block in edge.content if isinstance(block, mcp.types.TextContent)] == ["20"]


async def test_output_schema_judges_the_mapped_value():
    schema = {"type": "object", "properties": {"t": {"type": "integer", "minimum": 100}}, "required": ["t"]}
    tool = await preset_bind(_app(_report()), "report", {}, name="r", output_map="{t: .temp}", output_schema=schema)
    assert tool.output_schema == schema
    with pytest.raises(ToolError) as caught:
        await _in_process(tool, {"city": "paris"})
    assert str(caught.value) == (
        "preset 'r' output does not match its output_schema: value does not match schema at $.t: "
        "20 is less than the minimum of 100"
    )
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    edge = await _mcp_edge_error(tool, {"city": "paris"})
    assert edge.startswith("preset 'r' output does not match its output_schema: ")


async def _mcp_edge_error(tool: Tool, arguments: dict[str, Any]) -> str:
    with pytest.raises(ToolError) as caught:
        await tool.run(arguments)
    return str(caught.value)


async def test_plain_output_schema_mismatch_is_the_preset_refusal():
    schema = {"type": "object", "properties": {"temp": {"type": "integer", "minimum": 100}}, "required": ["temp"]}
    tool = await preset_bind(_app(_report()), "report", {}, name="r", output_schema=schema)
    with pytest.raises(ToolError, match=r"^preset 'r' output does not match its output_schema: "):
        await tool.run({"city": "paris"})


async def test_output_map_timeout_is_a_tool_error(monkeypatch: pytest.MonkeyPatch):
    from tai42_kit.presets import output_stage as kit_stage

    async def _slow(*args: Any, **kwargs: Any) -> Any:
        raise TimeoutError("jq evaluation exceeded 10s")

    monkeypatch.setattr(kit_stage, "run_jq_first", _slow)
    tool = await preset_bind(_app(_report()), "report", {}, name="r", output_map=".temp")
    with pytest.raises(ToolError, match=r"^preset 'r' output map failed$"):
        await tool.run({"city": "paris"})


async def test_output_map_over_a_secret_is_refused_on_both_doors():
    tool = await preset_bind(_app(_vault()), "vault", {}, name="v", output_map="{t: .token}")
    expected = "preset 'v' output map cannot run over a result that carries a secret"
    with pytest.raises(ToolError) as in_process:
        await _in_process(tool, {"account": "a"})
    assert str(in_process.value) == expected
    assert "tok-x" not in repr(in_process.value)
    with pytest.raises(ToolError) as edge:
        await tool.run({"account": "a"})
    assert str(edge.value) == expected
    assert "tok-x" not in repr(edge.value)


async def test_a_secret_result_that_breaks_its_schema_reads_value_free_text_on_both_doors():
    schema = {"type": "object", "properties": {"token": {"type": "string", "minLength": 6}}}
    tool = await preset_bind(_app(_vault()), "vault", {}, name="v", output_schema=schema)
    expected = (
        "preset 'v' output does not match its output_schema: value does not match schema at $.token: "
        "breaks the schema's 'minLength' rule: 6 (the value is not shown)"
    )
    for run in (_in_process(tool, {"account": "a"}), tool.run({"account": "a"})):
        with pytest.raises(ToolError) as caught:
            await run
        assert str(caught.value) == expected
        assert "tok-x" not in repr(caught.value)
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None


async def test_a_conforming_secret_result_stays_sealed_in_process():
    schema = {"type": "object", "properties": {"token": {"type": "string", "minLength": 3}}}
    tool = await preset_bind(_app(_vault()), "vault", {}, name="v", output_schema=schema)
    value = await _in_process(tool, {"account": "a"})
    assert isinstance(value["token"], SecretValue)


async def test_an_error_result_passes_the_output_stage_untouched():
    schema = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
    for output_map, output_schema in (("{mapped: .}", None), (None, schema)):
        tool = await preset_bind(
            _app(_failing_connector()), "connector", {}, name="c", output_map=output_map, output_schema=output_schema
        )
        result = await tool.run({"query": "q"})
        assert result.is_error is True
        edge = cast("mcp.types.CallToolResult", result.to_mcp_result())
        assert edge.isError is True
        assert [block.text for block in edge.content if isinstance(block, mcp.types.TextContent)] == [
            "hub-err:upstream down"
        ]
        assert _tool_result_value(result) == _serialize_result(result.to_mcp_result())


# -- agent bases -----------------------------------------------------------------------------------


async def test_agent_base_with_an_input_map_and_an_output_schema_bakes_response_format():
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
    city_schema = {"type": "object", "properties": {"c": {"type": "string"}}, "required": ["c"]}
    tool = await preset_bind(
        _app(_agent_base(), ("agent_run",)),
        "agent_run",
        {},
        name="ag",
        input_schema=city_schema,
        input_map="{city: .c}",
        output_schema=schema,
    )
    assert tool.output_schema == schema
    out = await tool.run({"c": "paris"})
    assert out.structured_content == {"city": "paris", "response_format": {**schema, "title": "ag"}}


async def test_agent_base_with_an_output_map_maps_the_answer_and_bakes_no_response_format():
    tool = await preset_bind(_app(_agent_base(), ("agent_run",)), "agent_run", {}, name="ag", output_map="{m: .}")
    out = await tool.run({"city": "paris"})
    assert out.structured_content == {"m": {"city": "paris", "response_format": None}}


async def test_agent_base_with_an_output_schema_only_keeps_the_forced_build():
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
    tool = await preset_bind(_app(_agent_base(), ("agent_run",)), "agent_run", {}, name="ag", output_schema=schema)
    # The forced build has no transform of its own: the agent's drain validates the forced answer.
    out = await tool.run({"city": "paris"})
    assert out.structured_content == {"city": "paris", "response_format": {**schema, "title": "ag"}}


async def test_agent_base_with_a_supported_input_schema_and_an_output_schema_bakes_response_format():
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
    city_schema = {"type": "object", "properties": {"c": {"type": "string"}}, "required": ["c"]}
    tool = await preset_bind(
        _app(_payload_agent_base(), ("agent_run",), {"agent_run": PresetInputSchemaSupport(payload_arg="payload")}),
        "agent_run",
        {},
        name="ag",
        input_schema=city_schema,
        output_schema=schema,
    )
    assert tool.output_schema == schema
    out = await tool.run({"c": "paris"})
    assert out.structured_content == {"payload": {"c": "paris"}, "response_format": {**schema, "title": "ag"}}
