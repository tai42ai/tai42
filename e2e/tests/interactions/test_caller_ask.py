"""A tool that asks its CALLER parks on the run's subject; the answer resolves the run.

``held_run`` async-asks its caller and parks on the subject the detached run was submitted
under. A later run on the SAME subject resumes the caller ask with a payload through the
generic list/resume tools (the ``caller_relay`` probe), takes the resumed run's final result
inline, and hands it back to the resumer. The stack runs no backend seam, so the module is
``backendless`` — it runs on the default backend leg only."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.backendless


def _subject(key: str) -> dict[str, str]:
    """A ``person``-kind subject scope both the parking run and its resumer address."""
    return {"target_kind": "tool", "target_name": "held_run", "kind": "person", "key": key}


async def _submit(api: Any, tool_name: str, arguments: dict[str, Any], subject: dict[str, str]) -> str:
    submitted = await api.post(
        "/api/tool-runs",
        json={"tool_name": tool_name, "arguments": arguments, "subject": subject},
        expect=202,
    )
    return submitted["run_id"]


async def _await_status(api: Any, run_id: str, status: str, *, deadline: float = 20.0) -> dict[str, Any]:
    async def _reached() -> dict[str, Any] | None:
        view = await api.get(f"/api/tool-runs/{run_id}")
        return view if view["status"] == status else None

    return await wait_for_async(_reached, deadline=deadline, message=f"run {run_id} never reached {status}")


_TERMINAL = {"succeeded", "failed"}


async def _await_terminal(api: Any, run_id: str, *, deadline: float = 20.0) -> dict[str, Any]:
    async def _reached() -> dict[str, Any] | None:
        view = await api.get(f"/api/tool-runs/{run_id}")
        return view if view["status"] in _TERMINAL else None

    return await wait_for_async(_reached, deadline=deadline, message=f"run {run_id} never reached a terminal")


async def test_caller_ask_parks_and_a_later_run_takes_the_resumed_result(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api_a = replicas_stack.api(port=replicas_stack.port_a)
    api_b = replicas_stack.api(port=replicas_stack.port_b)
    subject = _subject(uniq("subject"))

    # held_run async-asks its caller on A and parks on the subject.
    held_run_id = await _submit(api_a, "held_run", {"marker": uniq("q")}, subject)
    await _await_status(api_b, held_run_id, "parked")

    # A later run on the SAME subject (a different worker) resumes the caller ask with a
    # payload and takes the resumed run's final result inline.
    relay_run_id = await _submit(api_b, "caller_relay", {"payload": "the-answer"}, subject)
    view = await _await_status(api_a, relay_run_id, "succeeded")

    outcome = view["result"]
    assert outcome["action"] == "resumed"
    assert outcome["kind"] == "result"
    # The resumed held_run finalized by returning the answer it was resumed with.
    assert outcome["result"]["answer"] == "the-answer"


async def test_a_resumed_re_park_records_the_restored_chain(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api_a = replicas_stack.api(port=replicas_stack.port_a)
    api_b = replicas_stack.api(port=replicas_stack.port_b)
    subject = _subject(uniq("subject"))

    # held_run asks its caller through held_ask, so the first ask's chain names held_run.
    held_run_id = await _submit(api_a, "held_run", {"marker": uniq("q"), "reask": True}, subject)
    await _await_status(api_b, held_run_id, "parked")

    # Resuming re-drives held_run, which asks a SECOND time (a re-park): the resume outcome
    # hands back the new caller ask, whose asked_by is the RESTORED chain — held_run alone,
    # never the continuation tool held_run_resume.
    relay_run_id = await _submit(api_b, "caller_relay", {"payload": "first-answer"}, subject)
    view = await _await_status(api_a, relay_run_id, "succeeded")

    outcome = view["result"]
    assert outcome["action"] == "resumed"
    assert outcome["kind"] == "asks"
    assert len(outcome["asks"]) == 1
    reparked = outcome["asks"][0]
    assert reparked["asked_by"] == ["held_run"]
    assert "held_run_resume" not in reparked["asked_by"]


async def test_a_deeper_re_park_pushes_the_deeper_tool_onto_the_chain(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api_a = replicas_stack.api(port=replicas_stack.port_a)
    api_b = replicas_stack.api(port=replicas_stack.port_b)
    subject = _subject(uniq("subject"))

    held_run_id = await _submit(api_a, "held_run", {"marker": uniq("q"), "reask": True, "deeper": True}, subject)
    await _await_status(api_b, held_run_id, "parked")

    # The resumed drive re-asks one frame lower (held_deeper), so held_deeper is pushed onto
    # the restored chain — depth two — while the continuation tool stays absent.
    relay_run_id = await _submit(api_b, "caller_relay", {"payload": "first-answer"}, subject)
    view = await _await_status(api_a, relay_run_id, "succeeded")

    outcome = view["result"]
    assert outcome["kind"] == "asks"
    reparked = outcome["asks"][0]
    assert reparked["asked_by"] == ["held_run", "held_deeper"]
    assert "held_run_resume" not in reparked["asked_by"]


async def test_a_caller_ask_is_scoped_to_its_own_subject(replicas_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api_a = replicas_stack.api(port=replicas_stack.port_a)
    api_b = replicas_stack.api(port=replicas_stack.port_b)
    owner = _subject(uniq("owner"))
    stranger = _subject(uniq("stranger"))

    held_run_id = await _submit(api_a, "held_run", {"marker": uniq("q")}, owner)
    await _await_status(api_b, held_run_id, "parked")

    # A run on ANOTHER subject sees nothing on its own subject.
    stranger_run_id = await _submit(api_b, "caller_list", {}, stranger)
    stranger_view = await _await_status(api_a, stranger_run_id, "succeeded")
    assert stranger_view["result"] == []

    # The owning subject still holds the one ask.
    owner_list_id = await _submit(api_a, "caller_list", {}, owner)
    owner_view = await _await_status(api_b, owner_list_id, "succeeded")
    assert len(owner_view["result"]) == 1
    assert owner_view["result"][0]["status"] == "asking"


async def test_a_bad_resume_id_raises_and_cancels_nothing(replicas_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api_a = replicas_stack.api(port=replicas_stack.port_a)
    api_b = replicas_stack.api(port=replicas_stack.port_b)
    subject = _subject(uniq("subject"))

    held_run_id = await _submit(api_a, "held_run", {"marker": uniq("q")}, subject)
    await _await_status(api_b, held_run_id, "parked")

    # Resuming a bogus id on the subject fails loudly.
    bad_run_id = await _submit(api_b, "caller_resume_id", {"interaction_id": "not-a-real-id", "payload": "x"}, subject)
    bad_view = await _await_status(api_a, bad_run_id, "failed")
    assert bad_view["error"]

    # The real ask is untouched — still parked and resumable.
    survive_id = await _submit(api_a, "caller_list", {}, subject)
    survive_view = await _await_status(api_b, survive_id, "succeeded")
    assert len(survive_view["result"]) == 1
    assert survive_view["result"][0]["status"] == "asking"


async def test_two_racing_resumers_resolve_the_ask_exactly_once(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api_a = replicas_stack.api(port=replicas_stack.port_a)
    api_b = replicas_stack.api(port=replicas_stack.port_b)
    subject = _subject(uniq("subject"))

    held_run_id = await _submit(api_a, "held_run", {"marker": uniq("q")}, subject)
    await _await_status(api_b, held_run_id, "parked")

    # Two resumers race on the same subject from different workers: exactly one takes the ask,
    # the other finds it already resolved (or gone) and fails — never two resolutions.
    first_id = await _submit(api_a, "caller_relay", {"payload": "one"}, subject)
    second_id = await _submit(api_b, "caller_relay", {"payload": "two"}, subject)
    first_view = await _await_terminal(api_a, first_id)
    second_view = await _await_terminal(api_b, second_id)

    statuses = sorted([first_view["status"], second_view["status"]])
    assert statuses == ["failed", "succeeded"], statuses
    winner = first_view if first_view["status"] == "succeeded" else second_view
    assert winner["result"]["action"] == "resumed"
    assert winner["result"]["result"]["answer"] in {"one", "two"}
