"""The in-process conversation door tools — ``send_conversation_message`` and
``send_conversation_event`` — driven from inside a run on the real multi-worker bridge stack.

A run posts into a conversation without holding a route key: the turn runs AS the route's execution
key, the run's OWN execution identity is the accountable principal recorded on the submission, and
the answer is delivered by the route's own configured delivery (its callback / poll record), never
inline to the calling tool.

Over ``bridge_stack`` (access control ON, the redis conversations backend, a live turn engine):

- ``test_message_door_posts_under_the_run_identity_and_answers_by_the_route_own_delivery`` — a run
  calls the message door; the tool returns only a receipt (never the answer), the route's own record
  carries the answer, and the accountable principal on the record is the RUN's identity, not the
  route's execution key.
- ``test_event_door_delivers_onto_an_existing_thread_and_the_turn_runs`` — a run calls the event
  door against an existing thread of a tool route; the target turn runs the event.
- ``test_message_door_refuses_a_keyless_schedule_fire`` /
  ``test_event_door_refuses_a_keyless_schedule_fire`` — a schedule created with NO execution key
  fires with no bound execution identity; its tool calls the message / event door in-process and
  the door refuses LOUDLY with ``UnauthenticatedApiCallerError`` (the exception type recorded on
  the fire's failure record), never a silent drop.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async

# The seeded bridge root's key id — the identity a run-tool dispatch runs under, and so the
# accountable principal the door records (distinct from any route execution key a spec mints).
_RUN_IDENTITY = "bridge-root"

# An https callback with nothing behind it: the answer still lands on the route's record (poll),
# the delivery attempt merely fails — so the record is the route's own delivery, never inline.
_UNREACHABLE_CALLBACK = "https://127.0.0.1:9/callback"


def _root_api(bridge_stack: tuple[TaiStack, str]) -> ApiClient:
    stack, root_token = bridge_stack
    return stack.api(stack.port_b).with_token(root_token)


async def _mint_key(api: ApiClient, user_id: str) -> None:
    await api.post(
        "/api/auth/api-keys", json={"user_id": user_id, "description": "e2e door key", "scopes": ["e2e-all"]}
    )


async def _create_tool_api_route(api: ApiClient, *, route_name: str, tool: str, execution_key: str, **jqs: Any) -> None:
    body: dict[str, Any] = {
        "door": "api",
        "target_kind": "tool",
        "target_name": tool,
        "execution_key": execution_key,
        "callback_url": _UNREACHABLE_CALLBACK,
    }
    for name, content in jqs.items():
        body[name] = {"content": content}
    await api.post(f"/api/conversations/{route_name}", json=body, expect=200)


async def _run_tool(api: ApiClient, tool_name: str, arguments: dict[str, Any]) -> dict:
    return await api.post(
        "/api/run-tool", json={"tool_name": tool_name, "arguments": arguments}, expect=200, retry_on_reloading=True
    )


async def test_message_door_posts_under_the_run_identity_and_answers_by_the_route_own_delivery(
    bridge_stack: tuple[TaiStack, str], uniq: Callable[[str], str]
) -> None:
    api = _root_api(bridge_stack)
    route_name = uniq("md-route").replace("_", "-")
    route_key = uniq("md-routekey")
    await _mint_key(api, route_key)
    await _create_tool_api_route(
        api,
        route_name=route_name,
        tool="e2e_echo",
        execution_key=route_key,
        start_expr="{payload: .message}",
        reply_expr=".",
    )

    text = uniq("md-text")
    receipt = await _run_tool(
        api, "send_conversation_message", {"route_name": route_name, "external_user_id": uniq("md-user"), "text": text}
    )
    # The tool returns a receipt only — never the answer inline.
    assert set(receipt) == {"message_id", "thread_id"}, receipt
    message_id = receipt["message_id"]

    # The route answered by its OWN delivery: the answer lands on the route's record.
    async def _answered() -> dict | None:
        record = await api.get(f"/api/conversations/{route_name}/messages/{message_id}")
        return record if record.get("answer_status") == "answered" else None

    record = await wait_for_async(_answered, deadline=20.0, message="the route turn never answered on its own record")
    # The accountable principal is the RUN's execution identity, not the route's execution key.
    assert record["caller_principal"] == _RUN_IDENTITY, record
    assert record["caller_principal"] != route_key, record


async def test_event_door_delivers_onto_an_existing_thread_and_the_turn_runs(
    bridge_stack: tuple[TaiStack, str], uniq: Callable[[str], str]
) -> None:
    stack, _root = bridge_stack
    api = _root_api(bridge_stack)
    route_name = uniq("ed-route").replace("_", "-")
    route_key = uniq("ed-routekey")
    await _mint_key(api, route_key)
    # The target records its ``marker`` kwarg; the route maps an event's payload marker, else the
    # message text, so both the thread-minting message and the event run the same probe.
    await _create_tool_api_route(
        api,
        route_name=route_name,
        tool="e2e_extras_probe",
        execution_key=route_key,
        start_expr="{marker: (if .event then .event.payload.marker else .message end)}",
    )

    # A first message mints the thread the event will address.
    opened = await _run_tool(
        api,
        "send_conversation_message",
        {"route_name": route_name, "external_user_id": uniq("ed-user"), "text": uniq("ed-open")},
    )
    thread_id = opened["thread_id"]

    # The event door delivers a structured event onto that EXISTING thread; the target turn runs it.
    marker = uniq("ed-evt")
    await _run_tool(
        api,
        "send_conversation_event",
        {
            "route_name": route_name,
            "event": {"event_id": uniq("evt-id"), "kind": "e2e.probe", "payload": {"marker": marker}},
            "thread_id": thread_id,
        },
    )

    async def _ran() -> bool | None:
        return True if stack.records(f"extras:{marker}") else None

    await wait_for_async(_ran, deadline=20.0, message="the event turn never ran the target probe")


# A whole-second cadence — the recurring scheduler re-arms on integer seconds.
_KEYLESS_SCHEDULE_INTERVAL_SECONDS = 2


async def _drive_keyless_door_refusal(stack: TaiStack, uniq: Callable[[str], str], door: str) -> None:
    """Create a recurring schedule with NO execution key whose fire calls the ``door`` door
    in-process, wait for the fire's failure record, and assert the door refused with
    ``UnauthenticatedApiCallerError``.

    The schedule fires ``run_tool_schedule_task`` (the recurring branch) with NO ``execution_key``, so
    the backend worker fires it with no bound execution identity; its dispatch runs
    ``e2e_call_conversation_door``, which calls the door in-process. Under the enabled gate a run with
    no accountable execution identity is refused; the probe records the refusal type onto its record
    channel and re-raises, so the refusal is a loud failure the fire surfaces, not a silent drop.
    """
    api = stack.api(stack.port_a)
    schedule_name = uniq("keyless-door")
    route_name = uniq("kd-route").replace("_", "-")
    record_key = uniq("kd-record")
    await api.post(
        "/api/schedules",
        json={
            "tool_name": "run_tool_schedule_task",
            "tool_kwargs": {
                "tool_name": "e2e_call_conversation_door",
                "arguments": {"door": door, "route_name": route_name, "record_key": record_key},
            },
            "schedule_kwargs": {
                "backend_schedule_name": schedule_name,
                "backend_schedule": _KEYLESS_SCHEDULE_INTERVAL_SECONDS,
            },
        },
        expect=200,
        retry_on_reloading=True,
    )
    try:

        async def _refused() -> dict | None:
            records = stack.records(record_key)
            return json.loads(records[0]) if records else None

        record = await wait_for_async(
            _refused, deadline=40.0, message=f"the keyless {door}-door fire never recorded a refusal"
        )
        assert record["error"] == "UnauthenticatedApiCallerError", record
        assert record["door"] == door, record
    finally:
        await api.delete(f"/api/schedules/{schedule_name}", retry_on_reloading=True)


async def test_message_door_refuses_a_keyless_schedule_fire(
    door_schedule_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    await _drive_keyless_door_refusal(door_schedule_stack, uniq, "message")


async def test_event_door_refuses_a_keyless_schedule_fire(
    door_schedule_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    await _drive_keyless_door_refusal(door_schedule_stack, uniq, "event")
