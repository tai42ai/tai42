"""The hook door of the parkable-door contract: a webhook fire evaluates the hook's four door jqs
(``start_expr`` / ``cancel_expr`` / ``resume_expr`` / ``extras_expr``) over the event payload with
the run's parked interactions bound as ``$parked``, under the hook's bound execution key and its
subject context.

Over ``replicas_stack`` (the webhook home, backend on, access control off):

- ``test_hook_start_and_extras_expr_drive_the_fire`` — a hook whose ``start_expr`` maps the event
  payload to the fired tool's kwargs and whose ``extras_expr`` builds the run extras; the probe
  records both (a hook is receiver-less, so the return is observed through its side effect).
- ``test_hook_resume_of_a_caller_ask_waits_on_the_subject_and_a_later_run_takes_it`` — a caller ask
  parked on a subject is resumed by a hook fire (``resume_expr`` reads ``$parked``); the resumed
  result waits on the subject as a finished outcome, and a later run on that subject takes it.
- ``test_hook_cancel_expr_cancels_a_parked_interaction`` — a hook fire's ``cancel_expr`` names a
  parked interaction and whole-chain kills it, so the subject no longer lists it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async

_PARK_EXPIRY_SECONDS = 3600.0

# The subject scope a caller ask parks under and the hook resumes on.
_TARGET_KIND = "agent"
_TARGET_NAME = "a-42"
_SUBJECT_KIND = "job"


def _subject(key: str) -> dict[str, str]:
    return {"target_kind": _TARGET_KIND, "target_name": _TARGET_NAME, "kind": _SUBJECT_KIND, "key": key}


async def _register_hook(api: ApiClient, *, topic: str, tool: str, execution_key: str, **contract: Any) -> None:
    body: dict[str, Any] = {"name": f"{topic}-hook", "topic": topic, "tool": tool, "execution_key": execution_key}
    body.update(contract)
    await api.post("/api/hooks", json=body)


async def _fire(api: ApiClient, topic: str, payload: dict[str, Any]) -> None:
    await api.request_raw("POST", f"/universal_webhook/{topic}", json=payload)


async def _run_tool(api: ApiClient, tool_name: str, arguments: dict[str, Any], subject: dict[str, str]) -> Any:
    return await api.post(
        "/api/run-tool",
        json={"tool_name": tool_name, "arguments": arguments, "subject": subject},
        expect=200,
        retry_on_reloading=True,
    )


async def _parked(api: ApiClient, subject: dict[str, str]) -> list[dict[str, Any]]:
    return await _run_tool(api, "list_parked", {}, subject)


async def test_hook_start_and_extras_expr_drive_the_fire(replicas_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = replicas_stack.api(port=replicas_stack.port_a)
    topic = uniq("hook-topic").replace("_", "-")
    marker = uniq("hook-marker")
    tag = uniq("hook-tag")
    await _register_hook(
        api,
        topic=topic,
        tool="e2e_extras_probe",
        execution_key=uniq("hook-exec"),
        start_expr={"content": "{marker: .marker}"},
        extras_expr={"content": f'{{tag: "{tag}"}}'},
    )
    await _fire(api, topic, {"marker": marker})

    async def _record() -> dict[str, Any] | None:
        records = replicas_stack.records(f"extras:{marker}")
        return json.loads(records[0]) if records else None

    record = await wait_for_async(_record, deadline=20.0, message=f"the hook fire never ran the probe for {marker!r}")
    # start_expr mapped the event payload to the tool's kwargs, and extras_expr reached the run.
    assert record["marker"] == marker, record
    assert record["extras"] == {"tag": tag}, record


async def test_hook_resume_of_a_caller_ask_waits_on_the_subject_and_a_later_run_takes_it(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api = replicas_stack.api(port=replicas_stack.port_a)
    subject = _subject(uniq("job"))
    answer = uniq("hook-answer")

    # One hook drives both halves off ``$parked``: the first fire (nothing parked) STARTS the held
    # caller ask on the subject through the receiver-less hook door — so its resumed outcome
    # subject-tracks rather than delivering to a live caller; the second fire (the ask now parked)
    # RESUMES it with the event's answer.
    topic = uniq("hook-topic").replace("_", "-")
    await _register_hook(
        api,
        topic=topic,
        tool="e2e_caller_hold",
        execution_key=uniq("hook-exec"),
        subject={
            "target_kind": _TARGET_KIND,
            "target_name": _TARGET_NAME,
            "kind": _SUBJECT_KIND,
            "key_expr": {"content": ".key"},
        },
        start_expr={
            "content": (
                f"if ($parked | length) > 0 then null else "
                f'{{question: "{uniq("hook-q")}", expiry_seconds: {_PARK_EXPIRY_SECONDS}}} end'
            )
        },
        resume_expr={"content": "if ($parked | length) > 0 then {id: $parked[0].id, payload: .answer} else null end"},
    )

    # Fire 1 starts the caller ask; poll until it is parked and read its id.
    await _fire(api, topic, {"key": subject["key"]})

    async def _asking() -> str | None:
        entry = next((e for e in await _parked(api, subject) if e["status"] == "asking"), None)
        return entry["id"] if entry is not None else None

    await wait_for_async(_asking, deadline=20.0, message="the hook fire never parked a caller ask")

    # Fire 2 resumes it; the resumed result waits on the subject as a finished outcome (keyed by the
    # run's completion id — the caller-ask entry is consumed and a waiting outcome takes its place).
    await _fire(api, topic, {"key": subject["key"], "answer": answer})

    async def _finished() -> str | None:
        entry = next((e for e in await _parked(api, subject) if e["status"] == "finished"), None)
        return entry["id"] if entry is not None else None

    outcome_id = await wait_for_async(
        _finished, deadline=20.0, message="the hook resume never left a finished outcome on the subject"
    )

    # A later run on the subject TAKES the waiting outcome (resume_parked with no payload = a take).
    taken = await _run_tool(api, "resume_parked", {"interaction_id": outcome_id}, subject)
    assert taken["action"] == "taken", taken
    assert answer in json.dumps(taken), taken
    # The take consumed it — the subject no longer lists the outcome.
    assert outcome_id not in [e["id"] for e in await _parked(api, subject)]


async def test_hook_cancel_expr_cancels_a_parked_interaction(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api = replicas_stack.api(port=replicas_stack.port_a)
    subject = _subject(uniq("job"))

    parked = await _run_tool(
        api, "e2e_caller_hold", {"question": uniq("hook-q"), "expiry_seconds": _PARK_EXPIRY_SECONDS}, subject
    )
    interaction_id = parked["asks"][0]["id"]
    assert interaction_id in [e["id"] for e in await _parked(api, subject)]

    topic = uniq("hook-topic").replace("_", "-")
    await _register_hook(
        api,
        topic=topic,
        tool="e2e_echo",
        execution_key=uniq("hook-exec"),
        subject={
            "target_kind": _TARGET_KIND,
            "target_name": _TARGET_NAME,
            "kind": _SUBJECT_KIND,
            "key_expr": {"content": ".key"},
        },
        start_expr={"content": "null"},
        cancel_expr={"content": "if ($parked | length) > 0 then $parked[0].id else null end"},
    )
    await _fire(api, topic, {"key": subject["key"]})

    async def _gone() -> bool | None:
        return True if interaction_id not in [e["id"] for e in await _parked(api, subject)] else None

    await wait_for_async(_gone, deadline=20.0, message="the hook cancel never removed the parked interaction")
