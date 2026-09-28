"""The verified event flow: challenge echo, dedupe, correlation matching, the
answer forward, and the door-status policy. Every request here is signed with
the test secret — the flow always crosses real verification first."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from tai42_contract.channels import AnswerForwardError, InboundAnswerOutcome
from tai42_contract.conversations import InboundMediaKind, InboundRejectionReason
from tai42_contract.interactions import (
    MediaSourceReadError,
    MediaStoreUnavailableError,
    MediaTooLargeError,
    MediaTypeNotAllowedError,
)
from tai42_contract.interactions.models import MediaKind
from tai42_kit.net import MediaFetchError
from tai42_kit.settings import reset_all_settings

from tai42_channel_slack.inbound.events import _media_kind, slack_inbound

from .conftest import (
    TEST_ALLOWED_RECIPIENT,
    TEST_BOT_TOKEN,
    TEST_BOT_USER_ID,
    TEST_DEFAULT_RECIPIENT,
    TEST_SIGNING_SECRET,
    FakeStream,
    body_json,
    fake_open_media_stream,
    make_ingested,
    make_request,
    signed_headers,
)

pytestmark = pytest.mark.usefixtures("slack_env")

_CALLBACK = "http://gateway/api/interactions/callback/ticket-7"
_THREAD_TS = "1712345678.000100"
_CORR_KEY = f"channel:slack:corr:{_THREAD_TS}"
_DEDUPE_KEY = "channel:slack:event:Ev001"


def _signed(body: bytes):
    return make_request(body, signed_headers(body, TEST_SIGNING_SECRET))


def _event_body(event: dict[str, Any] | None = None, event_id: str | None = "Ev001", **envelope: Any) -> bytes:
    payload: dict[str, Any] = {"type": "event_callback", **envelope}
    if event_id is not None:
        payload["event_id"] = event_id
    if event is not None:
        payload["event"] = event
    return json.dumps(payload).encode()


def _reply_event(**overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "message",
        "channel": TEST_DEFAULT_RECIPIENT,
        "thread_ts": _THREAD_TS,
        "text": "yes, deploy it",
        "user": "U012345",
        "ts": "1712345679.000200",
    }
    event.update(overrides)
    return event


def _seed_correlation(fake_redis) -> None:
    # The corr record is now a JSON {callback_url, interaction_id, timeout_at}.
    fake_redis.store[_CORR_KEY] = json.dumps(
        {"callback_url": _CALLBACK, "interaction_id": "int-7", "timeout_at": "2999-01-01T00:00:00+00:00"}
    )
    fake_redis.ttls[_CORR_KEY] = 300


async def test_url_verification_echoes_challenge():
    body = json.dumps({"type": "url_verification", "challenge": "ch4ll3ng3"}).encode()

    response = await slack_inbound(_signed(body))

    assert response.status_code == 200
    assert body_json(response) == {"challenge": "ch4ll3ng3"}


async def test_unsigned_url_verification_is_401():
    # Verification precedes the challenge echo — an unverified echo would let
    # anyone confirm the endpoint.
    body = json.dumps({"type": "url_verification", "challenge": "ch4ll3ng3"}).encode()

    response = await slack_inbound(make_request(body, {}))

    assert response.status_code == 401


async def test_url_verification_without_challenge_is_400():
    body = json.dumps({"type": "url_verification"}).encode()

    response = await slack_inbound(_signed(body))

    assert response.status_code == 400


@pytest.mark.parametrize("body", [b"{not json", b"[1, 2]", b'"a string"'])
async def test_non_object_body_is_400(body):
    response = await slack_inbound(_signed(body))

    assert response.status_code == 400
    assert body_json(response) == {"error": "body must be a JSON object"}


async def test_event_callback_without_event_id_is_400():
    response = await slack_inbound(_signed(_event_body(event=_reply_event(), event_id=None)))

    assert response.status_code == 400
    assert body_json(response) == {"error": "event_callback without event_id"}


async def test_non_event_callback_envelope_is_ignored():
    body = json.dumps({"type": "app_rate_limited"}).encode()

    response = await slack_inbound(_signed(body))

    assert response.status_code == 200
    assert body_json(response) == {"status": "ignored"}


async def test_happy_path_forwards_answer_and_drops_correlation(fake_redis, channels):
    _seed_correlation(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED

    response = await slack_inbound(_signed(_event_body(event=_reply_event())))

    assert response.status_code == 200
    assert body_json(response) == {"status": "forwarded"}
    (call,) = channels.inbound_calls
    assert call.correlation_key == _THREAD_TS
    assert call.answer == "yes, deploy it"
    assert call.bridge.owns_retry_notice is False  # a threaded reply is re-answerable in place
    assert _CORR_KEY not in fake_redis.store  # released by the ladder (mirrored)
    assert _DEDUPE_KEY in fake_redis.store  # claim kept: retries ack as duplicates


async def test_reply_from_allowlisted_conversation_forwards(fake_redis, channels):
    # A question can be delivered to any allowlisted recipient, so a
    # correlated reply from that conversation — not only the default one — is
    # a real answer that reaches the ladder.
    _seed_correlation(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED

    event = _reply_event(channel=TEST_ALLOWED_RECIPIENT)
    response = await slack_inbound(_signed(_event_body(event=event)))

    assert body_json(response) == {"status": "forwarded"}
    (call,) = channels.inbound_calls
    assert call.answer == "yes, deploy it"


async def test_bridge_only_deployment_needs_no_ask_recipients(fake_redis, stub_conversations, monkeypatch):
    # No default recipient and an empty allowlist: ask correlation can never
    # match, but a bridge-only deployment must still forward messages to the bridge.
    monkeypatch.delenv("CHANNEL_SLACK_DEFAULT_RECIPIENT")
    monkeypatch.delenv("CHANNEL_SLACK_ALLOWED_RECIPIENTS")
    reset_all_settings()

    response = await slack_inbound(_signed(_event_body(event=_reply_event())))

    assert body_json(response) == {"status": "accepted"}
    (call,) = stub_conversations.accept_calls
    assert call.client_address == TEST_DEFAULT_RECIPIENT
    assert _DEDUPE_KEY in fake_redis.store  # processed: retries ack as duplicate


async def test_duplicate_event_id_acks_without_second_forward(fake_redis, channels):
    _seed_correlation(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED

    first = await slack_inbound(_signed(_event_body(event=_reply_event())))
    second = await slack_inbound(_signed(_event_body(event=_reply_event())))

    assert body_json(first) == {"status": "forwarded"}
    assert body_json(second) == {"status": "duplicate"}
    assert len(channels.inbound_calls) == 1  # the ladder was consulted exactly once


async def test_ladder_bridged_outcome_acks_and_drops_correlation(fake_redis, channels):
    # The ladder's BRIDGED outcome (the ask is gone — it released the correlation and
    # already bridged the reply internally, under the event id dedupe key) acks 200. The
    # channel supplies the faithful bridge text and the event id.
    _seed_correlation(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.BRIDGED

    response = await slack_inbound(_signed(_event_body(event=_reply_event())))

    assert body_json(response) == {"status": "bridged"}
    assert _CORR_KEY not in fake_redis.store  # released by the ladder (mirrored)
    assert _DEDUPE_KEY in fake_redis.store  # Slack's retry acks as duplicate
    (call,) = channels.inbound_calls
    assert call.bridge.bridge_text == "yes, deploy it"
    assert call.bridge.client_address == TEST_DEFAULT_RECIPIENT
    assert call.bridge.provider_message_id == "Ev001"


async def test_bridged_then_redelivery_acks_duplicate_no_second_ladder_call(fake_redis, channels):
    # After a BRIDGED outcome, Slack redelivers the same event_id: the dedupe claim
    # short-circuits — the retry acks as a duplicate and never re-runs the ladder.
    _seed_correlation(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.BRIDGED

    first = await slack_inbound(_signed(_event_body(event=_reply_event())))
    assert body_json(first) == {"status": "bridged"}

    second = await slack_inbound(_signed(_event_body(event=_reply_event())))
    assert body_json(second) == {"status": "duplicate"}
    assert len(channels.inbound_calls) == 1  # still once


async def test_ladder_forward_error_does_not_bridge_and_keeps_correlation(fake_redis, channels, stub_conversations):
    # A raised AnswerForwardError (5xx/transport) must NOT be converted into a bridge:
    # the ladder kept the correlation and the channel re-raises for Slack's retry.
    _seed_correlation(fake_redis)
    channels.inbound_error = AnswerForwardError("callback forward failed: HTTP 500 from the interactions door")

    with pytest.raises(AnswerForwardError, match="HTTP 500"):
        await slack_inbound(_signed(_event_body(event=_reply_event())))

    assert stub_conversations.accept_calls == []  # never bridged
    assert _CORR_KEY in fake_redis.store  # correlation kept


async def test_door_retry_kept_keeps_correlation_and_acks_rejected(fake_redis, channels):
    _seed_correlation(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.RETRY_KEPT

    response = await slack_inbound(_signed(_event_body(event=_reply_event())))

    assert body_json(response) == {"status": "rejected"}
    assert _CORR_KEY in fake_redis.store  # the human can reply again


async def test_door_bridged_kept_keeps_correlation_and_acks_bridged(fake_redis, channels):
    # The ladder's BRIDGED_KEPT outcome (a bridge-policy ask rejected the reply: the
    # correlation is KEPT and the reply was bridged as a digression turn) acks 200 with
    # the same "bridged" wire a released BRIDGE returns. Without the ack-map entry this
    # reachable outcome raised KeyError -> 500 -> Slack redelivery loop.
    _seed_correlation(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.BRIDGED_KEPT

    response = await slack_inbound(_signed(_event_body(event=_reply_event())))

    assert response.status_code == 200
    assert body_json(response) == {"status": "bridged"}
    assert _CORR_KEY in fake_redis.store  # the ask stays parked


async def test_ladder_forward_error_releases_claim_then_retry_recovers(fake_redis, channels):
    _seed_correlation(fake_redis)
    channels.inbound_error = AnswerForwardError("callback forward failed: HTTP 500 from the interactions door")

    with pytest.raises(AnswerForwardError, match="HTTP 500"):
        await slack_inbound(_signed(_event_body(event=_reply_event())))

    assert _DEDUPE_KEY not in fake_redis.store  # claim released for the retry
    assert _CORR_KEY in fake_redis.store

    # Slack's retry ladder redelivers the same event; the door recovered.
    channels.inbound_error = None
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED
    response = await slack_inbound(_signed(_event_body(event=_reply_event())))
    assert body_json(response) == {"status": "forwarded"}


async def test_transport_error_wrapped_by_ladder_releases_claim(fake_redis, channels):
    _seed_correlation(fake_redis)
    channels.inbound_error = AnswerForwardError("forwarding the answer to the door failed: connect error")

    with pytest.raises(AnswerForwardError, match="forwarding the answer"):
        await slack_inbound(_signed(_event_body(event=_reply_event())))

    assert _DEDUPE_KEY not in fake_redis.store
    assert _CORR_KEY in fake_redis.store


async def test_correlated_reply_without_text_raises_never_none(fake_redis, channels):
    _seed_correlation(fake_redis)
    event = _reply_event()
    del event["text"]

    with pytest.raises(ValueError, match="carries no text"):
        await slack_inbound(_signed(_event_body(event=event)))

    assert channels.inbound_calls == []  # never reached the ladder
    assert _DEDUPE_KEY not in fake_redis.store  # released so the retry reprocesses


async def test_event_callback_without_event_object_raises(fake_redis):
    with pytest.raises(ValueError, match="without an event object"):
        await slack_inbound(_signed(_event_body()))

    assert _DEDUPE_KEY not in fake_redis.store


@pytest.mark.parametrize(
    "event",
    [
        pytest.param(_reply_event(subtype="message_changed"), id="subtype-present"),
        pytest.param(_reply_event(subtype="channel_join"), id="join-subtype"),
        pytest.param(_reply_event(type="reaction_added"), id="not-a-message"),
    ],
)
async def test_non_message_traffic_is_acked_ignored_never_bridged(fake_redis, http_script, stub_conversations, event):
    # An edit or a join carries a subtype whose payload is not a participant utterance;
    # a non-message event is not text at all. Neither forwards nor bridges.
    _seed_correlation(fake_redis)

    response = await slack_inbound(_signed(_event_body(event=event)))

    assert response.status_code == 200
    assert body_json(response) == {"status": "ignored"}
    assert http_script.requests == []
    assert stub_conversations.accept_calls == []


async def test_me_message_bridges_like_a_plain_message(fake_redis, stub_conversations):
    # A `/me …` post arrives with subtype me_message and its top-level text is the
    # participant's own words: it bridges as a plain (non-threaded) message.
    event = {
        "type": "message",
        "subtype": "me_message",
        "channel": TEST_DEFAULT_RECIPIENT,
        "text": "is on it",
        "user": "U012345",
        "ts": "1712345679.000200",
    }

    response = await slack_inbound(_signed(_event_body(event=event)))

    assert body_json(response) == {"status": "accepted"}
    (call,) = stub_conversations.accept_calls
    assert call.text == "is on it"
    assert call.client_address == TEST_DEFAULT_RECIPIENT
    assert call.provider_message_id == "Ev001"
    assert call.params is None


async def test_thread_broadcast_in_allowlisted_channel_resolves_answer(fake_redis, channels):
    # A thread reply also sent to the channel (subtype thread_broadcast) carries thread_ts:
    # inside an allowlisted recipient with a pending question it resolves the answer.
    _seed_correlation(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED

    event = _reply_event(subtype="thread_broadcast", channel=TEST_ALLOWED_RECIPIENT)
    response = await slack_inbound(_signed(_event_body(event=event)))

    assert body_json(response) == {"status": "forwarded"}
    (call,) = channels.inbound_calls
    assert call.correlation_key == _THREAD_TS
    assert call.answer == "yes, deploy it"


async def test_thread_broadcast_without_pending_question_bridges(fake_redis, stub_conversations):
    # A thread_broadcast whose thread has no pending question (never seeded) bridges like
    # any uncorrelated message rather than being dropped.
    event = _reply_event(subtype="thread_broadcast")
    response = await slack_inbound(_signed(_event_body(event=event)))

    assert body_json(response) == {"status": "accepted"}
    (call,) = stub_conversations.accept_calls
    assert call.text == "yes, deploy it"
    assert call.client_address == TEST_DEFAULT_RECIPIENT


@pytest.mark.parametrize(
    ("mimetype", "kind"),
    [
        ("image/png", InboundMediaKind.IMAGE),
        ("video/mp4", InboundMediaKind.VIDEO),
        ("audio/mpeg", InboundMediaKind.AUDIO),
        ("application/pdf", InboundMediaKind.DOCUMENT),
        ("text/plain", InboundMediaKind.DOCUMENT),
        ("model/gltf-binary", InboundMediaKind.FILE),
        (None, InboundMediaKind.FILE),
        ("", InboundMediaKind.FILE),
    ],
)
def test_media_kind_maps_by_major_type(mimetype, kind):
    assert _media_kind(mimetype) is kind


_OPEN_ATTR = "tai42_channel_slack.inbound.events.open_media_stream"


def _file_share_event(files: list[Any], *, text: str | None = None, **overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "message",
        "subtype": "file_share",
        "channel": TEST_DEFAULT_RECIPIENT,
        "user": "U012345",
        "ts": "1712345679.000200",
        "files": files,
    }
    if text is not None:
        event["text"] = text
    event.update(overrides)
    return event


async def test_file_share_single_file_ingests_served_attachment_and_parity_params(
    fake_redis, stub_conversations, stub_media, monkeypatch
):
    # One image file: its bytes are fetched (bot token on the call only, redirects off) and
    # ingested; the bridged turn carries the typed served attachment AND the parity media_*
    # params off that ONE ingest, with the SERVED id and the seam's computed sha256.
    calls: list[Any] = []
    monkeypatch.setattr(_OPEN_ATTR, fake_open_media_stream(FakeStream(content_type="image/png"), calls=calls))
    served = make_ingested(kind=MediaKind.IMAGE, mime="image/png", size=2048, sha256="e" * 64)
    stub_media.ingest_default = served
    event = _file_share_event(
        [
            {
                "id": "F1",
                "url_private": "https://files.slack.com/f1",
                "url_private_download": "https://files.slack.com/f1?dl=1",
                "name": "chart.png",
                "mimetype": "image/png",
                "size": 2048,
            }
        ],
        text="look at this",
    )

    response = await slack_inbound(_signed(_event_body(event=event)))

    assert body_json(response) == {"status": "accepted"}
    (call,) = stub_conversations.accept_calls
    assert call.text == "look at this"  # the caption rides the file's turn
    assert call.provider_message_id == "Ev001-0"
    assert call.params == {
        "media_kind": "image",
        "media_id": served.media_id,  # the served id, never the raw url_private
        "media_mime_type": "image/png",
        "media_sha256": "e" * 64,
        "media_size": "2048",
    }
    # An image carries no filename param — MediaItem.filename is document-only.
    assert "media_filename" not in call.params
    (item,) = call.attachments
    assert item is served.item
    assert item.url == served.item.url
    assert stub_conversations.rejected_calls == []
    # The download-disposition url is preferred; the bot token rides only the call.
    (fetched,) = calls
    assert fetched.url == "https://files.slack.com/f1?dl=1"
    assert fetched.headers == {"Authorization": f"Bearer {TEST_BOT_TOKEN}"}
    assert fetched.follow_redirects is False
    # The stream reached ingest; the origin's message id is the per-file bridge id.
    (ingest,) = stub_media.ingest_calls
    assert ingest.origin.channel_id == "slack"
    assert ingest.origin.participant_identity == "U012345"
    assert ingest.origin.message_id == "Ev001-0"
    assert ingest.integrity_sha256 is None


async def test_file_share_two_files_bridge_two_served_turns_with_per_item_ids(
    fake_redis, stub_conversations, stub_media, monkeypatch
):
    monkeypatch.setattr(_OPEN_ATTR, fake_open_media_stream())
    doc = make_ingested(kind=MediaKind.DOCUMENT, mime="application/pdf", filename="a.pdf", size=10)
    video = make_ingested(kind=MediaKind.VIDEO, mime="video/mp4", size=20)
    stub_media.ingest_results = [doc, video]
    event = _file_share_event(
        [
            {"id": "F1", "url_private": "https://files.slack.com/f1", "name": "a.pdf", "mimetype": "application/pdf"},
            {"id": "F2", "url_private": "https://files.slack.com/f2", "name": "b.mp4", "mimetype": "video/mp4"},
        ],
        text="two files",
    )

    response = await slack_inbound(_signed(_event_body(event=event)))

    assert body_json(response) == {"status": "accepted"}
    first, second = stub_conversations.accept_calls
    assert first.provider_message_id == "Ev001-0"
    assert first.text == "two files"  # the caption rides the FIRST file only
    assert first.params["media_kind"] == "document"
    assert first.params["media_id"] == doc.media_id
    assert first.attachments[0].url == doc.item.url
    assert second.provider_message_id == "Ev001-1"
    assert second.text == "[video]"  # every later file gets the placeholder
    assert second.params["media_kind"] == "video"
    assert second.attachments[0].url == video.item.url


async def test_file_share_blank_text_uses_placeholder(fake_redis, stub_conversations, stub_media, monkeypatch):
    monkeypatch.setattr(_OPEN_ATTR, fake_open_media_stream(FakeStream(content_type="application/pdf")))
    stub_media.ingest_default = make_ingested(kind=MediaKind.DOCUMENT, mime="application/pdf", filename="notes.pdf")
    event = _file_share_event(
        [{"id": "F1", "url_private": "https://files.slack.com/f1", "name": "notes.pdf", "mimetype": "application/pdf"}],
        text="   ",
    )

    response = await slack_inbound(_signed(_event_body(event=event)))

    assert body_json(response) == {"status": "accepted"}
    (call,) = stub_conversations.accept_calls
    assert call.text == "[document: notes.pdf]"  # whitespace-only caption is not a caption


async def test_file_share_unfetchable_file_notifies_rejected_and_acks(fake_redis, stub_conversations):
    # A file with neither a fetchable url_private nor a name cannot be represented as a
    # turn: with no caption to bridge, the shared rejection reply + event fires and the
    # event is acked, no turn.
    event = _file_share_event([{"id": "F1", "filetype": "binary"}])

    response = await slack_inbound(_signed(_event_body(event=event)))

    assert response.status_code == 200
    assert body_json(response) == {"status": "ignored"}
    assert stub_conversations.accept_calls == []
    (rejected,) = stub_conversations.rejected_calls
    assert rejected.channel_id == "slack"
    assert rejected.recipient == TEST_DEFAULT_RECIPIENT
    assert rejected.sender_identity == TEST_BOT_USER_ID
    assert rejected.kind == "file"
    assert rejected.reason is InboundRejectionReason.UNSUPPORTED_TYPE


async def test_file_share_named_file_without_body_notifies_could_not_receive(fake_redis, stub_conversations):
    # A file that names itself but exposes no fetchable url_private has a body we cannot
    # receive: a per-file COULD_NOT_RECEIVE notice, no fetch attempted.
    event = _file_share_event([{"id": "F1", "name": "report.pdf", "mimetype": "application/pdf"}])

    response = await slack_inbound(_signed(_event_body(event=event)))

    assert body_json(response) == {"status": "ignored"}
    assert stub_conversations.accept_calls == []
    (rejected,) = stub_conversations.rejected_calls
    assert rejected.kind == "document"
    assert rejected.reason is InboundRejectionReason.COULD_NOT_RECEIVE


async def test_file_share_all_unfetchable_with_caption_bridges_caption(fake_redis, stub_conversations):
    # Every file is unfetchable (each gets its own rejection reply), but the message
    # carried a caption: the caption bridges as a text turn under the message's own event_id,
    # carrying the first file's kind/mime parity params (no served reference) so it matches the
    # rejected-media caption the other channels bridge — participant text is never silently dropped.
    event = _file_share_event([{"id": "F1", "filetype": "binary"}], text="just a note")

    response = await slack_inbound(_signed(_event_body(event=event)))

    assert response.status_code == 200
    assert body_json(response) == {"status": "accepted"}
    (rejected,) = stub_conversations.rejected_calls
    assert rejected.reason is InboundRejectionReason.UNSUPPORTED_TYPE
    (call,) = stub_conversations.accept_calls
    assert call.text == "just a note"
    assert call.provider_message_id == "Ev001"
    # The lone file carries no mimetype -> kind FILE, mime omitted.
    assert call.params == {"media_kind": "file"}
    assert call.attachments is None


async def test_file_share_all_rejected_caption_carries_first_file_kind_and_mime(fake_redis, stub_conversations):
    # Two files, both named but unfetchable (each a COULD_NOT_RECEIVE notice); the caption turn
    # carries the FIRST file's kind + declared mime as parity params, matching the other channels.
    event = _file_share_event(
        [
            {"id": "F0", "name": "a.pdf", "mimetype": "application/pdf"},
            {"id": "F1", "name": "b.png", "mimetype": "image/png"},
        ],
        text="see attached",
    )

    response = await slack_inbound(_signed(_event_body(event=event)))

    assert body_json(response) == {"status": "accepted"}
    assert len(stub_conversations.rejected_calls) == 2  # one notice per rejected file
    (call,) = stub_conversations.accept_calls
    assert call.text == "see attached"
    assert call.provider_message_id == "Ev001"
    assert call.params == {"media_kind": "document", "media_mime_type": "application/pdf"}
    assert call.attachments is None


async def test_file_share_caption_rides_the_first_bridged_file_when_the_first_is_rejected(
    fake_redis, stub_conversations, stub_media, monkeypatch
):
    # files=[rejected, good]: file 0's body is gone (a permanent 404 -> one notice), file 1
    # bridges. The message caption is offered to the FIRST BRIDGED file, so it rides file 1's
    # served turn (not the placeholder); exactly one rejection notice, participant text kept.
    results: list[Any] = [
        MediaFetchError(host="files.slack.com", status_code=404),
        FakeStream(content_type="image/png"),
    ]

    @asynccontextmanager
    async def _open(
        url: str, *, headers: Any = None, auth: Any = None, follow_redirects: bool = False
    ) -> AsyncIterator[Any]:
        item = results.pop(0)
        if isinstance(item, BaseException):
            raise item
        yield item

    monkeypatch.setattr(_OPEN_ATTR, _open)
    served = make_ingested(kind=MediaKind.IMAGE, mime="image/png", sha256="e" * 64)
    stub_media.ingest_default = served
    event = _file_share_event(
        [
            {"id": "F0", "url_private": "https://files.slack.com/f0", "name": "gone.png", "mimetype": "image/png"},
            {"id": "F1", "url_private": "https://files.slack.com/f1", "name": "chart.png", "mimetype": "image/png"},
        ],
        text="look at this",
    )

    response = await slack_inbound(_signed(_event_body(event=event)))

    assert body_json(response) == {"status": "accepted"}
    (rejected,) = stub_conversations.rejected_calls  # exactly one notice (file 0)
    assert rejected.reason is InboundRejectionReason.COULD_NOT_RECEIVE
    (call,) = stub_conversations.accept_calls  # file 1 bridged, the caption on its turn
    assert call.text == "look at this"
    assert call.provider_message_id == "Ev001-1"
    assert call.attachments[0].url == served.item.url
    assert call.params["media_id"] == served.media_id


async def test_file_share_permanent_fetch_fault_notifies_could_not_receive(
    fake_redis, stub_conversations, stub_media, monkeypatch
):
    # A 404 at open (the body is gone) is permanent: a COULD_NOT_RECEIVE notice + ack, no
    # ingest, no turn.
    monkeypatch.setattr(
        _OPEN_ATTR, fake_open_media_stream(error=MediaFetchError(host="files.slack.com", status_code=404))
    )
    event = _file_share_event(
        [{"id": "F1", "url_private": "https://files.slack.com/f1", "name": "gone.png", "mimetype": "image/png"}]
    )

    response = await slack_inbound(_signed(_event_body(event=event)))

    assert body_json(response) == {"status": "ignored"}
    assert stub_media.ingest_calls == []
    assert stub_conversations.accept_calls == []
    (rejected,) = stub_conversations.rejected_calls
    assert rejected.kind == "image"
    assert rejected.reason is InboundRejectionReason.COULD_NOT_RECEIVE


async def test_file_share_over_cap_file_notifies_too_large(fake_redis, stub_conversations, stub_media, monkeypatch):
    monkeypatch.setattr(_OPEN_ATTR, fake_open_media_stream())
    stub_media.ingest_results = [MediaTooLargeError("media exceeds the cap")]
    event = _file_share_event(
        [{"id": "F1", "url_private": "https://files.slack.com/f1", "name": "big.png", "mimetype": "image/png"}]
    )

    response = await slack_inbound(_signed(_event_body(event=event)))

    assert body_json(response) == {"status": "ignored"}
    assert stub_conversations.accept_calls == []
    (rejected,) = stub_conversations.rejected_calls
    assert rejected.reason is InboundRejectionReason.TOO_LARGE


async def test_file_share_disallowed_type_notifies_unsupported_type(
    fake_redis, stub_conversations, stub_media, monkeypatch
):
    monkeypatch.setattr(_OPEN_ATTR, fake_open_media_stream())
    stub_media.ingest_results = [MediaTypeNotAllowedError("active content is never served inline")]
    event = _file_share_event(
        [{"id": "F1", "url_private": "https://files.slack.com/f1", "name": "x.svg", "mimetype": "image/svg+xml"}]
    )

    response = await slack_inbound(_signed(_event_body(event=event)))

    assert body_json(response) == {"status": "ignored"}
    (rejected,) = stub_conversations.rejected_calls
    assert rejected.reason is InboundRejectionReason.UNSUPPORTED_TYPE


async def test_file_share_store_unavailable_notifies_could_not_receive(
    fake_redis, stub_conversations, stub_media, monkeypatch
):
    monkeypatch.setattr(_OPEN_ATTR, fake_open_media_stream())
    stub_media.ingest_results = [MediaStoreUnavailableError("no blob provider is registered")]
    event = _file_share_event(
        [{"id": "F1", "url_private": "https://files.slack.com/f1", "name": "a.png", "mimetype": "image/png"}]
    )

    response = await slack_inbound(_signed(_event_body(event=event)))

    assert body_json(response) == {"status": "ignored"}
    (rejected,) = stub_conversations.rejected_calls
    assert rejected.reason is InboundRejectionReason.COULD_NOT_RECEIVE


async def test_file_share_transient_fetch_fault_raises_then_redelivery_recovers(
    fake_redis, stub_conversations, stub_media, monkeypatch
):
    # A 5xx at open is transient: the handler re-raises so the door frees the event claim
    # and Slack redelivers. No notice, no turn. The redelivery ingests cleanly.
    monkeypatch.setattr(
        _OPEN_ATTR, fake_open_media_stream(error=MediaFetchError(host="files.slack.com", status_code=503))
    )
    event = _file_share_event(
        [{"id": "F1", "url_private": "https://files.slack.com/f1", "name": "a.png", "mimetype": "image/png"}]
    )

    with pytest.raises(MediaFetchError):
        await slack_inbound(_signed(_event_body(event=event)))
    assert _DEDUPE_KEY not in fake_redis.store  # event claim freed for the retry
    assert stub_conversations.accept_calls == []
    assert stub_conversations.rejected_calls == []

    monkeypatch.setattr(_OPEN_ATTR, fake_open_media_stream())
    served = make_ingested(kind=MediaKind.IMAGE, mime="image/png")
    stub_media.ingest_default = served
    response = await slack_inbound(_signed(_event_body(event=event)))

    assert body_json(response) == {"status": "accepted"}
    (call,) = stub_conversations.accept_calls
    assert call.attachments[0].url == served.item.url


async def test_file_share_torn_body_read_raises_for_redelivery(fake_redis, stub_conversations, stub_media, monkeypatch):
    # A body-read fault the seam surfaces as MediaSourceReadError is transient: re-raise so
    # the door frees the claim and Slack redelivers.
    monkeypatch.setattr(_OPEN_ATTR, fake_open_media_stream())
    stub_media.ingest_results = [MediaSourceReadError("media source read failed")]
    event = _file_share_event(
        [{"id": "F1", "url_private": "https://files.slack.com/f1", "name": "a.png", "mimetype": "image/png"}]
    )

    with pytest.raises(MediaSourceReadError):
        await slack_inbound(_signed(_event_body(event=event)))
    assert _DEDUPE_KEY not in fake_redis.store
    assert stub_conversations.rejected_calls == []


async def test_slack_parity_media_filename_equals_sanitised(fake_redis, stub_conversations, stub_media, monkeypatch):
    # The parity media_filename is the seam's SANITISED name, never the raw vendor one; the
    # raw name (with a control char and a U+202E bidi override) appears in no param value.
    raw = "re‮port\x07.pdf"
    sanitised = "report.pdf"
    monkeypatch.setattr(_OPEN_ATTR, fake_open_media_stream(FakeStream(content_type="application/pdf")))
    stub_media.ingest_default = make_ingested(kind=MediaKind.DOCUMENT, mime="application/pdf", filename=sanitised)
    event = _file_share_event(
        [{"id": "F1", "url_private": "https://files.slack.com/f1", "name": raw, "mimetype": "application/pdf"}],
        text="here",
    )

    response = await slack_inbound(_signed(_event_body(event=event)))

    assert body_json(response) == {"status": "accepted"}
    (call,) = stub_conversations.accept_calls
    assert call.params["media_filename"] == sanitised
    assert all(raw not in value for value in call.params.values())
    # The raw name is only ever the sanitisation INPUT the seam receives.
    assert stub_media.ingest_calls[0].filename == raw


async def test_slack_placeholder_label_uses_sanitised_filename(fake_redis, stub_conversations, stub_media, monkeypatch):
    # With no caption, the placeholder label reads the SANITISED filename; the raw name
    # appears nowhere in the turn text or params.
    raw = "re‮port\x07.pdf"
    sanitised = "report.pdf"
    monkeypatch.setattr(_OPEN_ATTR, fake_open_media_stream(FakeStream(content_type="application/pdf")))
    stub_media.ingest_default = make_ingested(kind=MediaKind.DOCUMENT, mime="application/pdf", filename=sanitised)
    event = _file_share_event(
        [{"id": "F1", "url_private": "https://files.slack.com/f1", "name": raw, "mimetype": "application/pdf"}]
    )

    await slack_inbound(_signed(_event_body(event=event)))

    (call,) = stub_conversations.accept_calls
    assert call.text == f"[document: {sanitised}]"
    assert raw not in call.text
    assert all(raw not in value for value in call.params.values())


async def test_file_share_rejection_not_repeated_when_a_later_file_faults_and_slack_retries(
    fake_redis, stub_conversations, stub_media, monkeypatch
):
    # files=[unfetchable, good image]: file 0 is rejected, then file 1's accept faults
    # once, so the door frees the message dedupe and re-raises (Slack retries). On the
    # retry the good file bridges cleanly and file 0's rejection reply — already
    # delivered — is deduped on its own f"{event_id}-{index}" key, not sent again.
    monkeypatch.setattr(_OPEN_ATTR, fake_open_media_stream())
    stub_media.ingest_default = make_ingested(kind=MediaKind.IMAGE, mime="image/png")
    event = _file_share_event(
        [
            {"id": "F0", "filetype": "binary"},
            {"id": "F1", "url_private": "https://files.slack.com/f1", "name": "chart.png", "mimetype": "image/png"},
        ]
    )

    stub_conversations.accept_error = RuntimeError("transient bus fault")
    with pytest.raises(RuntimeError, match="transient bus fault"):
        await slack_inbound(_signed(_event_body(event=event)))
    assert len(stub_conversations.rejected_calls) == 1  # file 0 rejected once
    assert _DEDUPE_KEY not in fake_redis.store  # message claim freed for the retry

    stub_conversations.accept_error = None
    response = await slack_inbound(_signed(_event_body(event=event)))

    assert body_json(response) == {"status": "accepted"}
    assert len(stub_conversations.rejected_calls) == 1  # still exactly one across both passes
    # The good file is (idempotently) bridged on both passes; the rejection is not repeated.
    assert [c.provider_message_id for c in stub_conversations.accept_calls] == ["Ev001-1", "Ev001-1"]
