"""The conversation door of the platform state store — a channel turn whose tool is a
preset over ``state_merge`` writes the subject's record with no explicit subject named,
the platform resolving the subject from the ambient conversation context.

Two composed paths run over ``bridge_stack`` (access control ON, the redis conversations
backend, the web + twilio channels):

- a WEB guest on a normal (single-channel) tool target: the state's
  ``default_subject_kind`` is ``thread``, so the turn's ``state_merge`` — carrying no
  subject — lands the record under the guest's thread, and the write ledger records the
  ``conversation`` door with the turn id and the actor (the route's execution key). A
  SECOND guest on the same route writes its OWN record under a different thread.
- a twilio guest on a MULTICHANNEL tool target: the state's ``default_subject_kind`` is
  ``person``, so the same door resolves the record onto the guest's person id (the
  candidate a multichannel target carries) rather than the thread.

The record and its ledger are read back through the platform's own ``/api/states`` doors,
so the assertion is on what the store persisted, not on what the tool returned.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.manifests import BRIDGE_TWILIO_CLIENT, BRIDGE_TWILIO_FROM
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.waiting import wait_for_async
from tai42_e2e.webchat import WebChatClient

from ._bridge_support import TWILIO_INBOUND_PATH, BridgeHarness, post_inbound

# The web channel has no vendor (always real); the twilio leg is the 'twilio' mock leg. No
# scripted-LLM turn runs — a tool target dispatches directly — so the 'llm' seam is unused.
pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("twilio"),
    reason="FakeTwilio is the 'twilio' mock leg; the real leg runs on the creds host",
)

_MERGE_TOOL = "state_merge"
# The inbound text maps to the ``patch`` the preset's ``state_merge`` writes; the preset
# already fixes ``state``, so the turn call is ``state_merge(state=…, patch={last: <text>})``.
_PAYLOAD_EXPR = "{patch: {last: .message}}"


async def _declare_state(api: ApiClient, name: str, default_kind: str) -> None:
    await api.put(
        f"/api/states/{name}",
        json={
            "description": "e2e conversation-keyed status",
            "schema": {"type": "object", "properties": {"last": {"type": "string"}}},
            "subject_kinds": [default_kind],
            "default_subject_kind": default_kind,
        },
    )


async def _create_merge_preset(api: ApiClient, name: str, state: str) -> None:
    await api.post(
        "/api/presets",
        json={
            "name": name,
            "base_tool": _MERGE_TOOL,
            "description": "merge the inbound into the subject's status",
            "fixed_kwargs": {"state": state},
        },
    )


async def _wait_subjects(api: ApiClient, state: str, count: int) -> list[dict[str, Any]]:
    async def probe() -> list[dict[str, Any]] | None:
        page = await api.get(f"/api/states/{state}/subjects")
        subjects = page["subjects"]
        return subjects if len(subjects) >= count else None

    return await wait_for_async(probe, deadline=30.0, message=f"state {state!r} never reached {count} subject(s)")


def _record_path(state: str, subject: dict[str, Any]) -> str:
    return (
        f"/api/states/{state}/records/"
        f"{subject['target_kind']}/{subject['target_name']}/{subject['kind']}/{subject['key']}"
    )


async def test_web_guest_turn_keys_state_on_thread_and_ledgers_the_conversation_door(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    api = bridge.api()  # root client on replica B
    state = uniq("web_status")
    preset = uniq("merge-web").replace("_", "-")
    await _declare_state(api, state, "thread")
    await _create_merge_preset(api, preset, state)

    identity = uniq("l-web-site").replace("_", "-")
    route_name = uniq("l-web-route").replace("_", "-")
    execution_key = uniq("l-web-exec")
    await bridge.mint_key(user_id=execution_key, scopes=["e2e-all"])
    await bridge.create_tool_channel_route(
        route_name=route_name,
        tool=preset,
        execution_key=execution_key,
        channel="web",
        our_identity=identity,
        payload_expr=_PAYLOAD_EXPR,
        reply_expr="null",
    )

    base_url = f"http://{bridge.stack.host}:{bridge.stack.port_b}"
    guest_a, page = await WebChatClient.open_page(base_url, identity, store_url=bridge.stack.resources.redis_url)
    assert page.status_code == 200, page.text
    marker_a = uniq("web-a")
    sent = await guest_a.send(marker_a)
    assert sent.status_code == 200, sent.text

    subjects = await _wait_subjects(api, state, 1)
    subject = subjects[0]["subject"]
    # The turn named no subject; the platform resolved the state's default kind (thread).
    assert subject["kind"] == "thread"
    assert subject["target_kind"] == "tool"
    assert subject["target_name"] == preset

    record = await api.get(_record_path(state, subject))
    assert record["data"]["last"] == marker_a

    # The write ledger carries the conversation door, the turn id, and the actor (the
    # route's execution key the turn ran as) — none of it forgeable by the tool.
    writes = (await api.get(f"{_record_path(state, subject)}/writes"))["items"]
    assert writes, "the conversation turn wrote no ledger row"
    origin = writes[0]["origin"]
    assert origin["door"] == "conversation"
    # The actor is the turn's accountable principal (the guest's caller identity the
    # attribution stamped), never the route's static execution key — the platform stamps
    # it, so the tool cannot forge who wrote.
    assert origin["actor"], origin
    assert origin["actor"] != execution_key, origin
    assert origin["turn_id"], f"the conversation write recorded no turn id: {origin!r}"
    # The consumer the state tool stamped — the invoked tool's own name (the preset, or the
    # base ``state_merge`` when the preset dispatches under the base's identity).
    assert origin["consumer"] in {_MERGE_TOOL, preset}, origin

    # A SECOND guest on the same route writes its OWN record under a different thread.
    guest_b, page_b = await WebChatClient.open_page(base_url, identity, store_url=bridge.stack.resources.redis_url)
    assert page_b.status_code == 200, page_b.text
    assert guest_b.visitor_id != guest_a.visitor_id
    marker_b = uniq("web-b")
    assert (await guest_b.send(marker_b)).status_code == 200

    two = await _wait_subjects(api, state, 2)
    keys = {s["subject"]["key"] for s in two}
    assert len(keys) == 2, f"the second guest did not get its own thread record: {two!r}"


async def test_multichannel_target_turn_keys_state_on_person(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    api = bridge.api()
    state = uniq("person_status")
    preset = uniq("merge-person").replace("_", "-")
    await _declare_state(api, state, "person")
    await _create_merge_preset(api, preset, state)

    # The target is multichannel, so the door carries a person candidate; the state's
    # default kind is ``person``, so the record keys on the person id, not the thread.
    await bridge.set_target_config(target_kind="tool", target_name=preset, multichannel=True)
    execution_key = uniq("l-person-exec")
    await bridge.mint_key(user_id=execution_key, scopes=["e2e-all"])
    route_name = uniq("l-person-route").replace("_", "-")
    await bridge.create_tool_channel_route(
        route_name=route_name,
        tool=preset,
        execution_key=execution_key,
        channel="twilio",
        our_identity=BRIDGE_TWILIO_FROM,
        payload_expr=_PAYLOAD_EXPR,
        reply_expr="null",
    )

    marker = uniq("person-msg")
    port = bridge.stack.port_b
    inbound = bridge.twilio_inbound(
        our_identity=BRIDGE_TWILIO_FROM, client=BRIDGE_TWILIO_CLIENT, text=marker, port=port
    )
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound, port=port)).status_code == 204

    subjects = await _wait_subjects(api, state, 1)
    subject = subjects[0]["subject"]
    assert subject["kind"] == "person"
    assert subject["key"], "the multichannel turn recorded no person id"

    record = await api.get(_record_path(state, subject))
    assert record["data"]["last"] == marker
    writes = (await api.get(f"{_record_path(state, subject)}/writes"))["items"]
    assert writes[0]["origin"]["door"] == "conversation"
    # The accountable principal of a person-keyed turn is that very person — the actor the
    # platform stamps equals the subject's person id.
    assert writes[0]["origin"]["actor"] == subject["key"], writes[0]["origin"]
