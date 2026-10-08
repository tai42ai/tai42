"""A tools-agent run sees a preset edit, then the preset's deletion, on its very next run.

A run's compiled graph is reused across runs of the same inputs and keyed on the tool-surface
generation, which every tool registration change moves. So a run naming a preset uses the edited
preset on the next run, and a run after the preset is deleted is refused with the unknown-tool
error — never a graph holding the old tool.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from tai42_e2e.llmstub import LlmStub
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack

pytestmark = [
    pytest.mark.backendless,
    pytest.mark.skipif(
        HarnessSettings().is_real("llm"),
        reason="scripted llm_stub is the 'llm' mock leg; the real leg runs on the e2e creds host",
    ),
    pytest.mark.needs("helper:llm", "setting:agent:tools_agent", "probe-tools"),
]


async def _run_naming(stack: TaiStack, llm_stub: LlmStub, preset: str) -> tuple[Any, list[dict]]:
    """One run naming ``preset``, whose scripted model calls it once and then answers."""
    llm_stub.reset()
    llm_stub.script([{"tool_call": {"name": preset, "arguments": {}}}, {"content": "done"}])
    async with stack.mcp() as mcp:
        result = await mcp.call_tool(
            "tools_agent",
            {"user_message": {"content": "call the tool"}, "tool_names": [preset]},
            raise_on_error=False,
            retry_on_reloading=True,
        )
    return result, list(llm_stub.requests)


def _tool_messages(requests: list[dict]) -> str:
    return json.dumps([m for request in requests for m in request["messages"] if m.get("role") == "tool"])


async def test_a_run_uses_the_edited_preset_then_refuses_the_deleted_one(
    agents_stack: TaiStack, llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    name = uniq("surface_preset")
    first, second = uniq("first"), uniq("second")
    api = agents_stack.api()
    await api.post(
        "/api/presets",
        json={
            "name": name,
            "base_tool": "e2e_echo",
            "description": "surface refresh probe",
            "fixed_kwargs": {"payload": first},
        },
    )

    result, requests = await _run_naming(agents_stack, llm_stub, name)
    assert not result.is_error, result
    assert first in _tool_messages(requests)

    await api.post(f"/api/presets/{name}/versions", json={"fixed_kwargs": {"payload": second}})
    result, requests = await _run_naming(agents_stack, llm_stub, name)
    assert not result.is_error, result
    tool_messages = _tool_messages(requests)
    assert second in tool_messages, tool_messages
    assert first not in tool_messages, tool_messages

    await api.delete(f"/api/presets/{name}")
    result, requests = await _run_naming(agents_stack, llm_stub, name)
    assert result.is_error, result
    error = next((getattr(part, "text", "") for part in result.content), "")
    assert name in error, error
    assert requests == [], "a refused run must not reach the model"
