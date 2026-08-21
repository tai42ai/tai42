"""C6 + C7 — the async ``ask_user`` park lifecycle end to end, across a worker
boundary.

A flow driver (the ``e2e_async_park_flow`` probe) binds a resume continuation tool
plus a synthetic execution identity and calls ``ask_user(mode="async", expiry_at=...)``
on replica A. The ask PARKS — it returns a ``SuspendedInteraction`` at once, never
blocking — and the stored generic continuation (``e2e_async_resume``) fires exactly
once when the park resolves, REBOUND to the STORED park identity (never the answerer's
or reaper's default), carrying ``{interaction_id, answer}`` and recording which branch it
resumed through, in which process, and under which identity.

Two legs cover the two resolutions:

* park -> answer -> resume: an answer through replica B's ``/answer`` door fires the
  continuation on B (a different worker than the park), which runs the ANSWER branch.
* park -> expiry -> resume: no answer arrives; after the deadline replica B's expiry
  reaper claims the park and fires the continuation, which runs the EXPIRY branch.

Each leg proves four facts about the resolution: it ran on a DIFFERENT worker than the
park (cross-worker resume), through the correct ANSWER/EXPIRY branch, EXACTLY ONCE, and
under the STORED park identity — the identity-rebind the seam guarantees. That last
assertion is discriminating on the auth-off stack: the stored identity is a synthetic
value nothing else produces, so a resume that dropped the rebind (default execution
identity ``None``) or fired under the answerer's/reaper's identity would FAIL it.

The continuation RPUSHes ``{"branch", "value", "pid", "identity"}`` onto
``e2e:rec:async_resume:{interaction_id}``; the specs read that record back through the
harness probe channel.

The multi-park "barrier" exercised below is a PROBE STAND-IN playing the consumer
role — the engine's real super-step barrier is a separate component. This leg proves
the platform park/resume seam under scrambled cross-worker resolution, not that
barrier."""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack

# The stack runs no backend, so the module is ``backendless`` — the async park
# mechanism touches no backend seam, so it runs on the default backend leg only
# instead of being re-run under rq and celery.
pytestmark = pytest.mark.backendless


async def _resume_record(stack: TaiStack, interaction_id: str) -> dict | None:
    """The single continuation record for ``interaction_id`` on the probe channel, or
    ``None`` until it lands. Fails loudly if the continuation ever fires more than
    once — the exactly-once claim is the whole point of the park resolution."""
    records = stack.records(f"async_resume:{interaction_id}")
    if not records:
        return None
    assert len(records) == 1, f"the continuation fired more than once: {records}"
    return json.loads(records[0])


async def _park(stack: TaiStack, question: str, expiry_seconds: float) -> dict:
    """Drive the park on replica A and return the driver's frame
    (``interaction_id`` + the parking process pid). The submit can race the boot-time
    self-resync gate, so poll past a retriable ``reloading``."""
    async with stack.mcp(port=stack.port_a) as mcp:
        result = await mcp.call_tool(
            "e2e_async_park_flow",
            {"question": question, "expiry_seconds": expiry_seconds},
            retry_on_reloading=True,
        )
    return result.data


async def _multipark_completion(stack: TaiStack, super_step_id: str) -> dict | None:
    """The single super-step completion record for ``super_step_id``, or ``None`` until it
    lands. Fails loudly if the barrier ever mints more than one — exactly-once minting under
    scrambled resolution AND a concurrent redelivery is the whole point of this leg."""
    records = stack.records(f"multipark:{super_step_id}")
    if not records:
        return None
    assert len(records) == 1, f"the super-step minted more than one completion: {records}"
    return json.loads(records[0])


async def test_async_multipark_super_step_resolves_once(async_park_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    # The barrier here is a PROBE STAND-IN for the consumer role; the engine's real
    # super-step barrier is a separate component. This asserts the park/resume seam, not it.
    # Three concurrent async parks minted in ONE super-step (one driver call), resolved in
    # scrambled order across real workers: slot 2 answered on B, slot 1 answered on A, slot 0
    # left to B's expiry reaper. Slot 1's continuation is held in flight past the 1s reaper
    # interval so a redelivery LAPS the original fire — a concurrent redelivery during a live
    # drive. The barrier's idempotent apply (HSETNX) + single mint guard must yield EXACTLY
    # one completion carrying all three results, each slot applied once, every fire rebound to
    # the stored park identity.
    super_step_id = uniq("superstep")
    async with async_park_stack.mcp(port=async_park_stack.port_a) as mcp:
        result = await mcp.call_tool(
            "e2e_async_multipark_flow",
            {
                "super_step_id": super_step_id,
                "expiry_seconds": [2.0, 3600.0, 3600.0],
                "slow_slot": 1,
                "slow_seconds": 2.5,
            },
            retry_on_reloading=True,
        )
    frame = result.data
    interaction_ids = frame["interaction_ids"]
    stored_identity = frame["stored_identity"]
    assert len(interaction_ids) == 3

    # Answer two slots through DIFFERENT workers than the parking driver, in scrambled order;
    # leave slot 0 for the expiry reaper.
    api_a = async_park_stack.api(port=async_park_stack.port_a)
    api_b = async_park_stack.api(port=async_park_stack.port_b)
    answered2 = await api_b.post(f"/api/interactions/{interaction_ids[2]}/answer", json={"answer": "answer-2"})
    assert answered2["status"] == "answered"
    answered1 = await api_a.post(f"/api/interactions/{interaction_ids[1]}/answer", json={"answer": "answer-1"})
    assert answered1["status"] == "answered"

    completion = await wait_for_async(
        lambda: _multipark_completion(async_park_stack, super_step_id),
        deadline=25.0,
        message="the multi-park super-step never minted its single completion",
    )
    results = completion["results"]
    # Exactly one completion, carrying all three slot results — each slot applied once.
    assert set(results) == {"0", "1", "2"}
    assert results["0"]["value"] == "expired"  # slot 0 resolved by the expiry reaper
    assert results["1"]["value"] == json.dumps("answer-1")
    assert results["2"]["value"] == json.dumps("answer-2")
    # Every slot fired REBOUND to the STORED park identity — never the answerer's or reaper's
    # default (``None`` when the rebind is dropped).
    assert all(slot["identity"] == stored_identity for slot in results.values())


async def test_async_park_answer_resumes_across_workers(async_park_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    question = uniq("question")
    # Park on A with a far expiry so the reaper never races the answer.
    frame = await _park(async_park_stack, question, expiry_seconds=3600)
    interaction_id = frame["interaction_id"]
    park_pid = frame["pid"]
    stored_identity = frame["stored_identity"]

    # Answer through replica B's door: this door claims the answer and fires the stored
    # continuation on B — a different worker than the one that parked on A.
    api_b = async_park_stack.api(port=async_park_stack.port_b)
    answered = await api_b.post(
        f"/api/interactions/{interaction_id}/answer",
        json={"answer": "resume-me"},
    )
    assert answered["status"] == "answered"

    record = await wait_for_async(
        lambda: _resume_record(async_park_stack, interaction_id),
        deadline=10.0,
        message="the answered park never fired its resume continuation",
    )
    # The continuation ran its ANSWER branch, carrying the exact delivered answer.
    assert record["branch"] == "answer"
    assert record["value"] == json.dumps("resume-me")
    # It fired in a DIFFERENT process than the park: the park ran on A, the answer door
    # (and thus the continuation) on B.
    assert record["pid"] != park_pid
    # It ran REBOUND to the park's STORED identity — not the answer door's default caller
    # identity (the auth-off default execution identity is ``None``, so this fails loudly
    # if the rebind is dropped or the continuation fires under the answerer's identity).
    assert record["identity"] == stored_identity


async def test_async_park_expiry_resumes(async_park_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    question = uniq("question")
    # Park with a short deadline and never answer: the expiry reaper (pinned to 1s on
    # this stack) claims the park once ``expiry_at`` passes and fires the continuation.
    frame = await _park(async_park_stack, question, expiry_seconds=2)
    interaction_id = frame["interaction_id"]
    stored_identity = frame["stored_identity"]

    record = await wait_for_async(
        lambda: _resume_record(async_park_stack, interaction_id),
        deadline=15.0,
        message="the expired park never fired its resume continuation",
    )
    # The continuation ran its EXPIRY branch — the generic expired marker, not a real answer.
    assert record["branch"] == "expiry"
    assert record["value"] == "expired"
    # It ran REBOUND to the park's STORED identity — not the expiry reaper's default caller
    # identity (the auth-off default execution identity is ``None``, so this fails loudly
    # if the rebind is dropped or the continuation fires under the reaper's identity).
    assert record["identity"] == stored_identity

    # The reaper claimed the park at expiry, so a late answer through the door now finds
    # it already resolved and is refused (409) — the single-resolution guarantee.
    api_b = async_park_stack.api(port=async_park_stack.port_b)
    late = await api_b.request_raw(
        "POST",
        f"/api/interactions/{interaction_id}/answer",
        json={"answer": "too-late"},
    )
    assert late.status_code == 409, late.text
