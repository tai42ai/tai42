"""An optional argument the caller leaves out of a backend branch never ships to the worker.

A branch over a synthesized signature (an agent run tool, a preset whose input is an authored
schema) receives every optional parameter the caller left out as the omitted-value marker. On a
real backend worker, each leg's job serializer must encode the job and the worker must run the
named tool with its own handling of the omitted argument.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastmcp.exceptions import ToolError

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.needs("kind:backend", "probe-tools", "setting:extension:sync_task")

# The backend worker and both API workers serve a created preset within this.
_PRESET_SERVED_DEADLINE = 15.0


def _tool_text(result: Any) -> str:
    """A tool call's scalar payload as text, from ``data``, structured content or text content."""
    if result.data is not None:
        return str(result.data)
    if result.structured_content is not None:
        return str(result.structured_content)
    return "".join(getattr(block, "text", "") for block in (result.content or []))


def _not_yet_served(exc: ToolError, tool: str) -> bool:
    """A worker mid-reload, or a sibling API worker whose own reload has not bound ``tool`` yet."""
    text = str(exc)
    return "reloading" in text or f"Unknown tool: '{tool}'" in text


async def _call(stack: TaiStack, tool: str, arguments: dict[str, Any]) -> str:
    async with stack.mcp() as mcp:
        result = await mcp.call_tool(tool, arguments)
    return _tool_text(result)


@pytest.mark.timeout(345)
async def test_an_agent_run_tool_left_without_an_optional_field_runs_with_its_default_on_the_worker(
    core_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    marker = uniq("fields")
    assert await _call(core_stack, "e2e_fields_agent_sync_task", {"text": marker}) == f"{marker}:3"


@pytest.mark.timeout(345)
@pytest.mark.needs("mutable")
async def test_a_preset_branch_left_without_an_optional_property_runs_the_presets_map_on_the_worker(
    core_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    name = uniq("omitted_preset")
    await core_stack.api().post(
        "/api/presets",
        json={
            "name": name,
            "base_tool": "e2e_echo",
            "description": "echo of text and an optional suffix",
            "input_schema": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "suffix": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
            "input_map": '{payload: (.text + ":" + (.suffix // "none"))}',
            "extensions": [["sync_task"]],
        },
        retry_on_reloading=True,
    )
    branch = f"{name}_sync_task"
    marker = uniq("omitted")

    async def served() -> str | None:
        try:
            return await _call(core_stack, branch, {"text": marker})
        except ToolError as exc:
            if _not_yet_served(exc, branch):
                return None
            raise

    answer = await wait_for_async(
        served, deadline=_PRESET_SERVED_DEADLINE, message=f"{branch} was never served by every worker"
    )
    assert answer == f"{marker}:none"
