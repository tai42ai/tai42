"""The deep agent's scratch is a LIVE durable shell over the sandbox volume; the caller-facing
tier is ephemeral by security design.

The ``StateBackend`` -> ``SandboxSessionBackend`` swap (§B2) makes the deep agent's built-in
filesystem tools AND its built-in ``execute`` shell one live surface over a real sandbox
WORKSPACE volume (dormant under ``StateBackend``, where ``execute`` is inert and the file tools
live in graph state). Two reachable truths of that swap:

* THE LIVE DURABLE SHELL — within one run, the ``execute`` shell writes a file and ``read_file``
  reads it back: the two built-ins share ONE real filesystem (the session volume), which is only
  possible because the scratch is a live sandbox shell, not per-tool graph state.

* THE CALLER TIER IS EPHEMERAL — a durable-workspace agent NEVER derives workspace identity from
  caller input (``claude_code``/``langchain_deep_agent`` deliberately expose NO ``thread_id``
  field; workspace identity arrives ONLY as a trusted param from the conversation-bridge turn).
  So a run over the caller-facing MCP tool-face gets a FRESH EPHEMERAL workspace: a second run on
  the SAME ``langgraph_config`` thread does NOT read the first's file (the workspace does not
  persist), even though the langgraph CHECKPOINT history DOES persist across the two turns.

The persistent-workspace tier (a scratch file SURVIVING across two TRUSTED-THREAD turns) rides
the conversation-bridge door (``astream(thread_id=<trusted>)``); it is proven at the plugin unit
level (``tests/test_langchain_deep_agent_durable.py``), and no e2e stack composes a bridge door
WITH a sandbox provider, so it is not asserted here (recorded as a foundation gap).
"""

from __future__ import annotations

import json
from collections.abc import Callable

from tai42_e2e.llmstub import LlmStub
from tai42_e2e.stack import TaiStack

from ._support import AGENT, DETERMINISTIC_MARKS

pytestmark = DETERMINISTIC_MARKS

_FILE = "/scratch.txt"


def _tool_messages(request: dict) -> list[dict]:
    return [m for m in request["messages"] if m.get("role") == "tool"]


async def test_execute_shell_and_file_tools_share_the_live_durable_volume(
    deep_agent_durable_stack: TaiStack, llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    """Within one run, the built-in ``execute`` shell writes a file and ``read_file`` reads it
    back — the two built-ins operate on ONE live sandbox volume (the ``SandboxSessionBackend``),
    which is dormant under the old in-graph ``StateBackend``."""
    stack = deep_agent_durable_stack
    token = uniq("live")
    llm_stub.reset()
    llm_stub.script(
        [
            # The shell writes the token to a file on the session volume...
            {"tool_call": {"name": "execute", "arguments": {"command": f"printf %s {token} > scratch.txt"}}},
            # ...and the file tool reads that same file back — only possible over one live shell.
            {"tool_call": {"name": "read_file", "arguments": {"file_path": _FILE}}},
            {"content": "done"},
        ]
    )
    async with stack.mcp(port=stack.port_a) as mcp:
        await mcp.call_tool(AGENT, {"user_message": "write then read the scratch file"}, retry_on_reloading=True)

    read_result = _tool_messages(llm_stub.requests[-1])[-1]
    assert token in json.dumps(read_result), (
        f"read_file did not see what the execute shell wrote; the durable shell is not live: {read_result}"
    )


async def test_caller_face_workspace_is_ephemeral_while_the_checkpoint_persists(
    deep_agent_durable_stack: TaiStack, llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    """The caller-facing tool-face workspace is EPHEMERAL (workspace identity is never
    caller-derivable, the security invariant): a second run on the same ``langgraph_config``
    thread does NOT read the first run's file, though the langgraph checkpoint history DOES carry
    across the two turns — so this is a genuine threaded conversation over an ephemeral workspace,
    not a keyless run."""
    stack = deep_agent_durable_stack
    port = stack.port_a
    thread_id = uniq("thread")
    token = uniq("ephemeral")
    config = {"configurable": {"thread_id": thread_id}}
    llm_stub.reset()

    # Run 1 (thread pinned via langgraph_config): write the token to a file.
    llm_stub.script(
        [
            {"tool_call": {"name": "write_file", "arguments": {"file_path": _FILE, "content": token}}},
            {"content": "noted"},
        ]
    )
    async with stack.mcp(port=port) as mcp:
        await mcp.call_tool(
            AGENT, {"user_message": "note the token", "langgraph_config": config}, retry_on_reloading=True
        )

    # Run 2 (same langgraph_config thread): read it back — the caller-face workspace was ephemeral,
    # so the file is GONE even though the conversation thread is the same.
    llm_stub.script(
        [
            {"tool_call": {"name": "read_file", "arguments": {"file_path": _FILE}}},
            {"content": "read"},
        ]
    )
    async with stack.mcp(port=port) as mcp:
        await mcp.call_tool(
            AGENT, {"user_message": "read the token", "langgraph_config": config}, retry_on_reloading=True
        )

    last = llm_stub.requests[-1]
    read_result = _tool_messages(last)[-1]
    # The read MISSED — the ephemeral workspace from run 1 did not carry into run 2.
    assert token not in json.dumps(read_result), (
        f"the caller-face workspace persisted across runs; it must be ephemeral (never caller-threaded): {read_result}"
    )
    assert "not_found" in json.dumps(read_result) or "not found" in json.dumps(read_result).lower(), (
        f"run 2's read was not a clean miss: {read_result}"
    )
    # The langgraph CHECKPOINT, however, DID persist: run 1's write call is replayed into run 2's
    # request history, so the thread is genuinely continuous — the ephemerality is the WORKSPACE's,
    # not the conversation's.
    assert token in json.dumps(last["messages"]), (
        "run 1's turn did not carry into run 2 via the checkpoint; the thread was not continuous"
    )
