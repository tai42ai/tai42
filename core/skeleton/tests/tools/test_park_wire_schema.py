"""The MCP edge advertises the park markers beside the declared output schema of every tool that can pause.

An MCP client validates ``structuredContent`` against the ``outputSchema`` a tool advertises on
``tools/list``. A tool that can pause answers a park as a reserved marker in ``structuredContent``, so
the edge advertises ``anyOf[declared, suspended-interaction marker, resume-buffered marker]`` for it
(``$defs`` and fastmcp's result-wrap keys kept at the root). The tool's own declared schema stays what
every in-process reader sees. Covered here: the composition itself, the ``tools/list`` advertisement,
what an MCP client reads for a normal result and for a park, and the unchanged in-process readers.
"""

from __future__ import annotations

import asyncio
import copy
from typing import Any

import pytest
from fastmcp import Client
from jsonschema import Draft202012Validator
from mcp.types import TextContent
from pydantic import BaseModel
from tai42_contract.interactions import (
    RESUME_BUFFERED_MARKER_KEY,
    RESUME_BUFFERED_MARKER_SCHEMA,
    SUSPENDED_INTERACTION_MARKER_SCHEMA,
    ResumeBuffered,
    SuspendedInteraction,
    read_resume_buffered_marker,
    read_suspended_interaction_marker,
    suspended_interaction_marker,
)
from tai42_contract.tools import TOOL_META_PAUSES, UndeclaredPauseError
from tai42_kit.utils.data.json_schema_util import JsonSchemaValidationError, validate_against_json_schema

from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest

_MOD = "tests.app._fixtures.suspend_tool"
_STRICT = {"type": "object", "properties": {"v": {"type": "integer"}}, "required": ["v"]}
_PARK = suspended_interaction_marker("p-1", None, None, interaction_ids=["p-1"], caller_interaction_ids=[])


class _Inner(BaseModel):
    n: int


class _Nested(BaseModel):
    inner: _Inner
    label: str


def _manifest() -> Manifest:
    return Manifest.model_validate(
        {
            "extensions_modules": ["tests.app._fixtures.ext_mod"],
            "tools": [
                {
                    "title": "suspend",
                    "module": _MOD,
                    "include": ["make_suspend", "make_buffered"],
                    "extensions": {"make_suspend": [["loud"]]},
                }
            ],
        }
    )


def _compose(declared: dict[str, Any]) -> dict[str, Any]:
    from tai42_skeleton.tools.dispatch_scope import park_admitting_output_schema

    return park_admitting_output_schema(declared)


# -- the composition -------------------------------------------------------------------------


def test_a_plain_object_schema_composes_to_any_of_the_declared_schema_and_the_park_markers() -> None:
    assert _compose(_STRICT) == {"anyOf": [_STRICT, SUSPENDED_INTERACTION_MARKER_SCHEMA, RESUME_BUFFERED_MARKER_SCHEMA]}


def test_defs_move_to_the_composed_root_so_a_ref_still_resolves() -> None:
    declared = {
        "$defs": {"Inner": {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}},
        "type": "object",
        "properties": {"inner": {"$ref": "#/$defs/Inner"}},
        "required": ["inner"],
    }
    composed = _compose(declared)
    assert composed["$defs"] == declared["$defs"]
    assert "$defs" not in composed["anyOf"][0]
    validate_against_json_schema({"inner": {"n": 3}}, composed)
    validate_against_json_schema(_PARK, composed)
    with pytest.raises(JsonSchemaValidationError):
        validate_against_json_schema({"inner": {"n": "three"}}, composed)


def test_a_wrapped_result_schema_keeps_the_wrap_keys_at_the_composed_root() -> None:
    declared = {
        "type": "object",
        "properties": {"result": {"type": "string"}},
        "required": ["result"],
        "x-fastmcp-wrap-result": True,
    }
    composed = _compose(declared)
    assert composed["x-fastmcp-wrap-result"] is True
    assert composed["properties"] == {"result": {"type": "string"}}
    assert composed["anyOf"][0] == declared
    assert "type" not in composed
    assert "required" not in composed


def test_the_composed_schema_admits_the_declared_result_and_both_markers_and_refuses_anything_else() -> None:
    composed = _compose(_STRICT)
    Draft202012Validator.check_schema(composed)
    validate_against_json_schema({"v": 1}, composed)
    validate_against_json_schema(_PARK, composed)
    validate_against_json_schema({RESUME_BUFFERED_MARKER_KEY: {"remaining_ids": ["r1"]}}, composed)
    with pytest.raises(JsonSchemaValidationError):
        validate_against_json_schema({"v": "one"}, composed)


def test_the_composition_never_mutates_the_declared_schema() -> None:
    declared = {
        "$defs": {"Inner": {"type": "object"}},
        "type": "object",
        "properties": {"result": {"$ref": "#/$defs/Inner"}},
        "x-fastmcp-wrap-result": True,
    }
    before = copy.deepcopy(declared)
    _compose(declared)
    assert declared == before


# -- over the MCP edge -----------------------------------------------------------------------


def _bind_shapes() -> None:
    """Register a pausing ``-> str`` tool, a pausing model-returning tool and two non-pausing controls."""

    @app.tools.tool(force=True, meta={TOOL_META_PAUSES: True})
    async def wrap_parker(park: bool = False) -> str:
        """A string tool that can park."""
        if park:
            return SuspendedInteraction(interaction_id="wrap-1")  # type: ignore[return-value]
        return "hello"

    @app.tools.tool(force=True, meta={TOOL_META_PAUSES: True})
    async def nested_parker(park: bool = False) -> _Nested:
        """A model-returning tool that can park."""
        if park:
            return SuspendedInteraction(interaction_id="nested-1")  # type: ignore[return-value]
        return _Nested(inner=_Inner(n=3), label="x")

    @app.tools.tool(force=True, meta={TOOL_META_PAUSES: True}, output_schema=_STRICT)
    async def strict_parker() -> dict:
        """A pausing tool whose result breaks its own declared schema."""
        return {"v": "one"}

    @app.tools.tool(force=True, meta={TOOL_META_PAUSES: True})
    async def schemaless_parker():
        """A pausing tool that declares no output schema."""
        return SuspendedInteraction(interaction_id="free-1")

    @app.tools.tool(force=True, output_schema=_STRICT)
    async def strict_plain() -> dict:
        """A non-pausing tool with a strict schema."""
        return {"v": 1}

    @app.tools.tool(force=True)
    async def nested_plain(arg: _Nested) -> _Nested:
        """A non-pausing tool taking and returning a nested model."""
        return arg

    @app.tools.tool(force=True)
    async def unflagged_buffered():
        """A tool that does not declare it can pause, returning a resume-buffered partial."""
        return ResumeBuffered(remaining_ids=["r1"])


def _over_the_edge(calls: list[tuple[str, dict[str, Any]]]) -> tuple[dict[str, Any], list[Any]]:
    async def run() -> tuple[dict[str, Any], list[Any]]:
        async with app.app_context(_manifest()):
            _bind_shapes()
            async with Client(app._fast_mcp) as client:
                listed = {tool.name: tool.outputSchema for tool in await client.list_tools()}
                results = [await client.call_tool(name, args, raise_on_error=False) for name, args in calls]
            return listed, results

    return asyncio.run(run())


def test_tools_list_advertises_the_park_markers_only_for_a_pausing_tool_with_a_declared_schema() -> None:
    listed, _ = _over_the_edge([])
    # A pausing tool with a declared schema: the composition of exactly that schema.
    assert listed["strict_parker"] == _compose(_STRICT)
    assert listed["make_suspend"]["anyOf"][1:] == [SUSPENDED_INTERACTION_MARKER_SCHEMA, RESUME_BUFFERED_MARKER_SCHEMA]
    # A branch of a pausing tool inherits its schema and pauses too.
    assert listed["make_suspend_loud"] == listed["make_suspend"]
    # A non-pausing tool's advertisement is its declared schema, untouched; a pausing tool that
    # declares none advertises none.
    assert listed["strict_plain"] == _STRICT
    assert listed["schemaless_parker"] is None


def test_a_pausing_string_tool_answers_its_result_and_its_park() -> None:
    _, (normal, parked) = _over_the_edge([("wrap_parker", {}), ("wrap_parker", {"park": True})])
    assert normal.is_error is False
    assert normal.data == "hello"
    assert parked.is_error is False, parked.content
    marker = read_suspended_interaction_marker(parked.structured_content)
    assert marker is not None
    assert marker["interaction_id"] == "wrap-1"


def test_a_pausing_model_tool_answers_its_typed_result_and_its_park() -> None:
    _, (normal, parked) = _over_the_edge([("nested_parker", {}), ("nested_parker", {"park": True})])
    assert normal.is_error is False
    assert normal.structured_content == {"inner": {"n": 3}, "label": "x"}
    assert normal.data.inner.n == 3
    assert parked.is_error is False, parked.content
    marker = read_suspended_interaction_marker(parked.structured_content)
    assert marker is not None
    assert marker["interaction_id"] == "nested-1"


def test_a_pausing_tools_result_that_breaks_its_declared_schema_is_still_refused() -> None:
    _, (result,) = _over_the_edge([("strict_parker", {})])
    assert result.is_error is True
    texts = [block.text for block in result.content if isinstance(block, TextContent)]
    assert texts[0].startswith("Output validation error")


def test_a_branch_over_a_pausing_tool_answers_the_park_marker() -> None:
    _, (result,) = _over_the_edge([("make_suspend_loud", {})])
    assert result.is_error is False, result.content
    marker = read_suspended_interaction_marker(result.structured_content)
    assert marker is not None
    assert marker["interaction_id"] == "i-preset"


def test_a_resume_buffered_partial_crosses_the_mcp_edge_as_its_marker() -> None:
    _, (result,) = _over_the_edge([("make_buffered", {})])
    assert result.is_error is False, result.content
    assert read_resume_buffered_marker(result.structured_content) == {"remaining_ids": ["r1", "r2"]}


def test_a_resume_buffered_partial_from_a_tool_that_does_not_declare_it_can_pause_is_refused() -> None:
    _, (result,) = _over_the_edge([("unflagged_buffered", {})])
    assert result.is_error is True
    texts = [block.text for block in result.content if isinstance(block, TextContent)]
    assert texts == [str(UndeclaredPauseError("unflagged_buffered"))]


def test_a_resume_buffered_partial_records_the_same_run_outcome_at_the_mcp_edge_as_in_process(
    preset_manager_restored: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    async def run() -> Any:
        async with app.app_context(_manifest()):
            await app.preset_manager.register("buffered_preset", "make_buffered", {}, [], "d")
            assert isinstance(await app.tools.run_tool("buffered_preset", {}), ResumeBuffered)
            async with Client(app._fast_mcp) as client:
                return await client.call_tool("buffered_preset", {}, raise_on_error=False)

    result = asyncio.run(run())
    assert read_resume_buffered_marker(result.structured_content) == {"remaining_ids": ["r1", "r2"]}
    # The run stays parked on both doors; the still-open sibling ids are no park of this dispatch.
    assert terminals == [{"outcome": "parked", "interaction_id": None}] * 2


def test_in_process_readers_keep_the_declared_schema() -> None:
    # The composition exists only on the MCP ``tools/list`` wire: the bound Tool object, the
    # in-process tool listing and the tool-schema operation all keep the declared schema.
    from tai42_skeleton.operations.tools import tool_schema

    async def run() -> tuple[Any, Any, Any, Any]:
        async with app.app_context(_manifest()):
            _bind_shapes()
            bound = await app.tools.get_tool("strict_parker")
            async with Client(app._fast_mcp) as client:
                await client.list_tools()
            listed = (await app.tools.get_tools())["strict_parker"]
            view = await tool_schema("strict_parker")
            return bound, listed, view, await app.tools.get_tool("strict_parker")

    bound, listed, view, bound_after = asyncio.run(run())
    assert bound.output_schema == _STRICT
    assert listed.output_schema == _STRICT
    assert view["output"] == _STRICT
    assert bound_after is bound
    assert bound_after.output_schema == _STRICT


def test_in_process_readers_see_the_schema_the_mcp_edge_advertises_with_its_refs_inlined() -> None:
    # Every in-process reader lists through the server's own schema shaping (``$ref`` inlined), so
    # a non-pausing tool reads exactly what the MCP edge advertises, and a pausing tool reads its
    # declared schema, inlined, without the park markers the MCP edge adds.
    from fastmcp.utilities.json_schema import dereference_refs

    from tai42_skeleton.operations.tools import tool_schema

    async def run() -> dict[str, Any]:
        async with app.app_context(_manifest()):
            _bind_shapes()
            async with Client(app._fast_mcp) as client:
                wire = {tool.name: tool for tool in await client.list_tools()}
            listed = await app.tools.get_tools()
            handles = {h.name: h for h in await app.tools.get_client_tools(["nested_plain", "nested_parker"])}
            return {
                "wire": wire,
                "listed": listed,
                "views": {name: await tool_schema(name) for name in ("nested_plain", "nested_parker")},
                "handles": handles,
                "declared": (await app.tools.get_tool("nested_parker")).output_schema,
            }

    seen = asyncio.run(run())
    wire, listed, views, handles = seen["wire"], seen["listed"], seen["views"], seen["handles"]

    plain = wire["nested_plain"]
    assert "$defs" not in plain.inputSchema
    assert listed["nested_plain"].parameters == plain.inputSchema
    assert listed["nested_plain"].output_schema == plain.outputSchema
    assert views["nested_plain"]["input"] == plain.inputSchema
    assert views["nested_plain"]["output"] == plain.outputSchema
    assert handles["nested_plain"].input_schema == plain.inputSchema

    parker = wire["nested_parker"]
    declared_inlined = dereference_refs(seen["declared"])
    assert "$defs" in seen["declared"]
    assert listed["nested_parker"].output_schema == declared_inlined
    assert views["nested_parker"]["output"] == declared_inlined
    assert listed["nested_parker"].parameters == parker.inputSchema
    assert handles["nested_parker"].input_schema == parker.inputSchema
    assert parker.outputSchema == dereference_refs(_compose(seen["declared"]))
