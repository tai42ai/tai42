"""Always-bridge rich content — inbound media, location, contacts, and reactions,
each bridging as a fresh turn and never answering a pending ask."""

from __future__ import annotations

import json

import pytest
from tai42_contract.interactions.models import LocationElement

import tai42_channel_whatsapp.inbound  # noqa: F401  (route registration side-effect)

from .conftest import (
    _CONTACT_KEY,
    _PENDING_KEY,
    _SEEN_KEY,
    _WAMID,
    WA_ID,
    FakeHttpx,
    FakeRedis,
    _params_envelope,
    _seed_pending,
    signed_request,
)

pytestmark = pytest.mark.usefixtures("whatsapp_env")


async def test_inbound_image_with_caption_bridges_caption_as_text_and_media_params(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A participant photo with a caption: the caption is the turn text; the media's identity rides
    # params (media_kind/id/mime/sha256). No typed attachment (see the INBOUND MEDIA design
    # note) — the id is the re-fetch handle.
    message = {
        "id": _WAMID,
        "from": WA_ID,
        "type": "image",
        "image": {"id": "media-abc", "mime_type": "image/jpeg", "sha256": "deadbeef", "caption": "the broken part"},
    }
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["text"] == "the broken part"
    assert call["attachments"] is None  # design gap: no served-media ingestion seam
    assert call["params"] == {
        "media_kind": "image",
        "media_id": "media-abc",
        "media_mime_type": "image/jpeg",
        "media_sha256": "deadbeef",
    }
    assert _SEEN_KEY in fake_redis.store


async def test_inbound_image_without_caption_uses_placeholder_text(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    message = {"id": _WAMID, "from": WA_ID, "type": "image", "image": {"id": "m1", "mime_type": "image/png"}}
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["text"] == "[image]"  # accept refuses blank text — a faithful placeholder rides
    assert call["params"] == {"media_kind": "image", "media_id": "m1", "media_mime_type": "image/png"}


async def test_inbound_document_carries_filename_in_text_and_params(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    message = {
        "id": _WAMID,
        "from": WA_ID,
        "type": "document",
        "document": {"id": "doc-1", "mime_type": "application/pdf", "filename": "report.pdf"},
    }
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["text"] == "[document: report.pdf]"
    assert call["params"]["media_filename"] == "report.pdf"
    assert call["params"]["media_kind"] == "document"


async def test_inbound_voice_note_flags_voice_param_and_placeholder(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    message = {
        "id": _WAMID,
        "from": WA_ID,
        "type": "audio",
        "audio": {"id": "a1", "mime_type": "audio/ogg", "voice": True},
    }
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["text"] == "[voice message]"
    assert call["params"]["media_voice"] == "true"


async def test_inbound_animated_sticker_flags_animated_param(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    message = {
        "id": _WAMID,
        "from": WA_ID,
        "type": "sticker",
        "sticker": {"id": "s1", "mime_type": "image/webp", "animated": True},
    }
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["text"] == "[sticker]"
    assert call["params"]["sticker_animated"] == "true"
    assert call["params"]["media_kind"] == "sticker"


async def test_inbound_video_bridges_and_dedupes_on_replay(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    message = {"id": _WAMID, "from": WA_ID, "type": "video", "video": {"id": "v1", "mime_type": "video/mp4"}}
    await handler(signed_request(_params_envelope(message)))
    # A redelivery of the same wamid is short-circuited (already seen).
    await handler(signed_request(_params_envelope(message)))

    assert len(stub_app.conversations.accept_calls) == 1
    assert stub_app.conversations.accept_calls[0]["params"]["media_kind"] == "video"


async def test_inbound_media_caption_carries_reply_context_params(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # Message-level context (reply-to) merges with the media params on the bridged turn.
    message = {
        "id": _WAMID,
        "from": WA_ID,
        "type": "image",
        "image": {"id": "m1", "mime_type": "image/jpeg", "caption": "see this"},
        "context": {"id": "wamid.QUOTED"},
    }
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["text"] == "see this"
    assert call["params"]["context_message_id"] == "wamid.QUOTED"
    assert call["params"]["media_id"] == "m1"


async def test_inbound_media_while_ask_pending_bridges_leaving_ask_parked(
    handler, stub_app, channels, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A photo cannot answer a pending text/select/form ask — it bridges as a fresh turn and
    # the parked ask is left untouched (never reaches the answer ladder).
    await _seed_pending()
    message = {"id": _WAMID, "from": WA_ID, "type": "image", "image": {"id": "m1", "caption": "unrelated photo"}}
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    assert channels.inbound_calls == []  # never the answer ladder
    assert stub_app.conversations.accept_calls[0]["text"] == "unrelated photo"
    assert _PENDING_KEY in fake_redis.store  # the ask stays parked


async def test_inbound_location_lands_typed_location_on_accept(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A shared location lands as a typed LocationElement on accept (a machine-consumable
    # field, not a param); the turn text is the place name.
    message = {
        "id": _WAMID,
        "from": WA_ID,
        "type": "location",
        "location": {"latitude": 51.5, "longitude": -0.12, "name": "Office", "address": "1 High St"},
    }
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["text"] == "Office"
    assert call["location"] == LocationElement(latitude=51.5, longitude=-0.12, name="Office", address="1 High St")
    assert call["params"] is None


async def test_inbound_location_without_labels_uses_coordinate_text(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    message = {"id": _WAMID, "from": WA_ID, "type": "location", "location": {"latitude": 1.5, "longitude": 2.5}}
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["text"] == "location: 1.5, 2.5"
    assert call["location"] == LocationElement(latitude=1.5, longitude=2.5)


async def test_inbound_location_out_of_range_degrades_to_text_only(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture
):
    # An out-of-range latitude cannot build a LocationElement — the turn still bridges as
    # text (never lost), with no typed location.
    message = {"id": _WAMID, "from": WA_ID, "type": "location", "location": {"latitude": 999.0, "longitude": 2.5}}
    with caplog.at_level("WARNING"):
        result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["location"] is None
    assert call["text"] == "location: 999.0, 2.5"


async def test_inbound_contacts_bridge_names_as_text_and_cards_in_params(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    contacts = [
        {"name": {"formatted_name": "Jane Doe"}, "phones": [{"phone": "+15551230001"}]},
        {"name": {"formatted_name": "John Roe"}},
    ]
    message = {"id": _WAMID, "from": WA_ID, "type": "contacts", "contacts": contacts}
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["text"] == "Jane Doe, John Roe"
    assert call["params"]["contacts_count"] == "2"
    assert json.loads(call["params"]["contacts"]) == contacts


async def test_inbound_reaction_carries_emoji_and_target_params(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    message = {
        "id": _WAMID,
        "from": WA_ID,
        "type": "reaction",
        "reaction": {"message_id": "wamid.TARGET", "emoji": "👍"},
    }
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["text"] == "👍"
    assert call["params"] == {"reaction_emoji": "👍", "reaction_message_id": "wamid.TARGET"}


async def test_inbound_removed_reaction_has_placeholder_text_and_no_emoji_param(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    message = {
        "id": _WAMID,
        "from": WA_ID,
        "type": "reaction",
        "reaction": {"message_id": "wamid.TARGET", "emoji": ""},
    }
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["text"] == "[reaction removed]"
    assert call["params"] == {"reaction_message_id": "wamid.TARGET"}  # empty emoji dropped


async def test_inbound_media_records_known_contact_marker(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    message = {"id": _WAMID, "from": WA_ID, "type": "image", "image": {"id": "m1", "caption": "hi"}}
    await handler(signed_request(_params_envelope(message)))

    assert _CONTACT_KEY in fake_redis.store  # a participant photo opens Meta's window too
