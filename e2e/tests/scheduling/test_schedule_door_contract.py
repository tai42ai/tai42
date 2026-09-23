"""The schedule door of the parkable-door contract: a recurring fire carries an
``execution_key`` and the four door jqs into the worker, so a fire's run can async-park (rebindable
and resumable under that key) with ``$parked`` bound in the worker; the create door refuses a
contract jq with no key and refuses a reserved schedule-door key.

Over ``schedule_stack`` (the scheduling home — backend + scheduler process, access control off),
on every backend leg:

- ``test_schedule_door_contract_parks_from_the_worker`` — a schedule whose ``execution_key`` and
  ``start_expr`` are bound in the worker fires ``run_tool_schedule_task``, whose dispatch async-asks
  and parks. The parking probe records the execution identity it ran under; the recorded identity is
  the schedule's ``execution_key`` — proof the key AND the ``start_expr`` jq bind in the worker and
  the fire's run reaches an async park.
- ``test_schedule_contract_jq_requires_execution_key`` — a contract jq with no ``execution_key`` is
  refused at ``POST /api/schedules``: a park it raised could never be rebound.
- ``test_reserved_schedule_door_key_refused_at_create`` — a caller-supplied ``backend_schedule_*``
  job kwarg (a key the platform alone stamps) is refused at the create enqueue door.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async

# A whole-second cadence — the rq scheduler re-arms on integer seconds.
_INTERVAL_SECONDS = 2

# Far enough out that the parked ask stays pending for the read — the fire's park resolves by an
# answer or its deadline, neither of which this suite drives.
_PARK_EXPIRY_SECONDS = 3600.0


async def test_schedule_door_contract_parks_from_the_worker(
    schedule_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api = schedule_stack.api(port=schedule_stack.port_a)
    schedule_name = uniq("schedule")
    execution_key = uniq("sched-exec")
    record_key = uniq("sched-park")
    question = uniq("sched-q")

    # ``start_expr`` (bound in the worker) builds the dispatch that async-parks — the parking probe
    # records the identity it ran under. ``run_tool_schedule_task``'s stored kwargs are a valid
    # placeholder; the worker-bound ``start_expr`` replaces them each fire.
    dispatch = (
        f'{{tool_name: "e2e_schedule_park", arguments: '
        f'{{question: "{question}", expiry_seconds: {_PARK_EXPIRY_SECONDS}, record_key: "{record_key}"}}}}'
    )
    await api.post(
        "/api/schedules",
        json={
            "tool_name": "run_tool_schedule_task",
            "tool_kwargs": {"tool_name": "e2e_echo", "arguments": {"payload": "placeholder"}},
            "schedule_kwargs": {"backend_schedule_name": schedule_name, "backend_schedule": _INTERVAL_SECONDS},
            "execution_key": execution_key,
            "start_expr": {"content": dispatch},
        },
        retry_on_reloading=True,
    )
    try:

        async def _record() -> dict[str, Any] | None:
            records = schedule_stack.records(record_key)
            return json.loads(records[0]) if records else None

        record = await wait_for_async(
            _record, deadline=30.0, message=f"the scheduled fire never parked (no {record_key!r} record)"
        )
        # The recorded identity is the schedule's own execution key — the key bound in the worker.
        assert record["identity"] == execution_key, record
        assert record["interaction_id"], record
    finally:
        await api.delete(f"/api/schedules/{schedule_name}", retry_on_reloading=True)


async def test_schedule_contract_jq_requires_execution_key(
    schedule_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api = schedule_stack.api(port=schedule_stack.port_a)
    # A door-contract jq with no execution_key is refused: a park the fire raised could never be
    # rebound and resumed, so the job is never persisted.
    resp = await api.request_raw(
        "POST",
        "/api/schedules",
        json={
            "tool_name": "run_tool_schedule_task",
            "tool_kwargs": {},
            "schedule_kwargs": {"backend_schedule_name": uniq("bad"), "backend_schedule": _INTERVAL_SECONDS},
            "start_expr": {"content": '{tool_name: "e2e_echo", arguments: {payload: .}}'},
        },
    )
    assert resp.status_code == 400, resp.text
    assert "execution_key" in resp.text, resp.text


async def test_reserved_schedule_door_key_refused_at_create(
    schedule_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api = schedule_stack.api(port=schedule_stack.port_a)
    # A caller-supplied reserved schedule-door kwarg (the platform alone stamps these) is refused at
    # the create enqueue door, so a forged fire subject/identity/binding can never reach the worker.
    resp = await api.request_raw(
        "POST",
        "/api/schedules",
        json={
            "tool_name": "e2e_record",
            "tool_kwargs": {"backend_schedule_forged": "x"},
            "schedule_kwargs": {"backend_schedule_name": uniq("bad"), "backend_schedule": _INTERVAL_SECONDS},
        },
    )
    assert resp.status_code == 400, resp.text
    assert "backend_schedule_forged" in resp.text, resp.text
