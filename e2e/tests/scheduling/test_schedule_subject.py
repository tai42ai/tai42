"""The schedule door of the platform state store — a scheduled fire carrying a
``subject`` writes the subject's record under the platform-stamped ``schedule`` door,
and a malformed subject is refused when the schedule is created.

A schedule fire is anonymous/system: the worker sees only the job kwargs, so "this is a
schedule, keyed on this subject" is stamped at creation (the backend ``schedule_task``
wrapper) and re-established at the fire (the worker's ``schedule`` state context). Over
``schedule_stack`` (the scheduling home, backend + scheduler process, access control OFF):

- a schedule whose ``tool_kwargs`` carry a well-formed ``subject`` fires ``state_merge``,
  and the record lands under that subject with the ledger recording the ``schedule`` door
  and a null actor (a system fire has no accountable principal);
- a schedule whose ``subject`` is malformed is refused at ``POST /api/schedules`` — a job
  that could never resolve its subject is never persisted.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async

# A whole-second interval — the rq scheduler re-arms on integer seconds, so a fractional
# period cannot be represented there (mirrors the schedule_task leg).
_INTERVAL_SECONDS = 2

_SUBJECT = {"target_kind": "agent", "target_name": "a-42", "kind": "job", "key": "j1"}


def _record_path(state: str) -> str:
    return (
        f"/api/states/{state}/records/"
        f"{_SUBJECT['target_kind']}/{_SUBJECT['target_name']}/{_SUBJECT['kind']}/{_SUBJECT['key']}"
    )


async def _declare_state(api: ApiClient, name: str) -> None:
    await api.put(
        f"/api/states/{name}",
        json={
            "description": "e2e schedule-keyed status",
            "schema": {"type": "object", "properties": {"last": {"type": "string"}}},
            "subject_kinds": [_SUBJECT["kind"]],
            "default_subject_kind": _SUBJECT["kind"],
        },
    )


async def test_scheduled_job_with_subject_writes_record_via_the_schedule_door(
    schedule_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api = schedule_stack.api(port=schedule_stack.port_a)
    state = uniq("jobstatus")
    await _declare_state(api, state)

    schedule_name = uniq("schedule")
    marker = uniq("tick")
    await api.post(
        "/api/schedules",
        json={
            "tool_name": "state_merge_schedule_task",
            "tool_kwargs": {"state": state, "patch": {"last": marker}, "subject": _SUBJECT},
            "schedule_kwargs": {"backend_schedule_name": schedule_name, "backend_schedule": _INTERVAL_SECONDS},
        },
        retry_on_reloading=True,
    )
    try:

        async def probe() -> dict[str, Any] | None:
            return await api.get(_record_path(state))

        record = await wait_for_async(
            probe, deadline=30.0, message=f"the scheduled fire never wrote the record for {state!r}"
        )
        assert record["data"]["last"] == marker
        assert record["subject"] == _SUBJECT

        writes = (await api.get(f"{_record_path(state)}/writes"))["items"]
        assert writes, "the scheduled write left no ledger row"
        origin = writes[0]["origin"]
        assert origin["door"] == "schedule"
        # A schedule fire is anonymous/system — no accountable principal.
        assert origin["actor"] is None, origin
    finally:
        await api.delete(f"/api/schedules/{schedule_name}", retry_on_reloading=True)


async def test_malformed_schedule_subject_is_refused_at_create(
    schedule_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api = schedule_stack.api(port=schedule_stack.port_a)
    state = uniq("jobstatus")
    await _declare_state(api, state)

    # A partial subject (no target) can never resolve — refused at create, never persisted.
    resp = await api.request_raw(
        "POST",
        "/api/schedules",
        json={
            "tool_name": "state_merge_schedule_task",
            "tool_kwargs": {"state": state, "patch": {"last": "x"}, "subject": {"kind": "job", "key": "j1"}},
            "schedule_kwargs": {"backend_schedule_name": uniq("bad"), "backend_schedule": _INTERVAL_SECONDS},
        },
    )
    assert resp.status_code == 400, resp.text
    assert "subject" in resp.json()["error"].lower(), resp.text
