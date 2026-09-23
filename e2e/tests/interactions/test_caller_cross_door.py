"""A caller ask parked by one door is resolvable from another — cross-door on the subject.

The ask lives on its subject, not on the door that raised it, so any door that operates on that
subject reaches it. Here the ask is parked by the tool-runs subject door and resumed by a HOOK
door: a webhook fire whose hook binds the same subject evaluates its ``resume_expr`` over the
run's parked interactions and resumes the caller ask receiver-less, so the resumed run's outcome
waits on the subject for a later run to take. The stack runs no backend seam, so the module is
``backendless``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack

from ._caller_support import await_result, await_status, await_terminal, caller_ask_id, subject, submit

pytestmark = pytest.mark.backendless

# resume_expr yields one ``{id, payload}`` per parked caller ask, so the hook fire resumes the
# subject's ask with this answer over the door contract's ``$parked`` binding.
_RESUME_EXPR = '[$parked[] | select(.status == "asking" and .to == "caller") | {id: .id, payload: "hook-answer"}]'


async def _register_resume_hook(api: Any, *, topic: str, execution_key: str) -> None:
    """Register a hook that, on a fire keyed to the caller subject, resumes its caller ask."""
    await api.post(
        "/api/hooks",
        json={
            "name": f"{topic}-resume",
            "topic": topic,
            "tool": "e2e_echo",
            "start_expr": {"content": "null"},
            "resume_expr": {"content": _RESUME_EXPR},
            "execution_key": execution_key,
            "subject": {
                "target_kind": "tool",
                "target_name": "held_run",
                "kind": "person",
                "key_expr": {"content": ".key"},
            },
        },
    )


async def _take_when_ready(stack: TaiStack, subj: dict[str, str], *, deadline: float = 15.0) -> dict[str, Any]:
    """Poll a taking run until the hook's receiverless resume has left a waiting outcome to take."""

    async def _try() -> dict[str, Any] | None:
        run_id = await submit(stack.api(port=stack.port_a), "caller_take", {}, subj)
        view = await await_terminal(stack.api(port=stack.port_b), run_id)
        return view if view["status"] == "succeeded" else None

    return await wait_for_async(_try, deadline=deadline, message="the hook-resumed outcome never became takeable")


async def test_a_caller_ask_parked_by_the_subject_door_is_resumed_by_a_hook(
    caller_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api_a = caller_stack.api(port=caller_stack.port_a)
    api_b = caller_stack.api(port=caller_stack.port_b)
    subj = subject(uniq("subject"))

    # Park the caller ask through the tool-runs subject door.
    run_id = await submit(api_a, "held_run", {"marker": uniq("q")}, subj)
    await await_status(api_a, run_id, "parked")
    await caller_ask_id(api_a, subj)

    # A hook bound to the same subject resumes the ask when its webhook fires — a DIFFERENT door
    # than the one that parked it. The hook has no receiver, so the resumed outcome waits.
    topic = uniq("resume-topic").replace("_", "-")
    await _register_resume_hook(api_a, topic=topic, execution_key=uniq("hook-exec"))
    await api_b.request_raw("POST", f"/universal_webhook/{topic}", json={"key": subj["key"]})

    # A later run on the subject takes the outcome the hook left waiting, carrying the hook answer.
    taken = await _take_when_ready(caller_stack, subj)
    outcome = taken["result"]
    assert outcome["action"] == "taken"
    assert outcome["result"]["answer"] == "hook-answer"


async def test_the_parked_record_carries_the_caller_ask_entries(
    caller_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    # The background submit door's PARKED record answers a caller-ask park with the ask entries —
    # the SAME ``{"asks": [...]}`` shape the sync door returns — so a poller of the submit door
    # sees the question and its ``to == "caller"`` addressing and knows to answer it through the
    # resume door, not to wait on a user.
    api_a = caller_stack.api(port=caller_stack.port_a)
    subj = subject(uniq("subject"))

    run_id = await submit(api_a, "held_run", {"marker": uniq("q")}, subj)
    view = await await_status(api_a, run_id, "parked")
    ask_id = await caller_ask_id(api_a, subj)

    answer = view["result"]
    assert list(answer) == ["asks"]
    entry = next(a for a in answer["asks"] if a["id"] == ask_id)
    assert entry["to"] == "caller"
    assert entry["question"]


async def test_a_caller_ask_parked_by_the_subject_door_is_resumed_by_a_direct_run(
    caller_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api_a = caller_stack.api(port=caller_stack.port_a)
    api_b = caller_stack.api(port=caller_stack.port_b)
    subj = subject(uniq("subject"))

    run_id = await submit(api_a, "held_run", {"marker": uniq("q")}, subj)
    await await_status(api_a, run_id, "parked")
    await caller_ask_id(api_a, subj)

    # The direct-run door takes the resumed outcome inline (its own receiver), the cross-door
    # contrast to the hook: same subject, a different door, the outcome delivered in-run.
    relay_run = await submit(api_b, "caller_relay", {"payload": "run-answer"}, subj)
    outcome = await await_result(api_a, relay_run)
    assert outcome["action"] == "resumed"
    assert outcome["result"]["answer"] == "run-answer"
