"""A tool result is reduced to JSON once per call: the platform never re-reduces what fastmcp reduced.

The direct path reduces the tool's return once at the seam; the preset path and the MCP edge
read a ``ToolResult`` whose ``structured_content`` fastmcp's own ``ToolResult`` construction
already reduced, so ``_tool_result_value`` returns it as it is.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.tools.base import ToolResult
from mcp.types import ImageContent, TextContent
from pydantic_core import to_jsonable_python

from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.tools.binding import result as result_module
from tai42_skeleton.tools.binding.result import _tool_result_value

_MARK = "reduce-probe"


def _payload() -> dict[str, Any]:
    return {"mark": _MARK, "rows": [{"id": i, "tags": ["a", "b"], "nested": {"n": i}} for i in range(5)]}


# -- the value equals the reduction ---------------------------------------------------------------


def test_an_unwrapped_structured_result_is_returned_as_fastmcp_reduced_it() -> None:
    result = ToolResult(structured_content=_payload())
    value = _tool_result_value(result)
    assert value == to_jsonable_python(result.structured_content)
    assert value is result.structured_content


def test_a_wrapped_result_is_unwrapped_as_fastmcp_reduced_it() -> None:
    result = ToolResult(structured_content={"result": [1, {"a": 2}]}, meta={"fastmcp": {"wrap_result": True}})
    value = _tool_result_value(result)
    assert value == [1, {"a": 2}]
    assert result.structured_content is not None
    assert value is result.structured_content["result"]


def test_a_text_only_result_is_its_text() -> None:
    assert _tool_result_value(ToolResult(content=[TextContent(type="text", text="hi")])) == "hi"
    two = ToolResult(content=[TextContent(type="text", text="a"), TextContent(type="text", text="b")])
    assert _tool_result_value(two) == ["a", "b"]


def test_a_media_result_is_its_wire_blocks() -> None:
    image = ImageContent(type="image", data="aGk=", mimeType="image/png")
    assert _tool_result_value(ToolResult(content=[image])) == to_jsonable_python(image)


# -- one full-tree reduction per call -----------------------------------------------------------


class _ReductionCounter:
    """Counts the seam's own ``to_jsonable_python`` passes over the probe payload."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, value: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(value, dict) and value.get("mark") == _MARK:
            self.calls += 1
        return to_jsonable_python(value, *args, **kwargs)


@pytest.fixture
def seam_reductions(monkeypatch: pytest.MonkeyPatch) -> _ReductionCounter:
    counter = _ReductionCounter()
    monkeypatch.setattr(result_module, "to_jsonable_python", counter)
    return counter


async def probe_payload() -> dict:
    """Return the probe payload."""
    return _payload()


def test_the_direct_path_reduces_once(seam_reductions: _ReductionCounter) -> None:
    async def run() -> Any:
        async with app.app_context(Manifest.model_validate({})):
            app.tools.tool(force=True)(probe_payload)
            return await app.tools.run_tool("probe_payload", {})

    assert asyncio.run(run()) == _payload()
    assert seam_reductions.calls == 1


def test_the_preset_path_adds_no_reduction_to_fastmcps(seam_reductions: _ReductionCounter) -> None:
    async def run() -> Any:
        async with app.app_context(Manifest.model_validate({})):
            app.tools.tool(force=True)(probe_payload)
            await app.preset_manager.register("probe_preset", "probe_payload", {}, [], "probe preset")
            try:
                return await app.tools.run_tool("probe_preset", {})
            finally:
                await app.preset_manager.remove("probe_preset")

    assert asyncio.run(run()) == _payload()
    assert seam_reductions.calls == 0


def test_a_nested_dispatch_reduces_once_per_level(seam_reductions: _ReductionCounter) -> None:
    async def outer() -> dict:
        """Dispatch the preset over the probe and return it with a new object of its own."""
        inner = await app.tools.run_tool("probe_preset", {})
        return {"mark": _MARK, "inner": inner}

    async def run() -> Any:
        async with app.app_context(Manifest.model_validate({})):
            app.tools.tool(force=True)(probe_payload)
            app.tools.tool(force=True)(outer)
            await app.preset_manager.register("probe_preset", "probe_payload", {}, [], "probe preset")
            try:
                return await app.tools.run_tool("outer", {})
            finally:
                await app.preset_manager.remove("probe_preset")

    assert asyncio.run(run()) == {"mark": _MARK, "inner": _payload()}
    # The outer tool's own return is reduced at its own seam; the inner preset level adds none.
    assert seam_reductions.calls == 1


def test_the_mcp_edge_observes_without_a_reduction_and_the_wire_result_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, seam_reductions: _ReductionCounter
) -> None:
    observed: list[Any] = []
    wire_before: list[Any] = []
    real_value = result_module._tool_result_value

    def recording_value(result: ToolResult) -> Any:
        wire_before.append(to_jsonable_python(result.structured_content))
        value = real_value(result)
        observed.append(value)
        return value

    monkeypatch.setattr(result_module, "_tool_result_value", recording_value)

    async def run() -> Any:
        async with app.app_context(Manifest.model_validate({})):
            app.tools.tool(force=True)(probe_payload)
            async with Client(app._fast_mcp) as client:
                return await client.call_tool("probe_payload", {})

    result = asyncio.run(run())
    assert observed == [_payload()]
    # The wire result equals what the edge observed before the binding updates ran.
    assert result.structured_content == wire_before[0] == _payload()
    assert seam_reductions.calls == 0
