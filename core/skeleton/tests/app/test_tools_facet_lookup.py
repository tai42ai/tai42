"""The tools facet's one-name lookup: ``has_tool`` answers whether a tool is bound to a name."""

from __future__ import annotations

from tai42_skeleton.app import instance
from tai42_skeleton.manifest import Manifest

_MANIFEST = {"tools": [{"title": "fx", "module": "tests.presets._fixtures", "include": ["echo", "weather"]}]}


async def test_has_tool_answers_a_bound_tool_an_unknown_name_and_a_removed_tool() -> None:
    async with instance.app.app_context(Manifest.model_validate(_MANIFEST)):
        tools = instance.app.tools
        assert await tools.has_tool("echo") is True
        assert await tools.has_tool("no-such-tool") is False

        tools.remove_tool("echo")
        assert await tools.has_tool("echo") is False
        assert await tools.has_tool("weather") is True
