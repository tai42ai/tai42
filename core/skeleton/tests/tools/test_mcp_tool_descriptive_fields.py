"""A connected server's tool is re-served with the descriptive fields it advertises.

The bound tool, and every extension branch of it, carries the server's ``title``,
``annotations`` and ``icons`` verbatim. The server's ``_meta`` and ``execution`` describe its
own edge and are never re-served: a server key under the platform's ``tai42/`` namespace must
not steer the platform's own facets, and the platform's edge runs the bound tool as a plain call.
"""

from __future__ import annotations

import asyncio

import mcp
import pytest
from mcp.types import Icon, ToolAnnotations, ToolExecution
from tai42_contract.manifest import MCPConfig, TaiMCPConfig

from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest


@pytest.fixture(autouse=True)
def _clean_server():
    """Clear the singleton FastMCP server's tools around each test: it outlives one
    ``app_context``, so a tool a prior test bound would collide with this test's bind."""

    async def _clear() -> None:
        provider = app._fast_mcp.local_provider
        for tool in list(await provider.list_tools()):
            provider.remove_tool(tool.name)

    asyncio.run(_clear())
    yield
    asyncio.run(_clear())


_ANNOTATIONS = ToolAnnotations(title="Annotated listing", readOnlyHint=True, openWorldHint=False)
_ICONS = [Icon(src="https://icons.example/list.svg", mimeType="image/svg+xml", sizes=["48x48"])]


def _advertised() -> mcp.types.Tool:
    return mcp.types.Tool(
        name="listing",
        title="List Things",
        description="List the things the account can see.",
        inputSchema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        annotations=_ANNOTATIONS,
        icons=_ICONS,
        execution=ToolExecution(taskSupport="required"),
        _meta={"tai42/hidden": True, "vendor/k": 1},
    )


def _cfg() -> TaiMCPConfig:
    return TaiMCPConfig(title="svc", include=[], config=MCPConfig(type="http", url="http://x/mcp"))


def test_bound_tool_and_its_branch_reserve_title_annotations_icons_but_not_meta_or_execution():
    async def run() -> None:
        manifest = Manifest.model_validate({"extensions_modules": ["tests.app._fixtures.ext_output"]})
        async with app.app_context(manifest):
            bound_manifest = app._tool_binding._require_manifest()
            bound_manifest.include_title_mcp_tools_map["svc"] = frozenset()
            bound_manifest.exclude_title_mcp_tools_map["svc"] = frozenset()
            app._tool_binding.register_tool_info("svc_listing", [["passw"]])

            app._tool_binding.mcp_tools(_cfg(), [_advertised()], frozenset())

            served = {tool.name: tool.to_mcp_tool() for tool in await app._fast_mcp.list_tools()}
            assert {"svc_listing", "svc_listing_passw"} <= served.keys()
            for name in ("svc_listing", "svc_listing_passw"):
                tool = served[name]
                assert tool.title == "List Things", name
                assert tool.annotations == _ANNOTATIONS, name
                assert tool.icons == _ICONS, name
                meta = tool.meta or {}
                assert "tai42/hidden" not in meta, (name, meta)
                assert "vendor/k" not in meta, (name, meta)
                assert tool.execution is None, (name, tool.execution)

    asyncio.run(run())


_HINTS = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


def test_a_preset_and_its_branch_serve_the_connected_tools_hints_without_its_title():
    async def run() -> None:
        manifest = Manifest.model_validate({"extensions_modules": ["tests.app._fixtures.ext_output"]})
        async with app.app_context(manifest):
            bound_manifest = app._tool_binding._require_manifest()
            bound_manifest.include_title_mcp_tools_map["svc"] = frozenset()
            bound_manifest.exclude_title_mcp_tools_map["svc"] = frozenset()
            app._tool_binding.mcp_tools(_cfg(), [_advertised()], frozenset())

            await app.preset_manager.register("listing_preset", "svc_listing", {}, [["passw"]], description="A preset.")

            served = {tool.name: tool.to_mcp_tool() for tool in await app._fast_mcp.list_tools()}
            for name in ("listing_preset", "listing_preset_passw"):
                tool = served[name]
                assert (tool.title, tool.icons) == (None, None), (name, tool)
                assert tool.annotations == _HINTS, (name, tool)

    asyncio.run(run())


def test_a_preset_over_a_projected_destructive_operation_keeps_destructive_hint():
    async def run() -> None:
        async with app.app_context(Manifest.model_validate({"api_tools": {"enabled": True}})):
            operation = (await app.tools.get_tool("set_tool_extensions")).to_mcp_tool()
            assert operation.annotations is not None
            assert operation.annotations.destructiveHint is True
            preset = await app._preset_bind("set_tool_extensions", {}, name="op_preset", description="A preset.")
            served = preset.to_mcp_tool()
            assert served.annotations is not None
            assert served.annotations.destructiveHint is True

    asyncio.run(run())
