"""``langchain_deep_agent`` async park + cross-worker resume over the durable backend.

The deep agent's run face binds the hidden ``agent_resume`` continuation, so an async
``ask_user`` a run drives PARKS and returns a suspended receipt; a resolution on the OTHER
replica — an answer through B's ``/answer`` door — fires ``agent_resume`` -> ``aresume_park``,
which rebuilds the same graph from the shared redis checkpoint and drives it to completion,
REBOUND to the park's STORED identity. This mirrors the ``tools_agent`` park lifecycle, exercised
here for the durable deep agent.

Two more park-door invariants (asserted FIRST, before the async-resume leg, so the reaper-driven
resume the resume leg spawns never drains the shared llm_stub from under them):

* RETENTION BOUND — a park lives only as long as EVERY backing store: its checkpoint AND its
  sandbox WORKSPACE volume. The bound is ``min(checkpoint, workspace)`` (§B3.1), so an ask
  deadline beyond the workspace horizon (``now + session_ttl``) is refused LOUDLY at
  park-persist, zero index state written.
* ``resume_checkpoint_id`` is UNHONORED on the durable deep agent — the durable WORKSPACE volume
  cannot be forked alongside the checkpoint (§B3.3), so the run door rejects it loudly.

(The HARD sandbox dependency §B3.7 — a run with no provider raising ``SandboxUnavailableError`` —
rides its own module, ``test_deep_agent_sandbox_required``, to keep the single checkpoint logical
DB free of a second concurrent stack.)
"""

from __future__ import annotations

import json
from collections.abc import Callable

from tai42_e2e import wait_for_async
from tai42_e2e.llmstub import LlmStub
from tai42_e2e.stack import TaiStack

from ._support import AGENT, DETERMINISTIC_MARKS, STORED_PARK_IDENTITY

pytestmark = DETERMINISTIC_MARKS

_TOOL_NAMES = ["e2e_agent_async_ask", "e2e_record_identity"]


async def _resume_record(stack: TaiStack, thread_id: str) -> dict | None:
    """The single ``e2e_record_identity`` record for ``thread_id``, or ``None`` until the resumed
    drive reaches it. Fails loudly if the resume drove more than once (exactly-once is the point:
    the checkpoint dedupes a redelivered terminal, so ``record_identity`` runs at most once)."""
    records = stack.records(f"agent_resume:{thread_id}")
    if not records:
        return None
    assert len(records) == 1, f"the resumed drive recorded more than once: {records}"
    return json.loads(records[0])


async def _park(stack: TaiStack, thread_id: str, question: str, expiry_seconds: float) -> dict:
    """Drive a ``langchain_deep_agent`` run on replica A that parks on ``e2e_agent_async_ask``,
    returning ``{receipt}`` on a suspended park or ``{error}`` on a loud refusal."""
    async with stack.mcp(port=stack.port_a) as mcp:
        result = await mcp.call_tool(
            AGENT,
            {
                "user_message": question,
                "tool_names": _TOOL_NAMES,
                "langgraph_config": {"configurable": {"thread_id": thread_id}},
            },
            raise_on_error=False,
            retry_on_reloading=True,
        )
    if result.is_error:
        return {"error": " ".join(getattr(p, "text", "") for p in result.content)}
    return {"receipt": result.data}


async def test_park_deadline_beyond_workspace_retention_is_refused(
    deep_agent_durable_stack: TaiStack, llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    """A parked ask whose expiry outlives the run's retention horizon — bounded by the sandbox
    WORKSPACE volume (``now + session_ttl``, the default 86400s) — is refused LOUDLY at
    park-persist, since a backing store would be swept before the ask resolves (§B3.1)."""
    stack = deep_agent_durable_stack
    thread_id = uniq("thread")
    question = uniq("question")
    llm_stub.reset()
    # 100000s (~27.8h) exceeds the workspace retention horizon (session_ttl default 86400s = 24h),
    # so min(checkpoint, workspace) refuses it regardless of the (unbounded) redis checkpoint.
    llm_stub.script(
        [{"tool_call": {"name": "e2e_agent_async_ask", "arguments": {"question": question, "expiry_seconds": 100000}}}]
    )

    outcome = await _park(stack, thread_id, question, expiry_seconds=100000)
    assert "error" in outcome, f"an over-retention park was not refused: {outcome}"
    assert "retention horizon" in outcome["error"], f"the refusal was not the retention-bound error: {outcome}"


async def test_resume_checkpoint_id_is_unhonored(
    deep_agent_durable_stack: TaiStack, llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    """``resume_checkpoint_id`` is unhonored on the durable deep agent (the workspace volume cannot
    be forked alongside the checkpoint, §B3.3) — the run door rejects it loudly rather than
    silently forking."""
    stack = deep_agent_durable_stack
    llm_stub.reset()
    async with stack.mcp(port=stack.port_a) as mcp:
        result = await mcp.call_tool(
            AGENT,
            {"user_message": uniq("q"), "resume_checkpoint_id": uniq("ckpt")},
            raise_on_error=False,
            retry_on_reloading=True,
        )
    assert result.is_error, "resume_checkpoint_id was accepted silently"
    text = " ".join(getattr(p, "text", "") for p in result.content)
    assert "resume_checkpoint_id" in text, f"the rejection did not name resume_checkpoint_id: {text}"


async def test_park_answer_resumes_across_workers(
    deep_agent_durable_stack: TaiStack, llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    """Runs LAST in the module: the answer fires a reaper-backed (at-least-once) ``agent_resume``,
    whose lingering redelivery must not drain the shared stub from a later sync test — so the two
    synchronous refusal legs above run first, and this leg's residue dies with the module stack."""
    stack = deep_agent_durable_stack
    thread_id = uniq("thread")
    question = uniq("question")
    llm_stub.reset()
    llm_stub.script(
        [
            # Turn 1 (replica A): the model calls the async-asking tool — the run parks here.
            {"tool_call": {"name": "e2e_agent_async_ask", "arguments": {"question": question, "expiry_seconds": 3600}}},
            # Turns 2 + 3 (replica B, resumed): record the resumed identity, then finish. Extra
            # terminal turns buffer a reaper redelivery of the final super-step so it never hits an
            # empty queue (the checkpoint dedupes, so record_identity still runs exactly once).
            {"tool_call": {"name": "e2e_record_identity", "arguments": {"thread_id": thread_id}}},
            {"content": "resumed and done"},
            {"content": "resumed and done"},
            {"content": "resumed and done"},
        ]
    )

    parked = await _park(stack, thread_id, question, expiry_seconds=3600)
    receipt = parked.get("receipt")
    assert isinstance(receipt, dict), f"the deep agent run did not return a receipt: {parked}"
    assert receipt.get("status") == "suspended", f"the deep agent run did not park: {parked}"
    interaction_ids = receipt["interaction_ids"]
    assert len(interaction_ids) == 1, f"expected one parked interaction, got {interaction_ids}"
    interaction_id = interaction_ids[0]

    # Answer through replica B's door: fires agent_resume on B — a different worker than the one
    # that parked on A — rebuilding the graph from the shared redis checkpoint.
    api_b = stack.api(port=stack.port_b)
    answered = await api_b.post(
        f"/api/interactions/{interaction_id}/answer", json={"answer": "resume-me"}, retry_on_reloading=True
    )
    assert answered["status"] == "answered"

    # The answer fires agent_resume on B; the continuation is at-least-once (a transient failure is
    # redelivered by the 1s reaper), so poll past a slow/redelivered resume under full-suite load.
    record = await wait_for_async(
        lambda: _resume_record(stack, thread_id),
        deadline=40.0,
        message="the answered deep-agent park never resumed its drive",
    )
    # The resumed drive ran REBOUND to the park's STORED identity — not the answer door's default
    # caller identity (auth-off default is None), proving the cross-worker rebind held.
    assert record["identity"] == STORED_PARK_IDENTITY
    # The parked tool's ask ran exactly once — the resume substituted the answer, never re-ran it.
    assert len(stack.records(f"agent_async_ask:{interaction_id}")) == 1
