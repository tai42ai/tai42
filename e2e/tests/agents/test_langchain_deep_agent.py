"""``langchain_deep_agent`` over the stack: the plan -> subagent -> synthesis loop.

The main agent delegates to a declared subagent through its built-in ``task``
tool (deepagents' ``SubAgentMiddleware``); the subagent runs its own graph and
returns a result, which the main agent then synthesizes into a final answer. The
scripted stub plays all three model turns in order (main delegates, subagent
answers, main synthesizes), and the recorded turns prove the subagent's output
flowed back into the main agent's synthesis.

``langchain_deep_agent`` is a durable-SESSION agent: its settings require a
digest-pinned session image and a run needs a registered sandbox provider even for
a pure model-turn flow (the session is bound at run start). The reference
``build_agents_stack`` loads the agent but wires neither, so this module runs on a
``fresh_stack`` derived from it that adds the deterministic in-process sandbox
provider and the stub session image — the hermetic home for the model-orchestration
property under test, no host subprocess or real image involved."""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from tai42_e2e.llmstub import LlmStub
from tai42_e2e.manifests import build_agents_stack
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import StackConfig, StackResources, TaiStack
from tai42_e2e.variants import Variants

# The agents stack runs no backend worker; skip this module on non-default
# backend legs (they exercise no backend seam).
pytestmark = [
    pytest.mark.backendless,
    # The scripted llm_stub round-trips (script + assert on llm_stub.requests) are the LLM
    # MOCK leg; a real-provider leg is exercised on the e2e creds host, not in CI,
    # so the stub-bound module steps aside when the 'llm' seam is real. Inert in the default
    # mock run — is_real("llm") is False, so collection is byte-for-byte today's.
    pytest.mark.skipif(
        HarnessSettings().is_real("llm"),
        reason="scripted llm_stub is the 'llm' mock leg; the real leg runs on the e2e creds host",
    ),
]

# The deterministic in-process sandbox provider fixture (the same module the sandbox-kind
# suite drives) and a digest-pinned stub session image — inert under the fake provider, but the
# durable-session agent's settings reject a bare tag, so a valid digest is required.
_SANDBOX_FAKE_MODULE = "tai42_e2e_fixtures.sandbox_provider"
_STUB_SESSION_IMAGE = "img@sha256:" + "0" * 64


def _agents_with_deep_session(res: StackResources, variants: Variants) -> StackConfig:
    """``build_agents_stack`` with the deep agent's durable-session prerequisites added: the
    deterministic sandbox provider (bound at run start) and the required digest-pinned session
    image. The reference stack loads ``langchain_deep_agent`` but wires neither, so a run would
    fail loudly (no provider / bare-tag image) without this."""
    config = build_agents_stack(res, variants)
    config.manifest["sandbox_module"] = _SANDBOX_FAKE_MODULE
    config.env["TAI_AGENTS_LANGCHAIN_DEEP_SESSION_IMAGE"] = _STUB_SESSION_IMAGE
    return config


async def test_langchain_deep_agent_plan_subagent_synthesis_over_mcp(
    fresh_stack: Callable[..., TaiStack], llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    finding = f"finding {uniq('sub')}"
    synthesis = f"synthesis {uniq('final')}"
    llm_stub.reset()
    llm_stub.script(
        [
            # Main agent: delegate to the researcher subagent via the task tool.
            {
                "tool_call": {
                    "name": "task",
                    "arguments": {"description": "research the topic", "subagent_type": "researcher"},
                }
            },
            # Researcher subagent: return its finding (no tool calls -> its graph ends).
            {"content": finding},
            # Main agent: synthesize the subagent's finding into the final answer.
            {"content": synthesis},
        ]
    )

    subagents = [
        {
            "name": "researcher",
            "description": "Researches a topic and reports findings.",
            "system_prompt": "You research topics and report concise findings.",
        }
    ]

    stack: TaiStack = fresh_stack(_agents_with_deep_session, resource_kwargs={"llm_base_url": llm_stub.base_url})
    async with stack.mcp() as mcp:
        result = await mcp.call_tool(
            "langchain_deep_agent",
            {"user_message": "research and summarize the topic", "subagents": subagents},
        )

    assert synthesis in json.dumps(result.data), f"langchain_deep_agent did not return the synthesis: {result.data}"
    requests = llm_stub.requests
    # Plan (delegate) + subagent + synthesis = three model round-trips.
    assert len(requests) == 3, f"expected 3 LLM round-trips, saw {len(requests)}"
    # The subagent's finding flowed back into the main agent's synthesis turn.
    assert finding in json.dumps(requests[2]), "the subagent finding never reached the main agent's synthesis"
