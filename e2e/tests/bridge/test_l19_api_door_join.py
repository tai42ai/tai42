"""L19 — an api-door conversation joins a person, and the api caller reads the merged thread.

An AGENT target reachable on a channel route (twilio) and an api route (person keys are
agent-only). The person mints a code on the channel side and submits it through the API
DOOR — a plain authed ``POST`` whose caller principal is the api conversation's identity
(api-door conversations can join persons). The redeem answers ``linked``.

Two switch-points are pinned at the door. The redeem response's ``thread_id`` is the api
leg's OLD route-keyed thread — the key is fixed at accept, BEFORE the merge lands, so this
turn still belongs to the pre-link thread. The very NEXT api message keys the aggregated
``bridge:@person:{id}`` thread instead. Then, after a post-merge message on each leg, the
SAME api caller reads the person thread through the api door and is served BOTH legs' records —
the full merged history, not the api leg's slice.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import quote, urlencode

import pytest
from _bridge_support import (
    LINKED_TEXT,
    TWILIO_INBOUND_PATH,
    BridgeHarness,
    extract_pair_code,
    post_inbound,
    script_reply,
    wait_send_to,
    wait_twilio_send,
)

from tai42_e2e.manifests import BRIDGE_TWILIO_CLIENT, BRIDGE_TWILIO_FROM
from tai42_e2e.settings import HarnessSettings

pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("llm") or HarnessSettings().is_real("twilio"),
    reason="scripted-LLM + FakeTwilio are the 'llm'/'twilio' mock leg; real on the creds host",
)

_AGENT = "tools_agent"
# The bridge thread namespaces: a route-keyed thread is ``bridge:{route}:{address}``; a
# linked person's aggregated thread is ``bridge:@person:{person_id}``.
_BRIDGE_PREFIX = "bridge:"
_PERSON_PREFIX = "bridge:@person:"
_UNREACHABLE_CALLBACK = "https://127.0.0.1:9/callback"


def _transcript_path(route_name: str, thread_id: str) -> str:
    return f"/api/conversations/{route_name}/transcript?{urlencode({'thread_id': thread_id})}"


async def test_api_caller_joins_a_person_and_reads_the_merged_thread(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    port = bridge.stack.port_b
    await bridge.set_target_config(target_kind="agent", target_name=_AGENT, multichannel=True)

    # The channel leg: a twilio route that mints a code with /link.
    exec_ch = uniq("l19-exec-ch")
    await bridge.mint_key(user_id=exec_ch, scopes=["e2e-all"])
    route_ch = uniq("l19-route-ch").replace("_", "-")
    await bridge.create_channel_route(
        route_name=route_ch, agent=_AGENT, execution_key=exec_ch, channel="twilio", our_identity=BRIDGE_TWILIO_FROM
    )
    # The api leg: an api route, plus the caller key whose principal IS the api conversation's
    # identity (the turn runs as the route's execution key, not the caller).
    exec_api = uniq("l19-exec-api")
    await bridge.mint_key(user_id=exec_api, scopes=["e2e-all"])
    route_api = uniq("l19-route-api").replace("_", "-")
    await bridge.create_api_route(
        route_name=route_api, agent=_AGENT, execution_key=exec_api, callback_url=_UNREACHABLE_CALLBACK
    )
    principal = uniq("l19-caller")
    caller = bridge.api(token=await bridge.mint_key(user_id=principal, scopes=["e2e-all"]))
    end_user = uniq("l19-user")
    old_key = f"{_BRIDGE_PREFIX}{route_api}:{quote(principal, safe='')}/{end_user}"

    # Mint the code on the channel side.
    link_inbound = bridge.twilio_inbound(
        our_identity=BRIDGE_TWILIO_FROM, client=BRIDGE_TWILIO_CLIENT, text="/link", port=port
    )
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, link_inbound, port=port)).status_code == 204
    (link_reply,) = await wait_send_to(bridge.fake_twilio, to=BRIDGE_TWILIO_CLIENT, needle="Your pairing code is ")
    code = extract_pair_code(link_reply["body"])

    # Submit the code through the api door. It answers ``linked`` — and its thread_id is the
    # api leg's OLD route key, because the key is fixed at accept, before the merge lands.
    redeem = await caller.post(
        f"/api/conversations/{route_api}/messages",
        json={"external_user_id": end_user, "text": code, "wait_seconds": 20},
        expect=200,
    )
    assert redeem["answer"]["status"] == "answered"
    assert redeem["answer"]["answer"] == LINKED_TEXT
    assert redeem["thread_id"] == old_key, redeem["thread_id"]

    # The two post-merge messages, one per leg, both key the aggregated person thread now.
    api_text = uniq("l19-api-in")
    ch_text = uniq("l19-ch-in")
    api_answer = uniq("l19-api-ans")
    ch_answer = uniq("l19-ch-ans")
    script_reply(bridge.llm_stub, api_answer, ch_answer)

    # Post-merge on the api leg: its thread_id is now the person key, and its answer is a real
    # agent turn (not a pairing reply).
    api_msg = await caller.post(
        f"/api/conversations/{route_api}/messages",
        json={"external_user_id": end_user, "text": api_text, "wait_seconds": 20},
        expect=200,
    )
    assert api_msg["answer"]["answer"] == api_answer
    assert api_msg["thread_id"].startswith(_PERSON_PREFIX), api_msg["thread_id"]
    person_thread = api_msg["thread_id"]
    assert person_thread != old_key

    # Post-merge on the channel leg.
    ch_inbound = bridge.twilio_inbound(
        our_identity=BRIDGE_TWILIO_FROM, client=BRIDGE_TWILIO_CLIENT, text=ch_text, port=port
    )
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, ch_inbound, port=port)).status_code == 204
    await wait_twilio_send(bridge.fake_twilio, ch_answer)

    # The SAME api caller reads the person thread through the api door and is served BOTH legs'
    # post-merge records — the merged history, aggregated across the person's routes.
    transcript = await caller.get(_transcript_path(route_api, person_thread))
    assert transcript["total"] == 2, transcript
    by_text = {item["inbound_text"]: item for item in transcript["items"]}
    assert set(by_text) == {api_text, ch_text}, set(by_text)
    # The aggregate genuinely spans BOTH doors: the api leg's record names this caller
    # principal, the channel leg's names none — one merged history across the two routes.
    assert by_text[api_text]["caller_principal"] == principal
    assert by_text[ch_text]["caller_principal"] is None
    assert by_text[api_text]["answer"] == api_answer
    assert by_text[ch_text]["answer"] == ch_answer
