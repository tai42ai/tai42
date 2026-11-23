"""An inbound the channel recognises but cannot read gets the one unreadable notice + event — never dropped.

The user reads "That message could not be read. Please send it again." once, the platform emits one
``conversations_inbound_rejected`` event carrying ``reason == "unreadable"``, and the webhook acks
2xx so the vendor stops redelivering:
- a whatsapp completed-form reply (``nfm_reply``) whose ``response_json`` is not a JSON object; a
  redelivery of the same message sends no second notice;
- a message whose text is blank on whatsapp (with a question pending, which stays open for the next
  reply), telegram or slack.

Driven over the live media-bridge stack; the channels mock over their in-process stubs, so any real
selection breaks the stubs and the module steps aside.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any, cast

import pytest

from tai42_e2e.manifests import BRIDGE_WHATSAPP_CLIENT, BRIDGE_WHATSAPP_PHONE_ID
from tai42_e2e.provider_stub import SignedInbound
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.waiting import wait_absent_async

from ._bridge_support import (
    SLACK_INBOUND_PATH,
    TELEGRAM_INBOUND_PATH,
    WHATSAPP_INBOUND_PATH,
    BridgeHarness,
    post_inbound,
    wait_probe_record,
    wait_whatsapp_send,
)
from ._rejection_support import REJECTION_NOTICES, assert_rejected, notices_to

pytestmark = [
    pytest.mark.needs(
        "kind:identity", "probe-tools", "setting:conversations", "setting:seeded-access-control", "store:redis"
    ),
    pytest.mark.skipif(
        any(HarnessSettings().is_real(seam) for seam in ("telegram", "slack", "twilio", "whatsapp", "llm")),
        reason="the media-bridge stubs are the mock leg; real legs run under TAI_E2E_REAL",
    ),
]

# Far enough out that the pending question is answered before it expires.
_PARK_SECONDS = 600.0


@pytest.mark.needs("kind:channels:whatsapp", "helper:whatsapp")
async def test_whatsapp_form_reply_without_a_json_object_is_rejected_unreadable_once(
    media_bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    wamid = f"wamid.{uniq('wa-unreadable')}"

    def _reply() -> SignedInbound:
        return media_bridge.whatsapp_nfm_reply(
            phone_number_id=BRIDGE_WHATSAPP_PHONE_ID,
            wa_id=BRIDGE_WHATSAPP_CLIENT,
            response={},
            wamid=wamid,
            response_json='["not", "an", "object"]',
        )

    await assert_rejected(
        media_bridge,
        uniq,
        inbound_path=WHATSAPP_INBOUND_PATH,
        inbound=_reply(),
        fake=media_bridge.fake_whatsapp,
        recipient=BRIDGE_WHATSAPP_CLIENT,
        recipient_field="to",
        reason="unreadable",
    )
    before = len(notices_to(media_bridge.fake_whatsapp, REJECTION_NOTICES["unreadable"], BRIDGE_WHATSAPP_CLIENT, "to"))

    # The vendor redelivers the same message: acked, and the user reads no second notice.
    redelivered = await post_inbound(
        media_bridge.stack, WHATSAPP_INBOUND_PATH, _reply(), port=media_bridge.stack.port_b
    )
    assert redelivered.status_code in (200, 204), redelivered.text

    async def _second_notice() -> list[dict] | None:
        sent = notices_to(media_bridge.fake_whatsapp, REJECTION_NOTICES["unreadable"], BRIDGE_WHATSAPP_CLIENT, "to")
        return sent if len(sent) > before else None

    await wait_absent_async(_second_notice, window=3.0, message="a redelivered unreadable reply sent a second notice")


@pytest.mark.needs("kind:channels:whatsapp", "kind:interactions", "helper:whatsapp")
async def test_whatsapp_blank_text_while_a_question_is_pending_is_rejected_unreadable_and_the_ask_stays(
    media_bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    wa_id = f"1650{uuid.uuid4().int % 10**9:09d}"
    question, answer = uniq("wa-blank-q"), uniq("wa-blank-a")
    async with media_bridge.stack.mcp(port=media_bridge.stack.port_a, auth=media_bridge.root_token) as mcp:
        parked = await mcp.call_tool(
            "e2e_async_park_driver",
            {"question": question, "expiry_seconds": _PARK_SECONDS, "channel": "whatsapp", "recipient": wa_id},
        )
    interaction_id = cast("dict[str, Any]", parked.data)["interaction_id"]
    await wait_whatsapp_send(media_bridge.fake_whatsapp, question)

    await assert_rejected(
        media_bridge,
        uniq,
        inbound_path=WHATSAPP_INBOUND_PATH,
        inbound=media_bridge.whatsapp_inbound(phone_number_id=BRIDGE_WHATSAPP_PHONE_ID, wa_id=wa_id, text="   "),
        fake=media_bridge.fake_whatsapp,
        recipient=wa_id,
        recipient_field="to",
        reason="unreadable",
    )

    # The blank never answered the question: the user's next real reply does.
    reply = media_bridge.whatsapp_inbound(phone_number_id=BRIDGE_WHATSAPP_PHONE_ID, wa_id=wa_id, text=answer)
    resp = await post_inbound(media_bridge.stack, WHATSAPP_INBOUND_PATH, reply, port=media_bridge.stack.port_b)
    assert resp.status_code in (200, 204), resp.text
    (resumed,) = await wait_probe_record(media_bridge, f"async_resume:{interaction_id}", deadline=20.0)
    assert resumed["branch"] == "answer", resumed
    assert json.loads(resumed["value"]) == answer


@pytest.mark.needs("kind:channels:telegram", "helper:telegram")
async def test_telegram_blank_text_is_rejected_unreadable(
    media_bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    chat_id = "970002"
    await assert_rejected(
        media_bridge,
        uniq,
        inbound_path=TELEGRAM_INBOUND_PATH,
        inbound=media_bridge.telegram_inbound_text(chat_id=chat_id, text="   "),
        fake=media_bridge.fake_telegram,
        recipient=chat_id,
        recipient_field="chat_id",
        reason="unreadable",
    )


@pytest.mark.needs("kind:channels:slack", "helper:slack")
async def test_slack_blank_message_is_rejected_unreadable(
    media_bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    channel = f"C0{uniq('slk-blank').upper().replace('_', '').replace('-', '')[:8]}"
    inbound = media_bridge.slack_inbound_text(
        channel=channel, thread_ts="1700000000.000100", text="   ", event_id=f"Ev{uniq('slk-blank-ev')}"
    )
    await assert_rejected(
        media_bridge,
        uniq,
        inbound_path=SLACK_INBOUND_PATH,
        inbound=inbound,
        fake=media_bridge.slack,
        recipient=channel,
        recipient_field="channel",
        reason="unreadable",
    )
