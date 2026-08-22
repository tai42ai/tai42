"""``langchain_deep_agent`` over the REAL direct/host ``sandbox-local`` provider.

The deep agent's durable scratch backend roots its file tree at
``{session.workspace_path}/project`` (never a hardcoded ``/workspace``), so a run drives the
built-in ``write_file`` / ``read_file`` tools straight onto the direct provider's host-dir
workspace. A write-then-read round-trip proves the deep agent is provider-agnostic and that
its file ops resolve workspace-relative against ``sandbox-local`` — it would FAIL under the
old hardcoded ``/workspace/project`` root. Fully deterministic (the scripted LLM stub drives
the two file-tool turns; no extra host runtime).

This deterministic leg drives a TOOL-FACE run (no ``thread_id``): its scratch is a fresh
ephemeral host workspace, which is all that is needed to exercise the workspace-relative
resolution over the direct provider. The cross-session HOST-DIR DURABILITY of a persistent
workspace is proven directly against the provider in ``test_sandbox_local_durability.py``.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from tai42_e2e.llmstub import LlmStub
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack

# The stack runs no backend worker; the scripted ``llm_stub`` round-trips are the LLM MOCK
# leg, so the module steps aside when the 'llm' seam is real (that leg runs on the creds host).
pytestmark = [
    pytest.mark.backendless,
    pytest.mark.skipif(
        HarnessSettings().is_real("llm"),
        reason="scripted llm_stub is the 'llm' mock leg; the real leg runs on the e2e creds host",
    ),
]


async def test_deep_agent_scratch_round_trips_through_the_local_provider(
    sandbox_local_deep_stack: TaiStack, llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    token = uniq("scratch")
    final = f"stored {uniq('done')}"
    llm_stub.reset()
    llm_stub.script(
        [
            # Write the token into the durable scratch (lands under {workspace_path}/project).
            {"tool_call": {"name": "write_file", "arguments": {"file_path": "/notes.txt", "content": token}}},
            # Read it straight back from the SAME workspace-relative path.
            {"tool_call": {"name": "read_file", "arguments": {"file_path": "/notes.txt"}}},
            {"content": final},
        ]
    )

    async with sandbox_local_deep_stack.mcp() as mcp:
        result = await mcp.call_tool(
            "langchain_deep_agent", {"user_message": "write the token to a file then read it back"}
        )

    assert final in json.dumps(result.data), f"the deep agent did not return the final content: {result.data}"
    requests = llm_stub.requests
    # write_file turn + read_file turn + synthesis = three model round-trips.
    assert len(requests) == 3, f"expected 3 LLM round-trips, saw {len(requests)}"
    # The token is nowhere in the prompt, so it can only reach the third completion as the
    # read_file tool result — proving the write and its read resolved to the SAME host-dir
    # workspace under the direct provider.
    assert token in json.dumps(requests[2]), "the read-back scratch never reached the model over the local provider"
