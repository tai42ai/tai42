"""Records that carry a quoted message's text, for the tests proving a forget or an erase leaves
no store holding it."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

from tai42_contract.conversations import ReplyTo, ReplyToStatus

from tai42_skeleton.conversations.models import AskReply, AskSend, ConversationRecord, DeliveryStatus

QUOTED_TEXT = "the words a reply quoted"


def quoting_records(route_name: str, thread_id: str, address: str, identity: str) -> list[ConversationRecord]:
    """An inbound record whose quote was found, and an answered ask whose reply quoted the same text."""
    now = time.time()
    quote = ReplyTo(message_id="mid-quoted", status=ReplyToStatus.FOUND, text=QUOTED_TEXT)
    shared = {
        "route_name": route_name,
        "door": "channel",
        "thread_id": thread_id,
        "client_address": address,
        "channel": "twilio",
        "our_identity": identity,
        "created_at": now,
        "updated_at": now,
    }
    inbound = ConversationRecord(
        message_id="m-quoting",
        origin="client",
        inbound_text="yes",
        inbound_reply_to=quote,
        answer_status="answered",
        answer="noted",
        delivery_status=DeliveryStatus.DELIVERED,
        **shared,
    )
    asked = ConversationRecord(
        message_id="s-asked",
        origin="send",
        sent_by="ask",
        inbound_text="",
        answer_status="answered",
        answer="proceed?",
        delivery_status=DeliveryStatus.DELIVERED,
        ask=AskSend(interaction_id="i-quoted", answer_format="text", sensitive=False),
        ask_reply=AskReply(answer="yes", reply_to=quote, answered_at=datetime.now(UTC)),
        **shared,
    )
    return [inbound, asked]


def holds_text(fake: Any, text: str) -> bool:
    """Whether any key of the fake record Redis holds ``text``."""
    stores = [fake._hashes, fake._strings, fake._zsets, getattr(fake, "_lists", {}), getattr(fake, "_sets", {})]
    return text in json.dumps(stores, default=str)
