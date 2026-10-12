"""What the MCP listing serves for a preset: its own name and description, none of its base
tool's display title or icons, and its base tool's behaviour hints."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from mcp.types import Tool

from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async
from tai42_e2e_fixtures.tool_display import E2E_ECHO_ANNOTATIONS, E2E_ECHO_TITLE

pytestmark = pytest.mark.needs("kind:versioning", "probe-tools")


async def _served(stack: TaiStack, *names: str) -> dict[str, Tool]:
    async def listed() -> dict[str, Tool] | None:
        async with stack.mcp() as mcp:
            tools = {tool.name: tool for tool in await mcp.list_tools() if tool.name in names}
        return tools if len(tools) == len(names) else None

    return await wait_for_async(listed, deadline=30.0, interval=0.5, message=f"{names} never all served")


async def test_a_preset_serves_its_own_identity_and_its_base_tools_hints(
    core_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    name = uniq("preset")
    await core_stack.api().post(
        "/api/presets",
        json={
            "name": name,
            "base_tool": "e2e_echo",
            "description": "Echo a fixed payload.",
            "fixed_kwargs": {"payload": "baked"},
        },
    )
    served = await _served(core_stack, "e2e_echo", name)

    base = served["e2e_echo"]
    assert (base.title, base.annotations) == (E2E_ECHO_TITLE, E2E_ECHO_ANNOTATIONS), base
    preset = served[name]
    assert (preset.description, preset.title, preset.icons) == ("Echo a fixed payload.", None, None), preset
    assert preset.annotations == E2E_ECHO_ANNOTATIONS, preset
