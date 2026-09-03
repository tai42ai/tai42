"""Pure delivery-executor helpers: long-answer splitting, the signed-callback digest, the
retry backoff, canonical address forms, and the rich-part notification/capability seams."""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import cast

import pytest
from tai42_contract.channels import Channel
from tai42_contract.conversations import AnswerPart

from tai42_skeleton.conversations.address import canonical_address
from tai42_skeleton.conversations.delivery import (
    _backoff_seconds,
    _part_notification,
    _sign,
    _unsupported_rich_capability,
    split_message,
)
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.settings import ConversationsSettings


def test_split_short_message_is_one_chunk():
    assert split_message("hello", 100) == ["hello"]


def test_split_preserves_and_reorders_nothing():
    text = "word " * 100  # 500 chars
    chunks = split_message(text, 40)
    assert all(len(c) <= 40 for c in chunks)
    assert "".join(chunks) == text
    assert len(chunks) > 1


def test_split_hard_cuts_an_unbreakable_run():
    text = "x" * 90
    chunks = split_message(text, 40)
    assert chunks == ["x" * 40, "x" * 40, "x" * 10]
    assert "".join(chunks) == text


def test_split_rejects_nonpositive_limit():
    with pytest.raises(ValueError, match="must be positive"):
        split_message("hi", 0)


def test_sign_is_hmac_sha256_hex_with_prefix():
    body = b'{"answer":"hi"}'
    signature = _sign("s3cr3t", body)
    assert signature.startswith("sha256=")
    expected = hmac.new(b"s3cr3t", body, hashlib.sha256).hexdigest()
    assert signature == f"sha256={expected}"


def test_backoff_is_exponential_and_capped(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_BACKOFF_BASE_SECONDS", "8")
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_BACKOFF_MAX_SECONDS", "900")
    settings = ConversationsSettings()
    assert _backoff_seconds(settings, 1) == 8
    assert _backoff_seconds(settings, 2) == 16
    assert _backoff_seconds(settings, 3) == 32
    # Capped.
    assert _backoff_seconds(settings, 20) == 900


def test_canonical_address_trims_and_rejects_blank():
    assert canonical_address("  +1555  ") == "+1555"
    with pytest.raises(ValueError, match="non-blank"):
        canonical_address("   ")


# -- rich-part seams: the schema field on a delivered part -----------------------


_FORM_SCHEMA = {"type": "object", "properties": {"name": {"type": "string"}}}


def _channel_record() -> ConversationRecord:
    now = time.time()
    return ConversationRecord(
        message_id="m1",
        route_name="line",
        door="channel",
        thread_id="bridge:line:+15550002222",
        client_address="+15550002222",
        channel="twilio",
        our_identity="+15550001111",
        origin="client",
        inbound_text="hi",
        delivery_status=DeliveryStatus.PENDING_DELIVERY,
        answer_status="answered",
        answer="fill this in",
        created_at=now,
        updated_at=now,
    )


def test_part_notification_schema_rides_the_final_chunk_only():
    # The part's rich fields land with its FINAL chunk (its completed message); an earlier
    # chunk of a multi-chunk part is plain text — the schema follows the same rule.
    part = AnswerPart(message="fill this in", schema=_FORM_SCHEMA)
    record = _channel_record()
    early = _part_notification(part, "fill th", record, final=False)
    assert early.schema is None
    last = _part_notification(part, "is in", record, final=True)
    assert last.schema == _FORM_SCHEMA


class _PlainChannel:
    async def notify(self, notification):
        return []


class _FormChannel(_PlainChannel):
    supports_form_notifications = True


def test_unsupported_rich_capability_names_form():
    # A part carrying a schema to a channel without supports_form_notifications is
    # unrenderable — the executor refuses the record, naming the form capability. The
    # fakes carry only ``notify`` (the one method the walk's channel touches), cast to
    # the protocol as the notify-door tests do.
    part = AnswerPart(message="fill this in", schema=_FORM_SCHEMA)
    assert _unsupported_rich_capability(cast("Channel", _PlainChannel()), [part]) == "form"
    assert _unsupported_rich_capability(cast("Channel", _FormChannel()), [part]) is None
    assert _unsupported_rich_capability(cast("Channel", _PlainChannel()), [AnswerPart(message="plain")]) is None
