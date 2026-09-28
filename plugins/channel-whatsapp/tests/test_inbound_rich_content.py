"""Always-bridge rich content — inbound media, location, contacts, and reactions.

Media is fetched from the vendor and ingested into the platform's served-media store, bridging
as a turn carrying the typed ``attachments`` entry + parity ``media_*`` params; location, contacts,
and reactions each bridge as a fresh turn. None of them ever answers a pending ask."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from tai42_contract.conversations import InboundRejectionReason
from tai42_contract.interactions import (
    IngestedMedia,
    MediaSourceReadError,
    MediaStoreUnavailableError,
    MediaTooLargeError,
    MediaTypeNotAllowedError,
)
from tai42_contract.interactions.models import LocationElement, MediaItem, MediaKind
from tai42_kit.net import MediaFetchError, UrlGuardError

import tai42_channel_whatsapp.inbound  # noqa: F401  (route registration side-effect)
from tai42_channel_whatsapp.client import fetch_media_metadata

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
    response,
    signed_request,
)

pytestmark = pytest.mark.usefixtures("whatsapp_env")

_SERVED_ID = "A" * 43
_SERVED_URL = f"/api/interactions/media/{_SERVED_ID}"
_LOOKASIDE_URL = "https://lookaside.example/download/blob"
_ACCESS_TOKEN = "test-access-token"


def _meta_response(
    *,
    url: str = _LOOKASIDE_URL,
    mime_type: str | None = "image/jpeg",
    sha256: str | None = "deadbeef",
    file_size: int | None = 1234,
    media_id: str = "media-abc",
) -> httpx.Response:
    """A Graph media-metadata JSON response (the ``{url, mime_type, sha256, file_size}`` lookaside)."""
    body: dict[str, Any] = {"id": media_id}
    if url is not None:
        body["url"] = url
    if mime_type is not None:
        body["mime_type"] = mime_type
    if sha256 is not None:
        body["sha256"] = sha256
    if file_size is not None:
        body["file_size"] = file_size
    return response(200, json=body)


def _ingested(
    *,
    kind: MediaKind = MediaKind.IMAGE,
    filename: str | None = None,
    mime: str = "image/jpeg",
    sha256: str = "e" * 64,
    size: int = 1234,
    media_id: str = _SERVED_ID,
) -> IngestedMedia:
    """A served :class:`IngestedMedia` as the seam would return it (a served ``MediaItem`` + metadata)."""
    item = MediaItem(kind=kind, url=_SERVED_URL, filename=filename)
    return IngestedMedia(item=item, media_id=media_id, size=size, sha256=sha256, mime=mime)


async def _aiter(chunks: list[bytes]) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


@dataclass
class _FakeMediaStream:
    content_type: str | None
    content_length: int | None
    host: str
    chunks: AsyncIterator[bytes]


def _stub_stream(
    monkeypatch: pytest.MonkeyPatch,
    *,
    chunks: tuple[bytes, ...] = (b"bytes",),
    content_type: str | None = None,
    content_length: int | None = None,
    host: str = "lookaside.example",
    error: Exception | None = None,
    capture: list[dict[str, Any]] | None = None,
) -> None:
    """Replace the kit's ``open_media_stream`` at the client import site with a fake context manager."""

    @asynccontextmanager
    async def _open(
        url: str,
        *,
        headers: dict[str, str] | None = None,
        auth: tuple[str, str] | None = None,
        follow_redirects: bool = False,
    ) -> AsyncIterator[_FakeMediaStream]:
        if capture is not None:
            capture.append({"url": url, "headers": headers, "auth": auth, "follow_redirects": follow_redirects})
        if error is not None:
            raise error
        yield _FakeMediaStream(content_type, content_length, host, _aiter(list(chunks)))

    monkeypatch.setattr("tai42_channel_whatsapp.client.open_media_stream", _open)


async def test_inbound_image_with_caption_ingests_and_bridges_typed_attachment(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    # A captioned photo: metadata lookup → byte stream → ingest → a served MediaItem bridged as a
    # typed attachment AND parity params (media_id is now the SERVED id, sha/size/mime the ingested
    # values). The caption is the turn text.
    fake_httpx.get_responses.append(_meta_response())
    stub_app.media.ingest_result = _ingested(mime="image/jpeg", sha256="a" * 64, size=4096)
    capture: list[dict[str, Any]] = []
    _stub_stream(monkeypatch, chunks=(b"jpeg-bytes",), capture=capture)

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
    assert call["attachments"] == [stub_app.media.ingest_result.item]
    assert call["params"] == {
        "media_kind": "image",
        "media_id": _SERVED_ID,
        "media_mime_type": "image/jpeg",
        "media_sha256": "a" * 64,
        "media_size": "4096",
    }
    # The metadata GET carried the Bearer to the Graph media endpoint.
    (meta_call,) = fake_httpx.get_calls
    assert meta_call["url"] == "https://graph.facebook.com/v23.0/media-abc"
    assert meta_call["headers"]["Authorization"] == f"Bearer {_ACCESS_TOKEN}"
    # The download hop carried the Bearer to the lookaside url.
    (download,) = capture
    assert download["url"] == _LOOKASIDE_URL
    assert download["headers"]["Authorization"] == f"Bearer {_ACCESS_TOKEN}"
    # The ingest saw the declared metadata and the message-bound origin.
    (ingest,) = stub_app.media.ingest_calls
    assert ingest["declared_mime"] == "image/jpeg"
    assert ingest["declared_size"] == 1234
    assert ingest["integrity_sha256"] == "deadbeef"
    assert ingest["origin"].channel_id == "whatsapp"
    assert ingest["origin"].participant_identity == WA_ID
    assert ingest["origin"].message_id == _WAMID
    assert _SEEN_KEY in fake_redis.store


async def test_inbound_image_ingest_source_is_the_byte_stream(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    # The seam receives the live byte stream (the chunks the download yields), not a buffered blob.
    fake_httpx.get_responses.append(_meta_response())
    stub_app.media.ingest_result = _ingested()
    _stub_stream(monkeypatch, chunks=(b"chunk-1", b"chunk-2"))

    message = {"id": _WAMID, "from": WA_ID, "type": "image", "image": {"id": "media-abc", "mime_type": "image/jpeg"}}
    await handler(signed_request(_params_envelope(message)))

    (ingest,) = stub_app.media.ingest_calls
    assert [chunk async for chunk in ingest["source"]] == [b"chunk-1", b"chunk-2"]


async def test_inbound_image_without_caption_uses_placeholder_text(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    fake_httpx.get_responses.append(_meta_response(mime_type="image/png"))
    stub_app.media.ingest_result = _ingested(mime="image/png")
    _stub_stream(monkeypatch)

    message = {"id": _WAMID, "from": WA_ID, "type": "image", "image": {"id": "m1", "mime_type": "image/png"}}
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["text"] == "[image]"  # accept refuses blank text — a faithful placeholder rides
    assert call["params"]["media_kind"] == "image"
    assert call["params"]["media_id"] == _SERVED_ID


async def test_inbound_document_carries_sanitised_filename_in_text_and_params(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    fake_httpx.get_responses.append(_meta_response(mime_type="application/pdf"))
    stub_app.media.ingest_result = _ingested(kind=MediaKind.DOCUMENT, filename="report.pdf", mime="application/pdf")
    _stub_stream(monkeypatch)

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
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    fake_httpx.get_responses.append(_meta_response(mime_type="audio/ogg"))
    stub_app.media.ingest_result = _ingested(kind=MediaKind.AUDIO, mime="audio/ogg")
    _stub_stream(monkeypatch)

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
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    fake_httpx.get_responses.append(_meta_response(mime_type="image/webp"))
    # A sticker's sniffed type is an image — the served MediaItem is IMAGE, the wire kind STICKER.
    stub_app.media.ingest_result = _ingested(kind=MediaKind.IMAGE, mime="image/webp")
    _stub_stream(monkeypatch)

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
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    fake_httpx.get_responses.append(_meta_response(mime_type="video/mp4"))
    stub_app.media.ingest_result = _ingested(kind=MediaKind.VIDEO, mime="video/mp4")
    _stub_stream(monkeypatch)

    message = {"id": _WAMID, "from": WA_ID, "type": "video", "video": {"id": "v1", "mime_type": "video/mp4"}}
    await handler(signed_request(_params_envelope(message)))
    # A redelivery of the same wamid is short-circuited (already seen) before any fetch.
    await handler(signed_request(_params_envelope(message)))

    assert len(stub_app.conversations.accept_calls) == 1
    assert stub_app.conversations.accept_calls[0]["params"]["media_kind"] == "video"


async def test_inbound_media_caption_carries_reply_context_params(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    # Message-level context (reply-to) merges with the media params on the bridged turn.
    fake_httpx.get_responses.append(_meta_response())
    stub_app.media.ingest_result = _ingested()
    _stub_stream(monkeypatch)

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
    assert call["params"]["media_id"] == _SERVED_ID


async def test_inbound_media_while_ask_pending_bridges_leaving_ask_parked(
    handler, stub_app, channels, fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    # A photo cannot answer a pending text/select/form ask — it bridges as a fresh turn and
    # the parked ask is left untouched (never reaches the answer ladder).
    await _seed_pending()
    fake_httpx.get_responses.append(_meta_response())
    stub_app.media.ingest_result = _ingested()
    _stub_stream(monkeypatch)

    message = {"id": _WAMID, "from": WA_ID, "type": "image", "image": {"id": "m1", "caption": "unrelated photo"}}
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    assert channels.inbound_calls == []  # never the answer ladder
    assert stub_app.conversations.accept_calls[0]["text"] == "unrelated photo"
    assert _PENDING_KEY in fake_redis.store  # the ask stays parked


async def test_inbound_media_over_cap_rejects_too_large_and_preserves_caption(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    # An over-cap image: the seam raises MediaTooLargeError → notify TOO_LARGE + ack, the media is
    # NOT bridged as an attachment, and the caption still bridges as a text-only turn (never lost).
    fake_httpx.get_responses.append(_meta_response(mime_type="image/jpeg"))
    stub_app.media.ingest_error = MediaTooLargeError("media body exceeds the cap")
    _stub_stream(monkeypatch)

    message = {
        "id": _WAMID,
        "from": WA_ID,
        "type": "image",
        "image": {"id": "m1", "mime_type": "image/jpeg", "caption": "here it is"},
    }
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (rejected,) = stub_app.conversations.rejected_calls
    assert rejected["reason"] == InboundRejectionReason.TOO_LARGE
    assert rejected["kind"] == "image"
    assert rejected["recipient"] == WA_ID
    assert rejected["sender_identity"] == stub_app.conversations.accept_calls[0]["our_identity"]
    (call,) = stub_app.conversations.accept_calls
    assert call["text"] == "here it is"
    assert call["attachments"] is None  # the media does not exist — no served attachment
    assert call["params"] == {"media_kind": "image", "media_mime_type": "image/jpeg"}  # no served id/sha/size
    assert _SEEN_KEY in fake_redis.store


async def test_inbound_media_over_cap_without_caption_rejects_and_acks(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    fake_httpx.get_responses.append(_meta_response())
    stub_app.media.ingest_error = MediaTooLargeError("too big")
    _stub_stream(monkeypatch)

    message = {"id": _WAMID, "from": WA_ID, "type": "image", "image": {"id": "m1", "mime_type": "image/jpeg"}}
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (rejected,) = stub_app.conversations.rejected_calls
    assert rejected["reason"] == InboundRejectionReason.TOO_LARGE
    assert stub_app.conversations.accept_calls == []  # no caption, notice-only
    assert _SEEN_KEY in fake_redis.store


async def test_inbound_media_disallowed_type_rejects_unsupported(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    fake_httpx.get_responses.append(_meta_response(mime_type="image/svg+xml"))
    stub_app.media.ingest_error = MediaTypeNotAllowedError("svg is active content")
    _stub_stream(monkeypatch)

    message = {"id": _WAMID, "from": WA_ID, "type": "image", "image": {"id": "m1", "mime_type": "image/svg+xml"}}
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (rejected,) = stub_app.conversations.rejected_calls
    assert rejected["reason"] == InboundRejectionReason.UNSUPPORTED_TYPE
    assert _SEEN_KEY in fake_redis.store


async def test_inbound_media_store_unavailable_rejects_could_not_receive(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    fake_httpx.get_responses.append(_meta_response())
    stub_app.media.ingest_error = MediaStoreUnavailableError("no blob provider")
    _stub_stream(monkeypatch)

    message = {"id": _WAMID, "from": WA_ID, "type": "image", "image": {"id": "m1", "mime_type": "image/jpeg"}}
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (rejected,) = stub_app.conversations.rejected_calls
    assert rejected["reason"] == InboundRejectionReason.COULD_NOT_RECEIVE
    assert _SEEN_KEY in fake_redis.store


async def test_inbound_media_gone_at_metadata_rejects_could_not_receive(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    # The Graph media object has expired (404) — a permanent metadata fault: notify COULD_NOT_RECEIVE
    # + ack, the bytes are never fetched.
    fake_httpx.get_responses.append(response(404, json={"error": {"message": "not found"}}))
    _stub_stream(monkeypatch)

    message = {"id": _WAMID, "from": WA_ID, "type": "image", "image": {"id": "gone-1", "mime_type": "image/jpeg"}}
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (rejected,) = stub_app.conversations.rejected_calls
    assert rejected["reason"] == InboundRejectionReason.COULD_NOT_RECEIVE
    assert stub_app.media.ingest_calls == []  # never reached the byte fetch
    assert _SEEN_KEY in fake_redis.store


async def test_inbound_media_url_guard_reject_could_not_receive(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    # The lookaside url resolves to a guarded address — a permanent SSRF rejection at open.
    fake_httpx.get_responses.append(_meta_response())
    _stub_stream(monkeypatch, error=UrlGuardError("SSRF guard: blocked host"))

    message = {"id": _WAMID, "from": WA_ID, "type": "image", "image": {"id": "m1", "mime_type": "image/jpeg"}}
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (rejected,) = stub_app.conversations.rejected_calls
    assert rejected["reason"] == InboundRejectionReason.COULD_NOT_RECEIVE
    assert _SEEN_KEY in fake_redis.store


async def test_inbound_media_transient_metadata_fault_raises_and_does_not_mark_seen(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    # A Graph 5xx at metadata is transient: it RAISES so the webhook 5xx's and Meta redelivers;
    # the wamid is NOT marked seen (a redelivery must not dedupe the still-unhandled message away).
    fake_httpx.get_responses.append(response(502, text="bad gateway"))
    _stub_stream(monkeypatch)

    message = {"id": _WAMID, "from": WA_ID, "type": "image", "image": {"id": "m1", "mime_type": "image/jpeg"}}
    with pytest.raises(MediaFetchError):
        await handler(signed_request(_params_envelope(message)))

    assert stub_app.conversations.rejected_calls == []
    assert stub_app.conversations.accept_calls == []
    assert _SEEN_KEY not in fake_redis.store


async def test_fetch_media_metadata_transport_fault_carries_no_url_or_token_in_the_chain(
    stub_app, fake_httpx: FakeHttpx
):
    # A transport fault on the metadata GET: httpx's raw exception carries the request URL and, on
    # its ``.request``, the ``Authorization: Bearer`` header. The re-wrapped MediaFetchError must
    # chain neither (``__cause__``/``__context__`` are None) and carry the token in no error text.
    raw = httpx.ConnectError(f"connect failed https://graph.facebook.com/v23.0/media-x Bearer {_ACCESS_TOKEN}")
    fake_httpx.get_responses.append(raw)

    with pytest.raises(MediaFetchError) as ei:
        await fetch_media_metadata("media-x")

    exc = ei.value
    assert exc.transient is True  # a transport fault is transient (a redelivery may succeed)
    assert exc.status_code is None
    assert exc.__cause__ is None  # the URL/token-bearing httpx exception is not chained on
    assert exc.__context__ is None
    assert _ACCESS_TOKEN not in str(exc)
    assert "media-x" not in str(exc)


async def test_inbound_media_transient_download_read_raises_and_does_not_mark_seen(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    # A torn body read surfaces from the seam as MediaSourceReadError: transient → RAISE, not seen.
    fake_httpx.get_responses.append(_meta_response())
    stub_app.media.ingest_error = MediaSourceReadError("body read torn")
    _stub_stream(monkeypatch)

    message = {"id": _WAMID, "from": WA_ID, "type": "image", "image": {"id": "m1", "mime_type": "image/jpeg"}}
    with pytest.raises(MediaSourceReadError):
        await handler(signed_request(_params_envelope(message)))

    assert _SEEN_KEY not in fake_redis.store


async def test_inbound_media_redelivery_after_permanent_reject_sends_no_second_reply(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    # After a permanent reject marks the wamid seen, a Meta redelivery is short-circuited: exactly
    # one rejection, never a second reply.
    fake_httpx.get_responses.append(response(404, json={"error": {"message": "gone"}}))
    _stub_stream(monkeypatch)

    message = {"id": _WAMID, "from": WA_ID, "type": "image", "image": {"id": "gone-1", "mime_type": "image/jpeg"}}
    await handler(signed_request(_params_envelope(message)))
    await handler(signed_request(_params_envelope(message)))

    assert len(stub_app.conversations.rejected_calls) == 1


async def test_whatsapp_parity_media_filename_equals_sanitised(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    # A document whose Graph filename carries a control char and a bidi override: the RAW name is
    # handed to the seam, and the bridged parity media_filename equals the SANITISED item.filename —
    # the raw vendor substring appears in no param value.
    raw_name = "re‮port\x00.pdf"
    fake_httpx.get_responses.append(_meta_response(mime_type="application/pdf"))
    stub_app.media.ingest_result = _ingested(kind=MediaKind.DOCUMENT, filename="report.pdf", mime="application/pdf")
    _stub_stream(monkeypatch)

    message = {
        "id": _WAMID,
        "from": WA_ID,
        "type": "document",
        "document": {"id": "doc-1", "mime_type": "application/pdf", "filename": raw_name, "caption": "the file"},
    }
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (ingest,) = stub_app.media.ingest_calls
    assert ingest["filename"] == raw_name  # the RAW vendor name goes to the seam, which sanitises it
    (call,) = stub_app.conversations.accept_calls
    assert call["params"]["media_filename"] == "report.pdf"
    joined = "".join(call["params"].values())
    assert "‮" not in joined
    assert "\x00" not in joined


async def test_whatsapp_placeholder_label_uses_sanitised_filename(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    # The same document with no caption: the turn text is [document: <sanitised name>]; the raw
    # vendor filename substring appears nowhere in the turn text or params.
    raw_name = "re‮port\x00.pdf"
    fake_httpx.get_responses.append(_meta_response(mime_type="application/pdf"))
    stub_app.media.ingest_result = _ingested(kind=MediaKind.DOCUMENT, filename="report.pdf", mime="application/pdf")
    _stub_stream(monkeypatch)

    message = {
        "id": _WAMID,
        "from": WA_ID,
        "type": "document",
        "document": {"id": "doc-1", "mime_type": "application/pdf", "filename": raw_name},
    }
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["text"] == "[document: report.pdf]"
    haystack = call["text"] + "".join(call["params"].values())
    assert "‮" not in haystack
    assert "\x00" not in haystack


async def test_inbound_media_records_known_contact_marker(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    fake_httpx.get_responses.append(_meta_response())
    stub_app.media.ingest_result = _ingested()
    _stub_stream(monkeypatch)

    message = {"id": _WAMID, "from": WA_ID, "type": "image", "image": {"id": "m1", "caption": "hi"}}
    await handler(signed_request(_params_envelope(message)))

    assert _CONTACT_KEY in fake_redis.store  # a participant photo opens Meta's window too


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
