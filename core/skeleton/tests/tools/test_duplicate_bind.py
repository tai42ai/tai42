"""Duplicate tool/agent binds fail loud (``on_duplicate="error"``).

The FastMCP server is constructed with ``on_duplicate="error"``, so a second bind
of an existing tool name raises instead of silently last-write-win; and the agent
registry raises on a duplicate agent name. Every legitimate rebind removes the
name first, so an in-boot duplicate is always a genuine collision.
"""

from __future__ import annotations

import asyncio

import pytest

from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.marketplace.compat import CorePluginBootError


@pytest.fixture(autouse=True)
def _clean_server():
    async def _clear() -> None:
        provider = app._fast_mcp.local_provider
        for tool in list(await provider.list_tools()):
            provider.remove_tool(tool.name)

    asyncio.run(_clear())
    yield
    asyncio.run(_clear())


def test_duplicate_tool_name_bind_raises():
    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True)
            def dup(x: int) -> int:
                """First binding."""
                return x

            with pytest.raises(ValueError, match="already exists"):

                @app.tools.tool(force=True, name="dup")
                def dup_again(x: int) -> int:
                    """Second binding of the same tool name ``dup``."""
                    return x

    asyncio.run(run())


def test_duplicate_agent_name_bind_raises():
    # Two agents-config entries import the same agent module, so the decorator
    # fires twice for one name within a single boot — a genuine collision. The
    # SECOND module cannot load, so boot aborts through the shared abort seam.
    manifest = Manifest.model_validate(
        {
            "agents": [
                {"title": "a1", "module": "tests.agent._fixtures", "include": ["echo_fields"]},
                {"title": "a2", "module": "tests.agent._fixtures2", "include": ["echo_fields"]},
            ]
        }
    )

    async def run() -> None:
        async with app.app_context(manifest):
            pass  # pragma: no cover — start() aborts before the body runs

    with pytest.raises(CorePluginBootError, match="already registered"):
        asyncio.run(run())
