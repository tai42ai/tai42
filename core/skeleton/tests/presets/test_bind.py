"""The preset bind kernel: a hidden/fixed FastMCP transform of a base tool.

Pins the typed-schema rebuild — the remaining arguments keep the base tool's real
schema, each baked ``fixed_kwargs`` key is HIDDEN and FIXED (a caller that passes
it is rejected, never overriding the baked constant), and the preset's
``description`` is set on the transformed tool. The kernel is the single point
every preset builds through, so these guarantees reach every preset.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from fastmcp.tools import Tool
from tai42_contract.app import TaiApp
from tai42_kit.utils.data.json_schema_util import JsonSchemaValidationError

from tai42_skeleton.presets.bind import deep_merge, preset_bind


def _base_tool() -> Tool:
    def weather(city: str, units: str = "metric") -> str:
        """Report the weather."""
        return f"{city}:{units}"

    return Tool.from_function(weather, name="weather")


def _dict_base_tool() -> Tool:
    def report(city: str, units: str = "metric") -> dict:
        """Report the weather as a structured object."""
        return {"city": city, "units": units, "temp": 20}

    return Tool.from_function(report, name="report")


def _agent_base_tool() -> Tool:
    def agent_run(city: str, response_format: dict[str, Any] | None = None) -> dict:
        """A stand-in agent run tool that echoes the response_format it was baked with."""
        return {"city": city, "response_format": response_format}

    return Tool.from_function(agent_run, name="agent_run")


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
        # Only the KEYS are read by the bind kernel (agent-base detection).
        return {name: object() for name in self._names}


class _FakePresets:
    """Exposes the input-schema support lookup the bind kernel reads."""

    def __init__(self, support: object | None) -> None:
        self._support = support

    def input_schema_support(self, base_tool: str) -> object | None:
        return self._support


class _FakeApp:
    def __init__(self, tool: Tool, agent_names: tuple[str, ...] = (), support: object | None = None) -> None:
        self.tools = _FakeTools(tool)
        self.agents = _FakeAgents(agent_names)
        self.presets = _FakePresets(support)


def _app(tool: Tool, agent_names: tuple[str, ...] = (), support: object | None = None) -> TaiApp:
    return cast("TaiApp", _FakeApp(tool, agent_names, support))


def _payload_base_tool() -> Tool:
    def runner(payload: dict, image: str = "x") -> dict:
        """A base tool with a structured payload argument."""
        return {"payload": payload, "image": image}

    return Tool.from_function(runner, name="runner")


async def test_bind_hides_baked_key_and_keeps_typed_schema():
    tool = await preset_bind(
        _app(_base_tool()),
        "weather",
        {"units": "imperial"},
        name="paris_weather",
        description="Weather in Paris",
    )
    assert tool.name == "paris_weather"
    schema = tool.to_mcp_tool().inputSchema
    props = schema.get("properties", {})
    # The baked key is removed from the exposed schema; the remaining argument
    # keeps its real typed schema (name + type), not one opaque ``params`` blob.
    assert "units" not in props
    assert props["city"]["type"] == "string"


async def test_bind_sets_description():
    # The preset's ``description`` is applied to the transformed tool; a preset
    # carries NO native tags (grouping is the tool_meta overlay's job).
    tool = await preset_bind(_app(_base_tool()), "weather", {}, name="p", description="Desc")
    assert tool.description == "Desc"


async def test_baked_kwarg_is_fixed_and_rejected_when_passed():
    tool = await preset_bind(_app(_base_tool()), "weather", {"units": "imperial"}, name="p")
    # The baked value is used as a fixed constant...
    ok = await tool.run({"city": "paris"})
    assert ok.structured_content == {"result": "paris:imperial"}
    # ...and a caller that passes the baked key is REJECTED, never overriding it.
    with pytest.raises(TypeError):
        await tool.run({"city": "paris", "units": "metric"})


async def test_baked_partial_binds_positional_through_presented_signature():
    # A baked MIDDLE arg: base ``(a, b, c)`` with ``b`` baked presents ``(a, c)``.
    # A positional call must map each value to the param it names in the PRESENTED
    # signature (1->a, 3->c), never onto the hidden baked slot.
    from fastmcp.tools.tool_transform import TransformedTool

    from tai42_skeleton.tools.binding import _baked_partial

    def base(a: int, b: int = 0, c: int = 0) -> dict:
        """base"""
        return {"a": a, "b": b, "c": c}

    tool = await preset_bind(_app(Tool.from_function(base, name="base")), "base", {"b": 99}, name="baked")
    # ``preset_bind`` returns a ``TransformedTool`` at runtime (typed as ``Tool``).
    partial = _baked_partial(cast(TransformedTool, tool))

    assert partial(1, 3) == {"a": 1, "b": 99, "c": 3}
    assert partial(a=1, c=3) == {"a": 1, "b": 99, "c": 3}
    # Passing the baked key is still rejected.
    with pytest.raises(TypeError):
        partial(1, b=5)


# -- output_schema front-door: plain-tool DECLARE + VALIDATE -----------------


async def test_plain_tool_output_schema_is_advertised_and_conforming_passes():
    schema = {
        "type": "object",
        "properties": {"temp": {"type": "integer", "minimum": 0}},
        "required": ["temp"],
    }
    tool = await preset_bind(_app(_dict_base_tool()), "report", {}, name="r", output_schema=schema)
    # The authored schema is advertised as the bound tool's output schema.
    assert tool.output_schema == schema
    # A conforming result passes through unchanged.
    ok = await tool.run({"city": "paris"})
    assert ok.structured_content is not None
    assert ok.structured_content["temp"] == 20


async def test_plain_tool_output_schema_violation_raises_on_constraint_keyword():
    # ``minimum`` is a constraint keyword the faithful validator enforces: the tool
    # returns ``temp=20``, which violates ``minimum: 100``.
    schema = {
        "type": "object",
        "properties": {"temp": {"type": "integer", "minimum": 100}},
        "required": ["temp"],
    }
    tool = await preset_bind(_app(_dict_base_tool()), "report", {}, name="r", output_schema=schema)
    with pytest.raises(JsonSchemaValidationError):
        await tool.run({"city": "paris"})


# -- output_schema front-door: agent-base FORCE (bake response_format) --------


async def test_agent_base_bakes_response_format_and_injects_preset_name_as_title():
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}  # no title
    tool = await preset_bind(
        _app(_agent_base_tool(), agent_names=("agent_run",)),
        "agent_run",
        {},
        name="my_preset",
        output_schema=schema,
    )
    # Advertised output schema stays the authored, title-free value.
    assert tool.output_schema == schema
    assert "title" not in (tool.output_schema or {})
    # response_format is baked (hidden) — removed from the exposed input schema.
    assert "response_format" not in tool.to_mcp_tool().inputSchema.get("properties", {})
    # The baked response_format carries the preset name injected as its title.
    out = await tool.run({"city": "paris"})
    assert out.structured_content is not None
    baked = out.structured_content["response_format"]
    assert baked["title"] == "my_preset"
    assert baked["properties"] == schema["properties"]


async def test_agent_base_preserves_an_authored_title():
    schema = {"type": "object", "title": "MyAnswer", "properties": {"answer": {"type": "string"}}}
    tool = await preset_bind(
        _app(_agent_base_tool(), agent_names=("agent_run",)),
        "agent_run",
        {},
        name="my_preset",
        output_schema=schema,
    )
    out = await tool.run({"city": "paris"})
    # An authored title is preserved, never overwritten by the preset name.
    assert out.structured_content is not None
    assert out.structured_content["response_format"]["title"] == "MyAnswer"


async def test_agent_base_does_not_double_validate():
    # The agent path attaches NO validation wrapper (the agent's own drain is the
    # only validator). The echo result lacks the required ``answer`` field, so a
    # validation wrapper WOULD raise — the agent path must return it unchanged.
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
    tool = await preset_bind(
        _app(_agent_base_tool(), agent_names=("agent_run",)),
        "agent_run",
        {},
        name="p",
        output_schema=schema,
    )
    out = await tool.run({"city": "paris"})
    assert out.structured_content is not None
    assert out.structured_content["city"] == "paris"


# -- input_schema mechanism ----------------------------------------------------


class _Support:
    """A minimal PresetInputSchemaSupport stand-in naming the payload argument."""

    def __init__(self, payload_arg: str) -> None:
        self.payload_arg = payload_arg


async def test_input_schema_routes_validated_object_into_payload_arg():
    authored = {
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "string"}},
        "required": ["a"],
    }
    tool = await preset_bind(
        _app(_payload_base_tool(), support=_Support("payload")),
        "runner",
        {"image": "pinned"},
        name="p",
        input_schema=authored,
    )
    # The exposed tool advertises the AUTHORED schema as its own input contract.
    assert tool.parameters == authored
    # The caller's validated object is delivered under ``payload`` on the base call,
    # alongside the baked ``image``.
    out = await tool.run({"a": 1, "b": "hi"})
    assert out.structured_content == {"payload": {"a": 1, "b": "hi"}, "image": "pinned"}


async def test_input_schema_rejects_invalid_caller_object_loudly_before_routing():
    # The exposed tool advertises the authored schema, but a custom transform_fn
    # bypasses FastMCP's own argument validation — the kernel MUST validate the
    # caller's object against the authored schema itself. A missing-required,
    # wrong-typed, or additionalProperties-forbidden field is rejected LOUDLY as a
    # bad caller call (FastMCP's ValidationError), never routed into ``payload_arg``.
    from fastmcp.exceptions import ValidationError as FastMCPValidationError

    authored = {
        "type": "object",
        "required": ["a"],
        "properties": {"a": {"type": "integer"}},
        "additionalProperties": False,
    }
    base = _payload_base_tool()
    tool = await preset_bind(
        _app(base, support=_Support("payload")),
        "runner",
        {"image": "pinned"},
        name="p",
        input_schema=authored,
    )

    # Missing the required "a".
    with pytest.raises(FastMCPValidationError):
        await tool.run({})
    # Wrong type for "a".
    with pytest.raises(FastMCPValidationError):
        await tool.run({"a": "notint"})
    # An extra field the schema forbids (additionalProperties: false).
    with pytest.raises(FastMCPValidationError):
        await tool.run({"a": 1, "zzz": 9})

    # A valid object IS routed into payload_arg, alongside the baked image.
    out = await tool.run({"a": 1})
    assert out.structured_content == {"payload": {"a": 1}, "image": "pinned"}


async def test_input_schema_over_unsupported_base_is_loud():
    with pytest.raises(ValueError, match="does not accept a preset input_schema"):
        await preset_bind(
            _app(_payload_base_tool(), support=None),
            "runner",
            {},
            name="p",
            input_schema={"type": "object"},
        )


# -- deep_merge (pure): caller-wins-per-key, recurse only into dicts on BOTH sides -----


def test_deep_merge_fills_gaps_and_caller_wins_per_key_including_nested():
    # Keys only in baked fill in; keys only in caller pass through; a key in both whose
    # values are BOTH dicts recurses; caller wins per-key inside the nested dict too.
    baked = {"a": 1, "b": {"c": 2, "keep": 0}}
    caller = {"b": {"d": 3, "keep": 9}, "e": 4}
    assert deep_merge(baked, caller) == {"a": 1, "b": {"c": 2, "d": 3, "keep": 9}, "e": 4}


def test_deep_merge_lists_replace_not_concat_and_mismatched_types_replace():
    # Lists REPLACE (never concatenate); a dict-vs-scalar mismatch replaces with the
    # caller's value; a scalar-vs-dict mismatch likewise takes the caller's dict.
    assert deep_merge({"xs": [1, 2]}, {"xs": [3]}) == {"xs": [3]}
    assert deep_merge({"x": {"nested": 1}}, {"x": 5}) == {"x": 5}
    assert deep_merge({"x": 5}, {"x": {"nested": 1}}) == {"x": {"nested": 1}}


def test_deep_merge_does_not_mutate_inputs():
    baked = {"b": {"c": 2}}
    caller = {"b": {"d": 3}}
    deep_merge(baked, caller)
    assert baked == {"b": {"c": 2}}
    assert caller == {"b": {"d": 3}}


def test_deep_merge_deep_copies_baked_only_subtrees():
    # A baked-only subtree (no caller key overriding it) must be DEEP-COPIED into the result,
    # never aliased: ``baked`` is a bind's shared defaults reused across calls, so a consumer
    # that mutates the merged object must not be able to poison them.
    baked = {"a": 1, "nested": {"deep": {"k": "pristine"}}}
    merged = deep_merge(baked, {"e": 4})
    # A consumer scribbles on the baked-only subtree of the merged result.
    merged["nested"]["deep"]["k"] = "poisoned"
    merged["nested"]["added"] = True
    # The shared defaults are untouched, and a SECOND merge still sees them pristine.
    assert baked == {"a": 1, "nested": {"deep": {"k": "pristine"}}}
    assert deep_merge(baked, {"e": 5})["nested"] == {"deep": {"k": "pristine"}}


# -- input_schema with a BAKED PAYLOAD DEFAULT: deep-merge caller over defaults --------


async def test_baked_payload_defaults_fill_gaps_and_caller_wins_per_key():
    # Baked payload defaults {a:1, b:{c:2}}; caller {b:{d:3}, e:4} deep-merges OVER them
    # (caller-wins-per-key, nested), and the merged object reaches the base under payload.
    authored = {"type": "object"}
    tool = await preset_bind(
        _app(_payload_base_tool(), support=_Support("payload")),
        "runner",
        {"payload": {"a": 1, "b": {"c": 2}}, "image": "pinned"},
        name="p",
        input_schema=authored,
    )
    out = await tool.run({"b": {"d": 3}, "e": 4})
    assert out.structured_content == {
        "payload": {"a": 1, "b": {"c": 2, "d": 3}, "e": 4},
        "image": "pinned",
    }


async def test_baked_payload_caller_wins_and_lists_replace():
    authored = {"type": "object"}
    tool = await preset_bind(
        _app(_payload_base_tool(), support=_Support("payload")),
        "runner",
        {"payload": {"a": 1, "b": {"c": 2}, "xs": [1, 2]}},
        name="p",
        input_schema=authored,
    )
    # Caller wins per-key (top-level and nested); lists REPLACE, not concatenate.
    out = await tool.run({"a": 9, "b": {"c": 8}, "xs": [3]})
    assert out.structured_content is not None
    assert out.structured_content["payload"] == {"a": 9, "b": {"c": 8}, "xs": [3]}


async def test_baked_payload_only_call_equals_baked_defaults():
    # A caller supplying {} yields exactly the baked defaults.
    authored = {"type": "object"}
    baked = {"a": 1, "b": {"c": 2}}
    tool = await preset_bind(
        _app(_payload_base_tool(), support=_Support("payload")),
        "runner",
        {"payload": baked},
        name="p",
        input_schema=authored,
    )
    out = await tool.run({})
    assert out.structured_content is not None
    assert out.structured_content["payload"] == baked


def _accumulating_payload_base_tool() -> Tool:
    def runner(payload: dict, image: str = "x") -> dict:
        """A base tool whose consumer MUTATES the payload it receives, appending to a
        baked-only nested list. If the merge aliased the shared defaults, the list would
        ACCUMULATE across calls."""
        payload["nested"].setdefault("log", []).append("call")
        return {"payload": payload, "image": image}

    return Tool.from_function(runner, name="runner")


async def test_baked_payload_defaults_survive_a_mutating_consumer():
    # The forwarded merged payload's baked-only subtree is a deep copy, so a consumer that
    # mutates it cannot poison the bind's shared defaults for a LATER call.
    authored = {"type": "object"}
    tool = await preset_bind(
        _app(_accumulating_payload_base_tool(), support=_Support("payload")),
        "runner",
        {"payload": {"nested": {"c": 2}}},  # ``nested`` is baked-only (no caller key overrides it)
        name="p",
        input_schema=authored,
    )
    first = await tool.run({"e": 1})
    assert first.structured_content is not None
    assert first.structured_content["payload"]["nested"]["log"] == ["call"]
    # The consumer mutated the FIRST call's forwarded payload; a SECOND call still starts from
    # pristine baked defaults — the log does not accumulate, and the baked ``c`` is intact.
    second = await tool.run({"e": 2})
    assert second.structured_content is not None
    assert second.structured_content["payload"]["nested"] == {"c": 2, "log": ["call"]}


async def test_no_baked_payload_behavior_is_unchanged():
    # When the payload arg is NOT baked, the caller's validated object is delivered
    # verbatim (no merge step); byte-identical to the pre-defaults path.
    authored = {
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "string"}},
        "required": ["a"],
    }
    tool = await preset_bind(
        _app(_payload_base_tool(), support=_Support("payload")),
        "runner",
        {"image": "pinned"},
        name="p",
        input_schema=authored,
    )
    out = await tool.run({"a": 1, "b": "hi"})
    assert out.structured_content == {"payload": {"a": 1, "b": "hi"}, "image": "pinned"}


async def test_non_dict_baked_payload_is_a_loud_authoring_error_at_bind():
    # A non-dict baked payload under an input_schema fails LOUDLY at BIND (reachable via
    # the authoring dry-run bake → a 400 at save), never deferred to the first call.
    with pytest.raises(ValueError, match="non-dict default for the payload argument"):
        await preset_bind(
            _app(_payload_base_tool(), support=_Support("payload")),
            "runner",
            {"payload": "not-a-dict"},
            name="p",
            input_schema={"type": "object"},
        )


async def test_merged_object_violation_is_loud_with_preset_attribution():
    from fastmcp.exceptions import ValidationError as FastMCPValidationError

    # The caller's own object passes (``n`` is optional), but the baked default n=-1
    # violates ``minimum: 0`` once merged — a defaults-induced violation, attributed to
    # the preset by name (NOT reported as a caller error).
    authored = {
        "type": "object",
        "properties": {"n": {"type": "integer", "minimum": 0}, "m": {"type": "integer"}},
    }
    tool = await preset_bind(
        _app(_payload_base_tool(), support=_Support("payload")),
        "runner",
        {"payload": {"n": -1}},
        name="strict_preset",
        input_schema=authored,
    )
    with pytest.raises(FastMCPValidationError, match="strict_preset"):
        await tool.run({"m": 5})


async def test_caller_object_violation_is_verbatim_even_with_baked_payload():
    from fastmcp.exceptions import ValidationError as FastMCPValidationError

    # With a baked payload present, a CALLER mistake still surfaces as the caller error
    # (verbatim), never mis-attributed to the preset's baked defaults.
    authored = {
        "type": "object",
        "properties": {"a": {"type": "integer"}},
        "additionalProperties": False,
    }
    tool = await preset_bind(
        _app(_payload_base_tool(), support=_Support("payload")),
        "runner",
        {"payload": {"a": 1}},
        name="p",
        input_schema=authored,
    )
    with pytest.raises(FastMCPValidationError, match="caller input does not match"):
        await tool.run({"zzz": 9})
