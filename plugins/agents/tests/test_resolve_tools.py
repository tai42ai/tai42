"""Tests for ``resolve_tools`` — the shared tool-input resolver.

Covers name resolution, preset binding (base tool + fixed kwargs, with fixed
keys hidden from the exposed schema), the duplicate-name guard, and the bound
callable's park contract: a park sentinel becomes the reserved marker, and a nested
run's is refused. A fake tool facet (mirroring
``tai42_app.tools``) stands in for a live app — no LLM.
"""

import asyncio
from typing import cast

import pytest
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from pydantic.v1 import BaseModel as V1BaseModel
from tai42_contract.agent.base import PresetSpec
from tai42_contract.secrets import SECRET_PLACEHOLDER, SecretValue
from tai42_contract.tools import AppTools

from tai42_agents._internal.resolve_tools import resolve_tools


def make_tool(name, props=None, required=None):
    async def call_tool(**kwargs):
        return kwargs

    args_schema = {"type": "object", "properties": props or {}}
    # ``required=None`` omits the key entirely (the no-``required``-key case);
    # a list pins it explicitly.
    if required is not None:
        args_schema["required"] = required
    return StructuredTool.from_function(
        func=None,
        coroutine=call_tool,
        name=name,
        description="t",
        args_schema=args_schema,
    )


class FakeTools:
    """A stand-in for the app's ``tools`` facet: only the two members
    ``resolve_tools`` reaches (``get_client_tools`` / ``run_tool``)."""

    def __init__(self):
        self.run_tool_calls = []

    async def get_client_tools(self, names=None):
        # Base tools a preset can bind to, keyed by name -> (props, required).
        # A ``required`` of ``None`` omits the schema's ``required`` key.
        props = {"example_config": {"type": "object"}, "example_config_kwargs": {"type": "object"}}
        schemas = {
            # "example_tool" carries no ``required`` key.
            "example_tool": (props, None),
            # "example_required" marks both runtime args as required.
            "example_required": (props, ["example_config", "example_config_kwargs"]),
        }
        return [make_tool(n, *schemas.get(n, (None, None))) for n in (names or [])]

    async def run_tool(self, key, arguments):
        self.run_tool_calls.append((key, arguments))
        return {"ok": True, "args": arguments}


def _app_tools(fake: FakeTools) -> AppTools:
    return cast(AppTools, fake)


def test_resolves_names_and_live_tools():
    app = FakeTools()
    live = make_tool("live")
    out = asyncio.run(resolve_tools(_app_tools(app), ["a", "b"], [live], []))
    assert [t.name for t in out] == ["live", "a", "b"]


def test_preset_hides_fixed_keys_and_binds():
    app = FakeTools()
    preset = PresetSpec(
        name="my_preset",
        description="run a preset",
        base_tool="example_tool",
        fixed_kwargs={"example_config": {"nodes": []}},
    )
    out = asyncio.run(resolve_tools(_app_tools(app), [], [], [preset]))
    (tool,) = out
    assert tool.name == "my_preset"
    # fixed key removed from the exposed schema; runtime key kept.
    assert "example_config" not in tool.args
    assert "example_config_kwargs" in tool.args
    # invoking merges fixed kwargs under runtime kwargs and calls the base tool.
    asyncio.run(tool.arun({"example_config_kwargs": {"x": 1}}))
    key, arguments = app.run_tool_calls[-1]
    assert key == "example_tool"
    assert arguments == {"example_config": {"nodes": []}, "example_config_kwargs": {"x": 1}}


def test_preset_over_a_parking_tool_stamps_the_park_marker():
    # R3: a preset over ANY parking tool must surface the park. The base tool's async park
    # returns the SuspendedInteraction sentinel through run_tool; the preset adapter (a plain
    # langchain tool) converts it to the reserved contract park marker, so the in-graph park
    # middleware recognizes the park by RESULT shape — exactly as the direct client-tool adapter.
    from tai42_contract.interactions import (
        SuspendedInteraction,
        get_resume_continuation_tool,
        read_suspended_interaction_marker,
        reset_resume_continuation_tool,
        set_resume_continuation_tool,
    )

    class _ParkingTools(FakeTools):
        async def run_tool(self, key, arguments):
            self.run_tool_calls.append((key, arguments))
            # Faithful to ``ask_user(mode="async")``: the park names the continuation bound
            # around this drive as its owner, so this run may adopt it.
            return SuspendedInteraction(interaction_id="i-preset", resume_owner=get_resume_continuation_tool())

    app = _ParkingTools()
    preset = PresetSpec(name="my_preset", base_tool="example_tool", fixed_kwargs={"example_config": {"nodes": []}})
    (tool,) = asyncio.run(resolve_tools(_app_tools(app), [], [], [preset]))
    token = set_resume_continuation_tool("agent_resume")
    try:
        result = asyncio.run(tool.arun({"example_config_kwargs": {"x": 1}}))
    finally:
        reset_resume_continuation_tool(token)
    marker = read_suspended_interaction_marker(result)
    assert marker is not None
    assert marker["interaction_id"] == "i-preset"
    # The owner rides the wire form the claim point later reads.
    assert marker["resume_owner"] == "agent_resume"


class _SecretReturningTools:
    """A ``tools`` facet whose base tool returns a ``SecretValue``-bearing dict,
    so the preset adapter's masking of the model-visible result can be exercised."""

    async def get_client_tools(self, names=None):
        return [make_tool(n) for n in (names or [])]

    async def run_tool(self, key, arguments):
        return {"endpoint_id": "we_1", "secret": SecretValue("whsec_REAL")}


def test_preset_result_masks_secrets_before_the_model():
    """The preset tool adapter this plugin registers hands langchain a masked
    result: a ``SecretValue`` in a tool return becomes the placeholder, so the
    model, checkpoint, and callback trace never see the real secret."""
    app = cast(AppTools, _SecretReturningTools())
    preset = PresetSpec(name="p", description="d", base_tool="example_tool", fixed_kwargs={})
    (tool,) = asyncio.run(resolve_tools(app, [], [], [preset]))
    result = asyncio.run(tool.arun({}))
    assert result == {"endpoint_id": "we_1", "secret": SECRET_PLACEHOLDER}
    assert "whsec_REAL" not in repr(result)


def test_preset_cannot_override_a_fixed_key():
    """A model that supplies a fixed key does not override the bound value."""
    app = FakeTools()
    preset = PresetSpec(
        name="my_preset",
        description="run a preset",
        base_tool="example_tool",
        fixed_kwargs={"example_config": {"nodes": []}},
    )
    (tool,) = asyncio.run(resolve_tools(_app_tools(app), [], [], [preset]))
    asyncio.run(tool.arun({"example_config": {"nodes": ["evil"]}, "example_config_kwargs": {"x": 1}}))
    _key, arguments = app.run_tool_calls[-1]
    assert arguments["example_config"] == {"nodes": []}  # bound value immutable


def test_preset_keeps_a_required_runtime_key_required():
    """A required base-tool arg that is not fixed stays required on the bound
    preset tool, so the model cannot silently omit it."""
    app = FakeTools()
    preset = PresetSpec(
        name="my_preset",
        description="run a preset",
        base_tool="example_required",
        fixed_kwargs={"example_config": {"nodes": []}},
    )
    (tool,) = asyncio.run(resolve_tools(_app_tools(app), [], [], [preset]))
    assert isinstance(tool.args_schema, dict)
    assert tool.args_schema["required"] == ["example_config_kwargs"]


def test_preset_drops_a_fixed_required_key():
    """A required base-tool arg that the preset fixes is removed from the
    exposed schema's ``required`` list."""
    app = FakeTools()
    preset = PresetSpec(
        name="my_preset",
        description="run a preset",
        base_tool="example_required",
        fixed_kwargs={"example_config": {"nodes": []}},
    )
    (tool,) = asyncio.run(resolve_tools(_app_tools(app), [], [], [preset]))
    assert isinstance(tool.args_schema, dict)
    assert "example_config" not in tool.args_schema["required"]


def test_preset_base_schema_without_required_key():
    """A base tool whose schema carries no ``required`` key binds to an empty
    ``required`` list rather than raising."""
    app = FakeTools()
    preset = PresetSpec(
        name="my_preset",
        description="run a preset",
        base_tool="example_tool",
        fixed_kwargs={"example_config": {"nodes": []}},
    )
    (tool,) = asyncio.run(resolve_tools(_app_tools(app), [], [], [preset]))
    assert isinstance(tool.args_schema, dict)
    assert tool.args_schema["required"] == []


def test_rejects_duplicate_names():
    app = FakeTools()
    a = make_tool("dup")
    b = make_tool("dup")
    with pytest.raises(ValueError, match="duplicate tool names"):
        asyncio.run(resolve_tools(_app_tools(app), [], [a, b], []))

    preset = PresetSpec(name="dup", base_tool="example_tool", fixed_kwargs={})
    with pytest.raises(ValueError, match="duplicate tool names"):
        asyncio.run(resolve_tools(_app_tools(app), [], [make_tool("dup")], [preset]))


class _SchemaTools:
    """A ``tools`` facet serving one base tool with a caller-chosen ``args_schema``."""

    def __init__(self, args_schema):
        self._args_schema = args_schema

    async def get_client_tools(self, names=None):
        async def call_tool(**kwargs):
            return kwargs

        tool = StructuredTool.from_function(
            func=None,
            coroutine=call_tool,
            name="base",
            description="t",
            args_schema={"type": "object", "properties": {}},
        )
        tool.args_schema = self._args_schema
        return [tool]

    async def run_tool(self, name, arguments):
        return arguments


def _preset() -> PresetSpec:
    return PresetSpec(name="p", description="d", base_tool="base", fixed_kwargs={})


def test_preset_reads_required_from_a_pydantic_args_schema():
    """A base tool declaring its schema as a pydantic model keeps its required list."""

    class Schema(BaseModel):
        keep: str
        drop: str = "d"

    app = cast(AppTools, _SchemaTools(Schema))
    (tool,) = asyncio.run(resolve_tools(app, [], [], [_preset()]))
    assert isinstance(tool.args_schema, dict)
    assert tool.args_schema["required"] == ["keep"]


def test_preset_keeps_an_aliased_required_field_required():
    """An aliased pydantic field declared required stays required on the bound
    preset tool. ``base_tool.args`` keys a field by its name, so the exposed
    ``required`` carries the field name and the tool is callable with it."""

    class Schema(BaseModel):
        session_id: str = Field(alias="sessionId")

    app = cast(AppTools, _SchemaTools(Schema))
    (tool,) = asyncio.run(resolve_tools(app, [], [], [_preset()]))
    assert isinstance(tool.args_schema, dict)
    assert tool.args_schema["required"] == ["session_id"]
    assert "session_id" in tool.args
    assert asyncio.run(tool.arun({"session_id": "s"})) == {"session_id": "s"}


def test_preset_base_tool_without_an_args_schema_requires_nothing():
    """A base tool that declares no schema requires nothing, rather than erroring."""
    app = cast(AppTools, _SchemaTools(None))
    (tool,) = asyncio.run(resolve_tools(app, [], [], [_preset()]))
    assert isinstance(tool.args_schema, dict)
    assert tool.args_schema["required"] == []


def test_preset_base_tool_with_an_unsupported_args_schema_raises():
    """An ``args_schema`` of an unexpected type is refused loudly, never defaulted away,
    and the error names the value's own type."""
    app = cast(AppTools, _SchemaTools(42))
    with pytest.raises(TypeError, match="unsupported args_schema") as excinfo:
        asyncio.run(resolve_tools(app, [], [], [_preset()]))
    assert "of type int" in str(excinfo.value)


def test_preset_base_tool_with_an_unsupported_args_schema_class_names_the_class():
    """An unsupported ``args_schema`` supplied as a class — a pydantic-v1 model, say —
    is named by the class itself, not by its metaclass (``ModelMetaclass``), which would
    tell the reader nothing about which schema was rejected."""

    class LegacySchema(V1BaseModel):
        keep: str

    app = cast(AppTools, _SchemaTools(LegacySchema))
    with pytest.raises(TypeError, match="unsupported args_schema") as excinfo:
        asyncio.run(resolve_tools(app, [], [], [_preset()]))
    assert "of type LegacySchema" in str(excinfo.value)
    assert "ModelMetaclass" not in str(excinfo.value)


def test_preset_base_tool_with_an_unsupported_args_schema_function_names_the_type():
    """A non-class value that carries its own ``__name__`` — a function, say — is
    named by its type, not by that ``__name__``: the message reports what kind of
    value was rejected, never the value's own label."""

    def my_helper() -> None: ...

    app = cast(AppTools, _SchemaTools(my_helper))
    with pytest.raises(TypeError, match="unsupported args_schema") as excinfo:
        asyncio.run(resolve_tools(app, [], [], [_preset()]))
    assert "of type function" in str(excinfo.value)
    assert "my_helper" not in str(excinfo.value)


def _named_preset() -> PresetSpec:
    return PresetSpec(name="p", description="d", base_tool="wobbly_tool", fixed_kwargs={})


@pytest.mark.parametrize(
    "malformed_required",
    ["session_id", None, ["ok", 123]],
    ids=["bare-string", "none", "list-with-non-string"],
)
def test_preset_base_tool_with_malformed_required_raises(malformed_required):
    """A hand-authored JSON-schema ``required`` that is not a list of names is refused
    loudly — a bare string would be walked character by character and silently downgrade a
    mandatory argument to optional — and the error names the offending base tool."""
    args_schema = {"type": "object", "properties": {"keep": {"type": "string"}}, "required": malformed_required}
    app = cast(AppTools, _SchemaTools(args_schema))
    with pytest.raises(TypeError, match="malformed args_schema 'required'") as excinfo:
        asyncio.run(resolve_tools(app, [], [], [_named_preset()]))
    assert "wobbly_tool" in str(excinfo.value)


def test_preset_reads_required_from_a_well_formed_dict_args_schema():
    """A well-formed JSON-schema ``required`` list of names passes the guard, and its
    names survive onto the exposed schema."""
    args_schema = {"type": "object", "properties": {"keep": {"type": "string"}}, "required": ["keep"]}
    app = cast(AppTools, _SchemaTools(args_schema))
    (tool,) = asyncio.run(resolve_tools(app, [], [], [_named_preset()]))
    assert isinstance(tool.args_schema, dict)
    assert tool.args_schema["required"] == ["keep"]


def test_preset_accepts_a_tuple_required():
    """A ``required`` supplied as a tuple is accepted — ``StructuredTool`` preserves
    either shape — and its surviving names land on the exposed schema as a list."""
    args_schema = {"type": "object", "properties": {"keep": {"type": "string"}}, "required": ("keep",)}
    app = cast(AppTools, _SchemaTools(args_schema))
    (tool,) = asyncio.run(resolve_tools(app, [], [], [_named_preset()]))
    assert isinstance(tool.args_schema, dict)
    assert tool.args_schema["required"] == ["keep"]


def test_preset_park_refusal_keeps_its_own_typed_message():
    # The park-ownership refusal is the ADAPTER's own guard, not the base tool's failure: it
    # raises its own typed ToolException and is not re-wrapped by the body's error handler.
    from langchain_core.tools import ToolException
    from tai42_contract.interactions import SuspendedInteraction

    class _NestedParkTools(FakeTools):
        async def run_tool(self, key, arguments):
            self.run_tool_calls.append((key, arguments))
            return SuspendedInteraction(interaction_id="i-nested", resume_owner="nested_driver_resume")

    app = _NestedParkTools()
    preset = PresetSpec(name="my_preset", description="run a preset", base_tool="example_tool", fixed_kwargs={})
    (tool,) = asyncio.run(resolve_tools(_app_tools(app), [], [], [preset]))

    with pytest.raises(ToolException) as caught:
        asyncio.run(tool.arun({"example_config_kwargs": {"x": 1}}))
    message = str(caught.value)
    assert message.startswith("Tool 'my_preset' cannot be used inside this run")
    assert "different run's resume binding" in message
    # The refusal names no owner value (the bound continuation is a public claim string).
    assert "'nested_driver_resume'" not in message
    # Its own typed message, not the body-failure wrap: no second "Error calling tool" prefix.
    assert "Error calling tool" not in message


def test_preset_park_refusal_fires_on_the_delivery_scoped_copy():
    # The two ownership rules compose on the ONE object the model is given: resolution hands
    # back a delivery-scoped copy (its body runs with the park-completion binding cleared), and
    # the park-adoption refusal is inside that body — so a nested driver reached through a
    # preset can neither claim the agent's deferred-answer ADDRESS nor hand the agent a park it
    # would never be resumed for. The caller's own binding survives the dispatch untouched.
    from langchain_core.tools import ToolException
    from tai42_contract.interactions import (
        SuspendedInteraction,
        get_park_completion,
        reset_park_completion,
        set_park_completion,
    )

    seen = []

    class _NestedParkTools(FakeTools):
        async def run_tool(self, key, arguments):
            seen.append(get_park_completion())
            return SuspendedInteraction(interaction_id="i-nested", resume_owner="nested_driver_resume")

    bound = ("conversation_deliver", {"thread_id": "bridge:acme:alice"})
    preset = PresetSpec(name="my_preset", description="run a preset", base_tool="example_tool", fixed_kwargs={})
    (tool,) = asyncio.run(resolve_tools(_app_tools(_NestedParkTools()), [], [], [preset]))

    token = set_park_completion(*bound)
    try:
        with pytest.raises(ToolException, match=r"different run's resume binding"):
            asyncio.run(tool.arun({"example_config_kwargs": {"x": 1}}))
        assert get_park_completion() == bound
    finally:
        reset_park_completion(token)
    # The base tool ran with NO completion bound (delivery scope), and the refusal still fired.
    assert seen == [(None, None)]
