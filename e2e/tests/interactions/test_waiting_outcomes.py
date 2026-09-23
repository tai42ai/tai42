"""A resumed run whose resumer cannot take the outcome leaves it WAITING on the subject.

One rule governs where a resumed run's terminal goes: whoever resumed and can take it in the
same run gets it; otherwise it waits on the subject for a later run. A direct run takes it
inline (covered by ``test_caller_ask``); a receiverless resume — a hook or schedule stand-in —
leaves the terminal waiting, and a later run on the same subject takes it. This drives the
receiverless leg: the resumed ``held_run`` finalizes, its outcome waits, and a later run takes
the exact result. The stack runs no backend seam, so the module is ``backendless``."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack

from ._caller_support import await_result, await_status, await_terminal, caller_ask_id, subject, submit

pytestmark = pytest.mark.backendless


async def _take_when_ready(stack: TaiStack, subj: dict[str, str], *, deadline: float = 20.0) -> dict[str, Any]:
    """Poll a taking run until a waiting outcome exists to take (the reaper redelivery must land it)."""

    async def _try() -> dict[str, Any] | None:
        run_id = await submit(stack.api(port=stack.port_a), "caller_take", {}, subj)
        view = await await_terminal(stack.api(port=stack.port_b), run_id)
        return view["result"] if view["status"] == "succeeded" else None

    return await wait_for_async(_try, deadline=deadline, message="a waiting outcome never became takeable")


async def test_a_receiverless_resume_waits_and_a_later_run_takes_it(
    caller_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api_a = caller_stack.api(port=caller_stack.port_a)
    api_b = caller_stack.api(port=caller_stack.port_b)
    subj = subject(uniq("subject"))

    run_id = await submit(api_a, "held_run", {"marker": uniq("q")}, subj)
    await await_status(api_a, run_id, "parked")
    await caller_ask_id(api_a, subj)

    # A receiverless resume (a hook/schedule stand-in) resolves the ask but takes nothing —
    # the resumed run finalized, and its outcome now waits on the subject.
    detached_run = await submit(api_b, "caller_resume_detached", {"payload": "the-answer"}, subj)
    await await_result(api_a, detached_run)

    # A later run on the same subject takes the waiting outcome and gets the exact result.
    take_run = await submit(api_a, "caller_take", {}, subj)
    taken = await await_result(api_b, take_run)
    assert taken["action"] == "taken"
    assert taken["result"]["answer"] == "the-answer"


async def test_taking_a_failed_waiting_outcome_fails_the_taker(
    caller_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api_a = caller_stack.api(port=caller_stack.port_a)
    api_b = caller_stack.api(port=caller_stack.port_b)
    subj = subject(uniq("subject"))

    run_id = await submit(api_a, "held_run", {"marker": uniq("q")}, subj)
    await await_status(api_a, run_id, "parked")
    await caller_ask_id(api_a, subj)

    # A receiverless resume whose drive reaches a FAILED terminal (the ``__held_fail__`` answer)
    # leaves a FAILED waiting outcome on the subject.
    detached_run = await submit(api_b, "caller_resume_detached", {"payload": "__held_fail__"}, subj)
    await await_result(api_a, detached_run)

    # Taking a failed waiting outcome raises into the taker, so its run fails.
    take_run = await submit(api_a, "caller_take", {}, subj)
    take_view = await await_terminal(api_b, take_run)
    assert take_view["status"] == "failed"
    assert take_view["error"]


async def test_a_resumer_killed_mid_resume_is_redelivered_and_the_outcome_is_taken(
    caller_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api_a = caller_stack.api(port=caller_stack.port_a)
    api_b = caller_stack.api(port=caller_stack.port_b)
    subj = subject(uniq("subject"))

    run_id = await submit(api_a, "held_run", {"marker": uniq("q")}, subj)
    await await_status(api_a, run_id, "parked")
    interaction_id = await caller_ask_id(api_a, subj)

    # A receiverless resume whose FIRST drive raises a plain (transient) error — a resumer killed
    # mid-resume. The due record survives, so the reaper redelivers; the redelivered drive settles
    # and the outcome then waits on the subject.
    await submit(api_b, "caller_resume_detached", {"payload": "__held_transient__"}, subj)

    # A later run takes the outcome the reaper redelivery landed; the drive ran twice (fail, then
    # redeliver), so the probe channel holds two records for the interaction.
    result = await _take_when_ready(caller_stack, subj)
    assert result["action"] == "taken"
    assert result["result"]["answer"] == "__held_transient__"
    records = caller_stack.records(f"held_run:{interaction_id}")
    assert len(records) == 2, f"the drive must run twice (fail then redeliver), saw {records!r}"


async def test_a_redelivered_resume_that_re_parks_records_the_restored_chain(
    caller_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api_a = caller_stack.api(port=caller_stack.port_a)
    api_b = caller_stack.api(port=caller_stack.port_b)
    subj = subject(uniq("subject"))

    # A held_run that re-parks on resume; its first ask's chain names held_run.
    run_id = await submit(api_a, "held_run", {"marker": uniq("q"), "reask": True}, subj)
    await await_status(api_a, run_id, "parked")
    await caller_ask_id(api_a, subj)

    # The first receiverless drive fails transiently; the reaper redelivers carrying the parked
    # run's chain as ``due.asked_by``, and the redelivered drive re-parks under it.
    await submit(api_b, "caller_resume_detached", {"payload": "__held_transient__"}, subj)

    async def _reparked() -> dict[str, Any] | None:
        list_run = await submit(api_a, "caller_list", {}, subj)
        entries = await await_result(api_b, list_run)
        asking = [e for e in entries if e["status"] == "asking"]
        return asking[0] if len(asking) == 1 else None

    reparked = await wait_for_async(
        _reparked, deadline=25.0, message="the redelivered re-park never appeared on the subject"
    )
    # The reaper redelivery restored the parked run's chain (due.asked_by): the re-park records it,
    # never the continuation tool's own name.
    assert reparked["asked_by"] == ["held_run"]
    assert "held_run_resume" not in reparked["asked_by"]


async def test_an_untaken_waiting_outcome_is_swept_and_fires_its_event(
    caller_sweep_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api_a = caller_sweep_stack.api(port=caller_sweep_stack.port_a)
    api_b = caller_sweep_stack.api(port=caller_sweep_stack.port_b)
    subj = subject(uniq("subject"))

    # A hook on the retention-sweep's platform event records the dropped outcome's completion id.
    sweep_key = uniq("sweep")
    await api_a.post(
        "/api/hooks",
        json={
            "name": uniq("sweep-hook").replace("_", "-"),
            "topic": "interactions_outcome_dropped_untaken",
            "tool": "e2e_record",
            "start_expr": {"content": f'{{key: "{sweep_key}", value: .completion_id}}'},
            "execution_key": uniq("sweep-exec"),
        },
        retry_on_reloading=True,
    )

    run_id = await submit(api_a, "held_run", {"marker": uniq("q")}, subj)
    await await_status(api_a, run_id, "parked")
    await caller_ask_id(api_a, subj)

    # A receiverless resume leaves a waiting outcome that NOBODY takes.
    detached_run = await submit(api_b, "caller_resume_detached", {"payload": "the-answer"}, subj)
    await await_result(api_a, detached_run)

    # Past the short retention horizon the reaper's sweep drops the untaken outcome and fires its
    # loud event, which the hook records.
    async def _swept() -> list | None:
        records = caller_sweep_stack.records(sweep_key)
        return records or None

    dropped = await wait_for_async(_swept, deadline=25.0, message="the untaken outcome was never swept")
    assert len(dropped) == 1, f"the sweep must fire its event exactly once, saw {dropped!r}"
    assert json.loads(dropped[0])["value"], "the dropped-outcome event carried no completion id"
