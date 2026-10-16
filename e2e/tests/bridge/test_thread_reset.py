"""Resetting one conversation thread now forgets that thread's declared-state record too.

A twilio turn on a single-channel tool target whose tool is a preset over ``state_merge``
writes the thread-subject record of a thread-keyed state (the state's ``default_subject_kind``
is ``thread``, so the record keys on the turn's own thread id). The operator then resets the
thread through ``DELETE /api/conversations/{route}/thread?thread_id=``, and the proof the reset
is a COMPLETE forget is twofold:

1. the delete response carries ``states_erased >= 1`` alongside the thread teardown — the
   thread-subject's declared-state record was erased in the same call;
2. afterward the record is GONE (a read returns ``null`` and the state carries no subject), and
   the thread is gone the ordinary way — its transcript reads the uniform 404 and the route
   listing no longer carries it.

The write mirrors ``test_state_turn`` (the conversation door of the state store); the reset
door + its transcript/listing teardown mirror ``test_thread_delete``. The DIFFERENCE from
``test_person_forget`` is the subject: a THREAD subject's state, cleared by a single-thread
reset, not a person's.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import pytest

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.manifests import BRIDGE_TWILIO_CLIENT, BRIDGE_TWILIO_FROM
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.waiting import wait_for_async

from ._bridge_support import TWILIO_INBOUND_PATH, BridgeHarness, post_inbound

# The twilio signed inbound is the 'twilio' mock leg; a tool target dispatches directly, so no
# scripted-LLM turn runs (the 'llm' seam is unused). The checkpoint provider must be configured
# all the same: the reset deletes the thread's agent checkpoint unconditionally.
pytestmark = [
    pytest.mark.needs(
        "kind:channels:twilio",
        "kind:identity",
        "helper:twilio",
        "setting:checkpoint:memory",
        "setting:conversations",
        "setting:router:presets",
        "setting:router:states",
        "setting:seeded-access-control",
        "setting:tool:state_merge",
    ),
    pytest.mark.skipif(
        HarnessSettings().is_real("twilio"),
        reason="FakeTwilio is the 'twilio' mock leg; the real leg runs on the creds host",
    ),
]

_MERGE_TOOL = "state_merge"
# The inbound text maps to the ``patch`` the preset's ``state_merge`` writes; the preset already
# fixes ``state``, so the turn call is ``state_merge(state=…, patch={last: <text>})``.
_PAYLOAD_EXPR = "{patch: {last: .message}}"


async def _declare_thread_state(api: ApiClient, name: str) -> None:
    """Declare a state that keys on the conversation THREAD (its default subject kind)."""
    await api.put(
        f"/api/states/{name}",
        json={
            "description": "e2e thread-keyed status",
            "schema": {"type": "object", "properties": {"last": {"type": "string"}}},
            "subject_kinds": ["thread"],
            "default_subject_kind": "thread",
        },
    )


async def _create_merge_preset(api: ApiClient, name: str, state: str) -> None:
    await api.post(
        "/api/presets",
        json={
            "name": name,
            "base_tool": _MERGE_TOOL,
            "description": "merge the inbound into the thread's status",
            "fixed_kwargs": {"state": state},
        },
    )


async def _wait_one_subject(api: ApiClient, state: str) -> dict[str, Any]:
    """The lone subject the state carries, once the turn's write has landed."""

    async def probe() -> list[dict[str, Any]] | None:
        subjects = (await api.get(f"/api/states/{state}/subjects"))["subjects"]
        return subjects if subjects else None

    subjects = await wait_for_async(probe, deadline=30.0, message=f"state {state!r} never carried a subject")
    assert len(subjects) == 1, f"expected exactly one subject under {state!r}, saw {subjects!r}"
    return subjects[0]["subject"]


async def _wait_thread_listed(api: ApiClient, route_name: str, thread_id: str) -> None:
    """Wait until the conversation thread is recorded and listed under its route."""

    async def probe() -> bool | None:
        listing = await api.get(f"/api/conversations/{route_name}/threads")
        return True if thread_id in [item["thread_id"] for item in listing["items"]] else None

    await wait_for_async(probe, deadline=30.0, message=f"thread {thread_id!r} was never listed on route {route_name!r}")


def _record_path(state: str, subject: dict[str, Any]) -> str:
    return (
        f"/api/states/{state}/records/"
        f"{subject['target_kind']}/{subject['target_name']}/{subject['kind']}/{subject['key']}"
    )


def _transcript_path(route_name: str, thread_id: str) -> str:
    return f"/api/conversations/{route_name}/transcript?{urlencode({'thread_id': thread_id})}"


def _thread_delete_path(route_name: str, thread_id: str) -> str:
    """The reset door — the thread id rides ``?thread_id=``, the same as the transcript read door."""
    return f"/api/conversations/{route_name}/thread?{urlencode({'thread_id': thread_id})}"


async def test_resetting_a_thread_erases_its_thread_subject_state_record(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    api = bridge.api()  # root client on replica B
    port = bridge.stack.port_b
    state = uniq("thread_status")
    preset = uniq("merge-thread").replace("_", "-")
    await _declare_thread_state(api, state)
    await _create_merge_preset(api, preset, state)

    # A single-channel tool target: no multichannel opt-in, so the turn carries no person
    # candidate and the thread-default state keys on the turn's own thread, not a person.
    execution_key = uniq("l-reset-exec")
    await bridge.mint_key(user_id=execution_key, scopes=["e2e-all"])
    route_name = uniq("l-reset-route").replace("_", "-")
    await bridge.create_tool_channel_route(
        route_name=route_name,
        tool=preset,
        execution_key=execution_key,
        channel="twilio",
        our_identity=BRIDGE_TWILIO_FROM,
        start_expr=_PAYLOAD_EXPR,
        reply_expr="null",
    )

    # One turn writes the thread-subject record and records the conversation thread.
    marker = uniq("reset-msg")
    inbound = bridge.twilio_inbound(
        our_identity=BRIDGE_TWILIO_FROM, client=BRIDGE_TWILIO_CLIENT, text=marker, port=port
    )
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound, port=port)).status_code == 204

    # The record landed under the THREAD subject, keyed on the turn's own thread id.
    subject = await _wait_one_subject(api, state)
    assert subject["kind"] == "thread"
    assert subject["target_kind"] == "tool"
    assert subject["target_name"] == preset
    thread_id = subject["key"]

    # (present) the record reads back the written value, and the conversation thread is listed.
    record = await api.get(_record_path(state, subject))
    assert record is not None, "the thread-subject state record was never written"
    assert record["data"]["last"] == marker
    await _wait_thread_listed(api, route_name, thread_id)
    assert (await api.get(_transcript_path(route_name, thread_id)))["total"] >= 1

    # Reset the thread: the shipped delete door forgets it AND erases its declared-state record,
    # reporting the count as ``states_erased`` alongside the thread teardown.
    reset = await api.delete(_thread_delete_path(route_name, thread_id))
    assert reset["thread_id"] == thread_id
    assert reset["states_erased"] >= 1, reset

    # (gone) the record is forgotten — a read returns null and the state carries no subject.
    assert (await api.get(_record_path(state, subject))) is None, "the thread-subject record survived the reset"
    assert (await api.get(f"/api/states/{state}/subjects"))["subjects"] == []

    # (gone) the thread is forgotten the ordinary way: the transcript is the uniform 404 and the
    # route listing no longer carries it.
    await api.get(_transcript_path(route_name, thread_id), expect=404)
    listing = await api.get(f"/api/conversations/{route_name}/threads")
    assert thread_id not in [item["thread_id"] for item in listing["items"]]
