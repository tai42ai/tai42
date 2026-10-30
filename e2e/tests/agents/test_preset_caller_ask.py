"""A caller ask behind a preset parks the ``tools_agent`` as a CALLER ask at the sync run-tool door.

A ``tools_agent`` run carries an inline preset over the ``e2e_caller_ask`` probe tool (an async
``ask`` addressed to the run's caller). The scripted model calls the preset, the probe parks, and
the preset's park marker carries both id lists — so the agent's park names its caller subset and
the sync door answers with the caller-ask entries (``{"asks": [...]}``), never a silent user park.

Runs over ``agent_route_park_stack`` — the durable-checkpoint, auth-ON stack the caller-ask probe is
registered on (a ``to="caller"`` ask needs an authenticated caller and a durable checkpoint).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e.llmstub import LlmStub
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack

pytestmark = [
    pytest.mark.backendless,
    pytest.mark.skipif(
        HarnessSettings().is_real("llm"),
        reason="scripted llm_stub is the 'llm' mock leg; the real leg runs under TAI_E2E_REAL",
    ),
    pytest.mark.needs(
        "kind:identity",
        "probe-tools",
        "helper:llm",
        "setting:agent:tools_agent",
        "setting:tai42_e2e_fixtures.door_agent",
    ),
]

_PRESET = "ask_the_caller"


@pytest.mark.needs("setting:checkpoint:redis")
async def test_a_caller_ask_behind_a_preset_answers_with_the_caller_ask_at_the_sync_door(
    agent_route_park_stack: tuple[TaiStack, str], llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    stack, _root_token = agent_route_park_stack
    question = uniq("preset-question")
    llm_stub.reset()
    llm_stub.script([{"tool_call": {"name": _PRESET, "arguments": {}}}])

    preset = {
        "name": _PRESET,
        "description": "Ask the caller a question.",
        "base_tool": "e2e_caller_ask",
        "fixed_kwargs": {"question": question, "expiry_seconds": 3600},
    }
    body = {
        "tool_name": "tools_agent",
        "arguments": {"user_message": {"content": uniq("preset-open")}, "presets": [preset]},
        # A ``to="caller"`` ask indexes its park on the run's subject, so the door names one.
        "subject": {"target_kind": "agent", "target_name": "tools_agent", "kind": "job", "key": uniq("preset-subj")},
    }
    answer = await stack.api().post("/api/run-tool", json=body, expect=200, retry_on_reloading=True)

    assert answer["kind"] == "asks", answer
    (entry,) = answer["asks"]
    assert entry["to"] == "caller"
    assert entry["question"] == question
