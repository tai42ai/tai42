"""The AGENT async ``ask`` park lifecycle end to end, across a worker boundary.

A real ``tools_agent`` run drives the ``e2e_agent_async_ask`` probe tool, whose async
``ask`` PARKS the run on replica A and returns a suspended receipt. The agents
plugin's own durable park index reverses the parked interaction id back to the run, and a
resolution on replica B — an answer through B's ``/answer`` door, or B's expiry reaper —
fires the hidden ``agent_resume`` continuation, which rebuilds the same graph on the other
worker (the shared redis checkpoint) and drives it to its final answer.

Two legs, mirroring the flow-driver ``interactions/test_async_park_resume`` legs:

* park -> answer -> resume: an answer through replica B fires ``agent_resume`` on B.
* park -> expiry -> resume: no answer arrives; the park is brought to its deadline and the 1s
  expiry reaper fires ``agent_resume``.

Each leg proves the parked tool's ``ask`` ran EXACTLY ONCE (a resume substitutes the
answer, never re-runs the tool) and that the resumed drive ran REBOUND to the STORED park
identity — a synthetic value (``e2e-async-driver``) the auth-off default execution identity
(``None``) never produces — recorded by the ``e2e_record_identity`` tool the scripted model
calls on the resumed turn.

The ``tools_agent`` is the real consumer here: its OWN park machinery binds the
``agent_resume`` continuation around the drive. The probe tools stand in only for the
authed caller identity ``ask`` needs (the auth-off stack carries none) and for the
resumed model turns; the park index, the interrupt barrier, and the resume drive are the
platform/agents-plugin components under test.

Gated on ``TAI_E2E_CHECKPOINT_REDIS_URL`` via the ``agent_async_park_stack`` fixture, which
skips with the ``docker compose --profile agents-redis up -d`` hint when the module-capable
Redis is absent.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest
from tai42_contract.interactions import SuspendedInteraction

from tai42_e2e import wait_for_async
from tai42_e2e.llmstub import LlmStub
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack

# The synthetic identity ``e2e_agent_async_ask`` binds and stores as the park's
# ``continuation_identity`` — kept in step with ``_ASYNC_PARK_IDENTITY`` in the probe tools.
_STORED_PARK_IDENTITY = "e2e-async-driver"

# The agents stack runs no backend worker; skip on non-default backend legs (they exercise
# no backend seam). The scripted llm_stub is the LLM MOCK leg — the real-provider leg runs on
# the e2e creds host, not in CI — so the stub-bound module steps aside when 'llm' is real.
pytestmark = [
    pytest.mark.backendless,
    pytest.mark.skipif(
        HarnessSettings().is_real("llm"),
        reason="scripted llm_stub is the 'llm' mock leg; the real leg runs on the e2e creds host",
    ),
]

_TOOL_NAMES = ["e2e_agent_async_ask", "e2e_record_identity"]


async def _resume_record(stack: TaiStack, thread_id: str) -> dict | None:
    """The single ``e2e_record_identity`` record for ``thread_id``, or ``None`` until the
    resumed drive reaches it. Fails loudly if the resume drives more than once — the
    exactly-once resume is the point."""
    records = stack.records(f"agent_resume:{thread_id}")
    if not records:
        return None
    assert len(records) == 1, f"the resumed drive recorded more than once: {records}"
    return json.loads(records[0])


async def _await_resume_drive_finished(llm_stub: LlmStub, scripted_turns: int) -> None:
    """Wait until the resume drive has consumed the whole scripted script — i.e. reached its terminal.

    The scripted LLM is one shared, order-only queue with no per-run isolation. A leg's assertions
    pass as soon as the resumed drive records its identity (an intermediate turn), but the drive
    keeps pulling later turns until it terminates. If the leg returned then, the still-running drive
    would consume the NEXT leg's freshly-scripted turns (or drive a spurious re-park off them),
    corrupting it. The served-completion count reaching the leg's turn count means the drive has
    consumed its whole script and terminated, so the next leg scripts a quiet stub.
    """

    async def _script_consumed() -> bool | None:
        return (len(llm_stub.requests) >= scripted_turns) or None

    await wait_for_async(
        _script_consumed,
        deadline=20.0,
        message=f"the resume drive never consumed its {scripted_turns} scripted turns",
    )


async def _park_agent_run(stack: TaiStack, thread_id: str, question: str) -> str:
    """Drive a ``tools_agent`` run on replica A that parks on ``e2e_agent_async_ask`` and
    return the single parked ``interaction_id``. The submit can race the boot-time
    self-resync gate, so poll past a retriable ``reloading``."""
    async with stack.mcp(port=stack.port_a) as mcp:
        result = await mcp.call_tool(
            "tools_agent",
            {
                "user_message": {"content": question},
                "tool_names": _TOOL_NAMES,
                "langgraph_config": {"configurable": {"thread_id": thread_id}},
            },
            retry_on_reloading=True,
        )
    receipt = result.data
    # A park crossing the agent TOOL-face is now the generic ``SuspendedInteraction`` sentinel
    # (model-dumped over the wire as ``{"interaction_id": ..., "expiry_at": ...}``, no top-level
    # ``"status"`` and a single ``interaction_id``, not an ``interaction_ids`` list) — never the
    # agent's INTERNAL suspended-receipt dict. Validate the wire dict back to that sentinel to
    # PROVE the run parked: a completed run returns free-form text or an answer dict, neither of
    # which carries the required ``interaction_id`` and so fails this validation loudly.
    assert isinstance(receipt, dict), f"the agent run did not return a park sentinel: {receipt}"
    suspended = SuspendedInteraction.model_validate(receipt)
    interaction_id = suspended.interaction_id
    # The parked tool's ask ran exactly once at park time.
    ask_records = stack.records(f"agent_async_ask:{interaction_id}")
    assert len(ask_records) == 1, f"e2e_agent_async_ask did not run exactly once: {ask_records}"
    return interaction_id


async def test_agent_park_answer_resumes_across_workers(
    agent_async_park_stack: TaiStack, llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    thread_id = uniq("thread")
    question = uniq("question")
    llm_stub.reset()
    llm_stub.script(
        [
            # Turn 1 (replica A): call the async-asking tool — the run parks here.
            {"tool_call": {"name": "e2e_agent_async_ask", "arguments": {"question": question, "expiry_seconds": 3600}}},
            # Turn 2 (replica B, resumed): record the identity the resumed drive runs under.
            {"tool_call": {"name": "e2e_record_identity", "arguments": {"thread_id": thread_id}}},
            # Turn 3 (replica B, resumed): final answer.
            {"content": "resumed and done"},
        ]
    )

    interaction_id = await _park_agent_run(agent_async_park_stack, thread_id, question)

    # Answer through replica B's door: this fires agent_resume on B — a different worker than
    # the one that parked on A — rebuilding the graph from the shared redis checkpoint.
    api_b = agent_async_park_stack.api(port=agent_async_park_stack.port_b)
    answered = await api_b.post(f"/api/interactions/{interaction_id}/answer", json={"answer": "resume-me"})
    assert answered["status"] == "answered"

    record = await wait_for_async(
        lambda: _resume_record(agent_async_park_stack, thread_id),
        deadline=20.0,
        message="the answered agent park never resumed its drive",
    )
    # The resumed drive ran REBOUND to the park's STORED identity — not the answer door's
    # default caller identity (auth-off default is ``None``, so this fails loudly if the
    # rebind is dropped or the resume fires under the answerer's identity).
    assert record["identity"] == _STORED_PARK_IDENTITY
    # The parked tool's ask still ran exactly once — the resume substituted the answer, never
    # re-ran the tool.
    assert len(agent_async_park_stack.records(f"agent_async_ask:{interaction_id}")) == 1

    # Let the resume drive reach its terminal (consume all three scripted turns) before the module's
    # next leg resets the shared scripted LLM — a drive still in flight would eat that leg's turns.
    await _await_resume_drive_finished(llm_stub, 3)


async def test_agent_park_expiry_resumes(
    agent_async_park_stack: TaiStack, llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    thread_id = uniq("thread")
    question = uniq("question")
    llm_stub.reset()
    llm_stub.script(
        [
            # Turn 1 (replica A): park under ``on_expiry="resume"`` with a far deadline the submit
            # always observes first, never answer.
            {
                "tool_call": {
                    "name": "e2e_agent_async_ask",
                    "arguments": {"question": question, "expiry_seconds": 3600, "on_expiry": "resume"},
                }
            },
            # Turns 2 + 3 (resumed by the expiry reaper): record the identity, then finish.
            {"tool_call": {"name": "e2e_record_identity", "arguments": {"thread_id": thread_id}}},
            {"content": "resumed on expiry"},
        ]
    )

    interaction_id = await _park_agent_run(agent_async_park_stack, thread_id, question)

    # Bring the durably-parked run to its deadline deterministically — a short park window would
    # race the submit latency — then never answer: the 1s expiry reaper claims the now-due park
    # and fires agent_resume, which rebuilds and drives the graph to completion.
    async with agent_async_park_stack.mcp(port=agent_async_park_stack.port_b) as mcp:
        await mcp.call_tool("e2e_expire_park", {"interaction_id": interaction_id}, retry_on_reloading=True)

    record = await wait_for_async(
        lambda: _resume_record(agent_async_park_stack, thread_id),
        deadline=25.0,
        message="the expired agent park never resumed its drive",
    )
    # The reaper-fired resume ran REBOUND to the park's STORED identity — not the reaper's
    # default caller identity (auth-off default is ``None``).
    assert record["identity"] == _STORED_PARK_IDENTITY
    # The parked tool's ask ran exactly once — the reaper resume substituted the expiry
    # marker, never re-ran the tool.
    assert len(agent_async_park_stack.records(f"agent_async_ask:{interaction_id}")) == 1

    # The reaper claimed the park at expiry, so a late answer through the door now finds it
    # already resolved and is refused (409) — the single-resolution guarantee.
    api_b = agent_async_park_stack.api(port=agent_async_park_stack.port_b)
    late = await api_b.request_raw(
        "POST",
        f"/api/interactions/{interaction_id}/answer",
        json={"answer": "too-late"},
    )
    assert late.status_code == 409, late.text

    # Let the resume drive reach its terminal before the module ends, so it never survives into a
    # later leg's shared scripted LLM (order-independent isolation, matching the answer leg).
    await _await_resume_drive_finished(llm_stub, 3)
