"""The input-annotation reader: where a tool's run input can hold a ``TemplatedText`` given by stored id."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from fastmcp.tools.tool_transform import TransformedTool
from pydantic import TypeAdapter, ValidationError

from tai42_skeleton.app import instance
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.operations.reference_graph import kwargs_references
from tai42_skeleton.tools.template_slots import annotated_template_ids, tool_input_annotations

_MANIFEST = {
    "extensions_modules": ["tests.presets._ext_fixtures"],
    "tools": [
        {
            "title": "slots",
            "module": "tests.tools._template_slot_fixtures",
            "include": ["render_items", "shaped_answer", "opaque_payload"],
        },
        {"title": "fx", "module": "tests.presets._fixtures", "include": ["echo"], "extensions": {"echo": [["jqpass"]]}},
        {"title": "refs", "module": "tests.presets._ref_fixtures", "include": ["jq_refs_tool"]},
    ],
    "agents": [{"title": "ag", "module": "tests.routers._authoring_fixtures", "include": ["tools_agent"]}],
}


def _within_app(body: Callable[[], Awaitable[None]]) -> None:
    async def run() -> None:
        async with instance.app.app_context(Manifest.model_validate(_MANIFEST)):
            await body()

    asyncio.run(run())


def test_a_list_field_yields_each_stored_id_by_index() -> None:
    async def body() -> None:
        ids = await annotated_template_ids("render_items", {"items": [{"content": "a"}, {"id": "t/one"}]})
        assert ids == [("items[1]", "t/one")]

    _within_app(body)


def test_a_union_settles_on_the_arm_the_run_settles_on() -> None:
    async def body() -> None:
        by_id = {"question": "q", "response_format": {"id": "schemas/x"}}
        assert await annotated_template_ids("shaped_answer", by_id) == [("response_format", "schemas/x")]
        schema = {"question": "q", "response_format": {"type": "object", "properties": {}}}
        assert await annotated_template_ids("shaped_answer", schema) == []

    _within_app(body)


def test_a_self_nesting_spec_is_read_at_every_depth() -> None:
    async def body() -> None:
        kwargs = {
            "system_prompt": {"id": "prompts/a"},
            "subagents": [
                {"subagents": [{"system_prompt": {"id": "prompts/c"}}], "system_prompt": {"id": "prompts/b"}}
            ],
        }
        assert await annotated_template_ids("tools_agent", kwargs) == [
            ("system_prompt", "prompts/a"),
            ("subagents[0].system_prompt", "prompts/b"),
            ("subagents[0].subagents[0].system_prompt", "prompts/c"),
        ]

    _within_app(body)


def test_an_agents_annotations_carry_its_field_constraints() -> None:
    async def body() -> None:
        annotations = await tool_input_annotations("tools_agent")
        assert annotations is not None
        adapter: TypeAdapter[Any] = TypeAdapter(annotations["max_steps"])
        assert adapter.validate_python(2) == 2
        with pytest.raises(ValidationError):
            adapter.validate_python(0)

    _within_app(body)


def test_a_tool_with_no_python_function_answers_none() -> None:
    async def body() -> None:
        fast_mcp = instance.app._fast_mcp
        view = TransformedTool.from_tool(await instance.app.tools.get_tool("render_items"), name="render_items_view")
        fast_mcp.add_tool(view)
        try:
            assert await tool_input_annotations("render_items_view") is None
            assert await annotated_template_ids("render_items_view", {"items": [{"id": "t/one"}]}) == []
        finally:
            fast_mcp.local_provider.remove_tool("render_items_view")

    _within_app(body)


def test_a_value_the_annotation_refuses_is_a_value_free_error() -> None:
    async def body() -> None:
        prefix = "system_prompt is not a valid value for tool 'tools_agent': "
        with pytest.raises(ValueError, match=f"^{prefix}") as caught:
            await annotated_template_ids("tools_agent", {"system_prompt": 5})
        assert "5" not in str(caught.value).removeprefix(prefix)

    _within_app(body)


def test_a_base_declaring_a_template_extractor_has_its_annotations_unread() -> None:
    async def body() -> None:
        refs = await kwargs_references("jq_refs_tool", {"prompt": {"id": "t/one"}}, "fixed_kwargs", body_root=True)
        assert [(ref.name, ref.registry, ref.site) for ref in refs] == [("jq/missing", "template", "node 'fetch' expr")]

    _within_app(body)


def test_an_extension_branch_presents_its_own_field_beside_the_bases() -> None:
    async def body() -> None:
        annotations = await tool_input_annotations("echo_jqpass")
        assert annotations is not None
        assert list(annotations) == ["text", "jq_expression"]
        ids = await annotated_template_ids("echo_jqpass", {"text": "hi", "jq_expression": {"id": "jq/x"}})
        assert ids == [("jq_expression", "jq/x")]

    _within_app(body)


def test_an_any_field_is_not_read() -> None:
    async def body() -> None:
        assert await annotated_template_ids("opaque_payload", {"payload": {"id": "t/one"}}) == []

    _within_app(body)


def test_a_name_the_registry_does_not_serve_answers_none() -> None:
    async def body() -> None:
        assert await tool_input_annotations("ghost") is None
        assert await annotated_template_ids("ghost", {"system_prompt": {"id": "prompts/a"}}) == []

    _within_app(body)


def test_a_key_that_is_not_a_field_is_not_read() -> None:
    async def body() -> None:
        assert await annotated_template_ids("render_items", {"extra": {"id": "t/one"}}) == []

    _within_app(body)
