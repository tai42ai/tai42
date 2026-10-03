"""The inbound webhook handler — signature validation, dedupe, correlation, forward policy."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.responses import Response
from tai42_contract.channels import AnswerForwardError, InboundAnswerOutcome
from tai42_contract.conversations import (
    BlankInboundTextError,
    DeliveryReceipt,
    InboundMediaKind,
    InboundRejectionReason,
)
from tai42_contract.interactions import (
    MediaSourceReadError,
    MediaStoreUnavailableError,
    MediaTooLargeError,
    MediaTypeNotAllowedError,
)
from tai42_kit.net import MediaFetchError, UrlGuardError

import tai42_channel_twilio.inbound
from tai42_channel_twilio.correlation import reserve_pending

from .conftest import (
    FakeHttpx,
    FakeRedis,
    build_request,
    compute_signature,
    make_delivery,
    response,
    signed_request,
)

pytestmark = pytest.mark.usefixtures("twilio_env")

# The absolute URL Twilio delivers to and signs over (the mount base resolves to
# this at the default). The registered route is the relative ``/inbound`` beneath it.
_PATH = "/api/channels/twilio/inbound"
_ROUTE = "/inbound"
_TWILIO = "+15550000001"
_HUMAN = "+15550000002"
_CALLBACK = "https://app.example/api/interactions/callback/ticket-1"
_SEEN_KEY = "channel:twilio:seen:SM777"


def _pairs(**overrides: str) -> list[tuple[str, str]]:
    form = {"MessageSid": "SM777", "To": _TWILIO, "From": _HUMAN, "Body": "yes please"}
    form.update(overrides)
    return list(form.items())


@pytest.fixture
def handler(stub_app) -> Callable[..., Awaitable[Response]]:
    routes = [route for route in stub_app.http.routes if route.path == _ROUTE]
    assert len(routes) == 1
    route = routes[0]
    assert route.methods == ["POST"]
    assert route.authed is None
    return route.handler


async def _seed_pending(callback_url: str = _CALLBACK) -> None:
    delivery = make_delivery(callback_url=callback_url)
    await reserve_pending(_TWILIO, _HUMAN, delivery.callback_url, delivery.interaction_id, delivery.timeout_at)


async def _pending_intact(fake_redis: FakeRedis) -> bool:
    return f"channel:twilio:pending:{_TWILIO}:{_HUMAN}" in fake_redis.store


# --- Happy path ---------------------------------------------------------------


async def test_valid_signature_hands_reply_to_shared_ladder(handler, channels, fake_redis: FakeRedis):
    # The signed reply is handed to the ONE shared ladder with the number-pair key,
    # the Body as the answer, and the bridge context; a FORWARDED outcome acks 204
    # and marks the sid seen. The ladder does the forwarding, not the plugin.
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED

    result = await handler(signed_request(_pairs()))

    assert result.status_code == 204
    assert len(channels.inbound_calls) == 1
    call = channels.inbound_calls[0]
    assert call.channel_id == "twilio"
    assert call.correlation_key == f"{_TWILIO}:{_HUMAN}"
    assert call.answer == "yes please"
    assert call.bridge.channel_id == "twilio"
    assert call.bridge.our_identity == _TWILIO
    assert call.bridge.client_address == _HUMAN
    assert call.bridge.cap_key == _HUMAN
    assert call.bridge.provider_message_id == "SM777"
    assert call.bridge.bridge_text == "yes please"
    assert _SEEN_KEY in fake_redis.store  # sid marked seen on a resolved outcome


async def test_answer_is_body_verbatim_minus_outer_whitespace(handler, channels, fake_redis: FakeRedis):
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED
    await handler(signed_request(_pairs(Body=" yes please \n")))
    # The answer forwarded to the ladder is the Body minus outer whitespace...
    assert channels.inbound_calls[0].answer == "yes please"
    # ...while the bridge text a gone-ask fallback would use is the Body verbatim.
    assert channels.inbound_calls[0].bridge.bridge_text == " yes please \n"


async def test_public_url_reconstructed_from_forwarded_headers(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # The ASGI scope says http://app-internal:8000, the forwarded headers say
    # https://public.example — the signature over the PUBLIC url must validate.
    await _seed_pending()
    fake_httpx.responses.append(response(200))

    result = await handler(signed_request(_pairs(), proto="https", host="public.example"))

    assert result.status_code == 204


async def test_duplicate_form_keys_validate(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A dict-collapsing implementation would drop one MediaUrl0 pair and 401 here.
    await _seed_pending()
    fake_httpx.responses.append(response(200))
    pairs = [*_pairs(), ("MediaUrl0", "https://a.example/1"), ("MediaUrl0", "https://b.example/2")]

    result = await handler(signed_request(pairs))

    assert result.status_code == 204


async def test_query_string_is_part_of_the_signed_url(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()
    fake_httpx.responses.append(response(200))

    result = await handler(signed_request(_pairs(), query="x=1"))
    assert result.status_code == 204

    # The same request signed WITHOUT the query must be rejected (a signature failure
    # short-circuits before the store, so no fresh reservation is needed here).
    rejected = await handler(signed_request(_pairs(), query="x=1", sign_url=f"https://public.example{_PATH}"))
    assert rejected.status_code == 401


async def test_fallback_to_request_scheme_and_host_without_proxy_headers(
    handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # No X-Forwarded-* headers: the signed URL is the request's own scheme + Host.
    await _seed_pending()
    fake_httpx.responses.append(response(200))

    def strip_forwarded(headers: dict[str, str]) -> None:
        del headers["x-forwarded-proto"]
        del headers["x-forwarded-host"]

    result = await handler(
        signed_request(_pairs(), sign_url=f"http://app-internal:8000{_PATH}", tamper=strip_forwarded)
    )

    assert result.status_code == 204


# --- Fail-closed branches -----------------------------------------------------


async def _assert_rejected(handler, request, fake_redis: FakeRedis, fake_httpx: FakeHttpx, status: int = 401):
    result = await handler(request)
    assert result.status_code == status
    assert not fake_httpx.calls  # zero forwards
    assert await _pending_intact(fake_redis)  # pending NOT consumed


async def test_missing_signature_header_rejected(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()
    await _assert_rejected(handler, signed_request(_pairs(), omit_signature=True), fake_redis, fake_httpx)


async def test_wrong_token_signature_rejected(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()
    await _assert_rejected(handler, signed_request(_pairs(), token="other-token"), fake_redis, fake_httpx)


async def test_url_mismatch_rejected(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()
    request = signed_request(_pairs(), sign_url=f"https://evil.example{_PATH}")
    await _assert_rejected(handler, request, fake_redis, fake_httpx)


async def test_non_base64_signature_rejected(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()
    request = signed_request(_pairs(), signature="!!! not base64 !!!")
    await _assert_rejected(handler, request, fake_redis, fake_httpx)


async def test_wrong_digest_length_rejected(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()
    request = signed_request(_pairs(), signature="c2hvcnQ=")  # base64("short") — 5 bytes, not 20
    await _assert_rejected(handler, request, fake_redis, fake_httpx)


async def test_empty_auth_token_fails_closed_with_config_error(
    handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    from tai42_kit.settings import reset_all_settings

    await _seed_pending()
    monkeypatch.setenv("CHANNEL_TWILIO_AUTH_TOKEN", "")
    reset_all_settings()

    # Operator misconfiguration is a logged, constant 500 — never a 401 that
    # reads like an ordinary bad signature, never any processing.
    result = await handler(signed_request(_pairs()))

    assert result.status_code == 500
    assert json.loads(result.body) == {"error": "channel misconfigured"}
    assert not fake_httpx.calls
    assert await _pending_intact(fake_redis)


async def test_tampered_body_rejected(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()
    # Sign one body, deliver another.
    signature = compute_signature("testtoken", f"https://public.example{_PATH}", _pairs())
    request = signed_request(_pairs(Body="no way"), signature=signature)
    await _assert_rejected(handler, request, fake_redis, fake_httpx)


async def test_missing_host_header_rejected(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()

    def strip_hosts(headers: dict[str, str]) -> None:
        del headers["x-forwarded-host"]
        del headers["host"]

    await _assert_rejected(handler, signed_request(_pairs(), tamper=strip_hosts), fake_redis, fake_httpx)


async def test_oversized_body_413_before_any_signature_work(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()
    # No X-Twilio-Signature header at all, yet the response is 413 (not 401):
    # the bounded read runs BEFORE any HMAC work.
    request = build_request(
        body=b"",
        chunks=[b"x" * (512 * 1024), b"y" * (512 * 1024), b"z"],
        headers={"host": "app-internal:8000", "content-type": "application/x-www-form-urlencoded"},
    )
    await _assert_rejected(handler, request, fake_redis, fake_httpx, status=413)


async def test_non_utf8_body_is_clean_401(
    handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture
):
    await _seed_pending()
    request = build_request(
        body=b"\xff\xfe",
        headers={
            "host": "app-internal:8000",
            "content-type": "application/x-www-form-urlencoded",
            "x-twilio-signature": "AAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        },
    )

    with caplog.at_level("WARNING"):
        await _assert_rejected(handler, request, fake_redis, fake_httpx)

    assert any("not valid UTF-8" in record.message for record in caplog.records)


# --- Post-signature branches ---------------------------------------------------


async def test_missing_message_sid_is_400(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()
    pairs = [pair for pair in _pairs() if pair[0] != "MessageSid"]
    await _assert_rejected(handler, signed_request(pairs), fake_redis, fake_httpx, status=400)


async def test_message_sid_dedupe_skips_second_delivery(handler, channels, fake_redis: FakeRedis):
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED

    first = await handler(signed_request(_pairs()))
    second = await handler(signed_request(_pairs()))

    assert first.status_code == 204
    assert second.status_code == 204
    # The second delivery is deduped on its MessageSid BEFORE the ladder — the shared
    # handler is consulted exactly once.
    assert len(channels.inbound_calls) == 1


async def test_uncorrelated_unrouted_inbound_logged_ack_no_turn(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture
):
    # No pending question and no bound route: accept() raises LookupError -> the
    # bridge logs and success-acks, marks the sid seen, and starts no forward.
    stub_app.conversations.accept_error = LookupError("no channel conversation route matches (twilio, +15550000001)")

    with caplog.at_level("WARNING"):
        result = await handler(signed_request(_pairs()))

    assert result.status_code == 204
    assert not fake_httpx.calls  # no ask forward
    assert len(stub_app.conversations.accept_calls) == 1  # the bridge was attempted
    assert any("unrouted" in record.message for record in caplog.records)
    assert _SEEN_KEY in fake_redis.store  # replay of the same sid dedupes


async def test_uncorrelated_blank_inbound_logged_ack_no_turn(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture
):
    # No pending question and a whitespace-only body: accept() raises
    # BlankInboundTextError -> the bridge logs and success-acks, marks the sid seen,
    # and starts no forward (never a 5xx retry-storm), no turn produced.
    stub_app.conversations.accept_error = BlankInboundTextError("inbound text is blank")

    with caplog.at_level("WARNING"):
        result = await handler(signed_request(_pairs(Body="   ")))

    assert result.status_code == 204
    assert not fake_httpx.calls  # no ask forward
    assert len(stub_app.conversations.accept_calls) == 1  # the bridge was attempted, no turn produced
    assert any("blank" in record.message for record in caplog.records)
    assert _SEEN_KEY in fake_redis.store  # replay of the same sid dedupes


# --- Bridge branch (correlation miss) -----------------------------------------


async def test_uncorrelated_routed_inbound_calls_accept_with_verbatim_args(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # No pending question, a route is bound: accept() is called with To/From/Body
    # verbatim and the MessageSid, the sid is marked seen, and nothing is forwarded.
    result = await handler(signed_request(_pairs(Body="ship it")))

    assert result.status_code == 204
    assert not fake_httpx.calls  # bridge does not use the ask forward
    assert stub_app.conversations.accept_calls == [
        {
            "channel": "twilio",
            "our_identity": _TWILIO,
            "client_address": _HUMAN,
            # The provider attests the From number, so it is also the accountable cap key.
            "cap_key": _HUMAN,
            "text": "ship it",
            "provider_message_id": "SM777",
            "params": None,
            "attachments": None,
        }
    ]
    assert _SEEN_KEY in fake_redis.store


# --- Bridge branch: MMS media (correlation miss) ------------------------------

# The vendor MediaUrls Twilio delivers on the webhook (each 307s to a foreign CDN).
_MEDIA0 = "https://media.example/ME0"
_MEDIA1 = "https://media.example/ME1"
# The Basic-auth credential pair the twilio_env fixture configures.
_ACCOUNT = "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
_TOKEN = "testtoken"


async def _aiter_bytes(chunks: tuple[bytes, ...]) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


class _FakeStream:
    """A ``MediaStream``-shaped object the fake ``open_media_stream`` yields."""

    def __init__(
        self,
        *,
        content_type: str | None = None,
        content_length: int | None = None,
        host: str = "cdn.example",
        chunks: tuple[bytes, ...] = (b"media-bytes",),
    ) -> None:
        self.content_type = content_type
        self.content_length = content_length
        self.host = host
        self.chunks = _aiter_bytes(chunks)


def _patch_stream(monkeypatch: pytest.MonkeyPatch, recorder: list[dict[str, Any]], results: list[Any]) -> None:
    """Patch ``open_media_stream`` at the inbound import site with a fake context manager.

    Records each open call's arguments into ``recorder`` and, per call, yields the next
    ``_FakeStream`` in ``results`` or raises it when it is an exception.
    """
    queue = list(results)

    @asynccontextmanager
    async def _open(
        url: str,
        *,
        headers: dict[str, str] | None = None,
        auth: tuple[str, str] | None = None,
        follow_redirects: bool = False,
    ) -> AsyncIterator[Any]:
        recorder.append({"url": url, "headers": headers, "auth": auth, "follow_redirects": follow_redirects})
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        yield item

    monkeypatch.setattr(tai42_channel_twilio.inbound, "open_media_stream", _open)


def _ingested(
    *,
    media_id: str = "A" * 43,
    mime: str = "image/jpeg",
    size: int = 1234,
    sha256: str = "b" * 64,
    filename: str | None = None,
) -> SimpleNamespace:
    """An ``IngestedMedia``-shaped result: the served item plus its metadata."""
    item = SimpleNamespace(filename=filename, url=f"/api/interactions/media/{media_id}")
    return SimpleNamespace(item=item, media_id=media_id, size=size, sha256=sha256, mime=mime, pending=False)


@pytest.fixture
def stub_media(stub_app) -> Any:
    return stub_app.media


def _media_pairs(*, body: str = "", num: int = 1, sid: str = "SM777", **extra: str) -> list[tuple[str, str]]:
    form = {"MessageSid": sid, "To": _TWILIO, "From": _HUMAN, "Body": body, "NumMedia": str(num)}
    form.update(extra)
    return list(form.items())


async def test_media_fetch_passes_basic_auth_and_follows_redirects(
    handler, stub_app, stub_media, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
):
    # The vendor fetch carries HTTP Basic auth on the first hop and follows the 307 to the
    # CDN; the kit drops the auth on that cross-origin hop (its own proven behaviour).
    calls: list[dict[str, Any]] = []
    _patch_stream(monkeypatch, calls, [_FakeStream(content_length=1234)])
    stub_media.ingest_results.append(_ingested())

    await handler(signed_request(_media_pairs(MediaUrl0=_MEDIA0, MediaContentType0="image/jpeg")))

    assert calls == [{"url": _MEDIA0, "headers": None, "auth": (_ACCOUNT, _TOKEN), "follow_redirects": True}]


async def test_mms_single_image_fetches_ingests_and_bridges_served_turn(
    handler, stub_app, stub_media, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
):
    # An MMS image is fetched, streamed to the ONE ingest chokepoint, and bridged as a served
    # turn: the Body rides as text, the typed attachment AND the parity media_* params (with the
    # SERVED id) come off one ingest, the provider id is sid-0.
    served = _ingested(media_id="C" * 43, mime="image/jpeg", size=2048)
    stub_media.ingest_results.append(served)
    _patch_stream(monkeypatch, [], [_FakeStream(content_type="image/jpeg", content_length=2048)])

    result = await handler(
        signed_request(_media_pairs(body="look at this", MediaUrl0=_MEDIA0, MediaContentType0="image/jpeg"))
    )

    assert result.status_code == 204
    (ingest,) = stub_media.ingest_calls
    assert ingest["kind_hint"] is InboundMediaKind.IMAGE
    assert ingest["declared_mime"] == "image/jpeg"
    assert ingest["filename"] is None
    assert ingest["declared_size"] == 2048
    assert ingest["origin"].channel_id == "twilio"
    assert ingest["origin"].participant_identity == _HUMAN
    assert ingest["origin"].message_id == "SM777-0"
    (accept,) = stub_app.conversations.accept_calls
    assert accept["text"] == "look at this"
    assert accept["provider_message_id"] == "SM777-0"
    assert accept["attachments"] == [served.item]
    assert accept["params"] == {
        "media_kind": "image",
        "media_id": served.media_id,
        "media_mime_type": served.mime,
        "media_sha256": served.sha256,
        "media_size": str(served.size),
    }
    assert _SEEN_KEY in fake_redis.store


async def test_mms_multiple_media_bridges_one_served_turn_per_item(
    handler, stub_app, stub_media, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
):
    # NumMedia=2, blank Body: each item is fetched and ingested independently into one served
    # turn under a distinct provider id sid-N; a caption-less item uses the [kind] placeholder.
    served0 = _ingested(media_id="D" * 43, mime="image/png", size=10)
    served1 = _ingested(media_id="E" * 43, mime="video/mp4", size=20)
    stub_media.ingest_results.extend([served0, served1])
    calls: list[dict[str, Any]] = []
    _patch_stream(
        monkeypatch,
        calls,
        [_FakeStream(content_length=10), _FakeStream(content_length=20)],
    )

    result = await handler(
        signed_request(
            _media_pairs(
                num=2,
                MediaUrl0=_MEDIA0,
                MediaContentType0="image/png",
                MediaUrl1=_MEDIA1,
                MediaContentType1="video/mp4",
            )
        )
    )

    assert result.status_code == 204
    assert [call["url"] for call in calls] == [_MEDIA0, _MEDIA1]
    accepts = stub_app.conversations.accept_calls
    assert [call["provider_message_id"] for call in accepts] == ["SM777-0", "SM777-1"]
    assert accepts[0]["text"] == "[image]"
    assert accepts[0]["attachments"] == [served0.item]
    assert accepts[0]["params"]["media_id"] == served0.media_id
    assert accepts[1]["text"] == "[video]"
    assert accepts[1]["attachments"] == [served1.item]
    assert accepts[1]["params"]["media_kind"] == "video"
    assert _SEEN_KEY in fake_redis.store


async def test_mms_blank_body_document_bridges_with_sanitised_placeholder(
    handler, stub_app, stub_media, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
):
    # A blank Body + a document bridges: the [document: <name>] placeholder keeps the turn
    # non-blank, carrying the seam-derived SANITISED filename (never a raw vendor name).
    served = _ingested(media_id="F" * 43, mime="application/pdf", size=99, filename="document.pdf")
    stub_media.ingest_results.append(served)
    _patch_stream(monkeypatch, [], [_FakeStream(content_length=99)])

    result = await handler(
        signed_request(_media_pairs(body="   ", MediaUrl0=_MEDIA0, MediaContentType0="application/pdf"))
    )

    assert result.status_code == 204
    (accept,) = stub_app.conversations.accept_calls
    assert accept["text"] == "[document: document.pdf]"
    assert accept["provider_message_id"] == "SM777-0"
    assert accept["params"]["media_filename"] == "document.pdf"
    assert _SEEN_KEY in fake_redis.store


async def test_twilio_parity_media_filename_equals_sanitised(
    handler, stub_app, stub_media, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
):
    # Twilio sends NO vendor filename (ingest is called with filename=None); the bridged parity
    # media_filename equals the SANITISED ingested.item.filename the seam derives for a document.
    served = _ingested(media_id="G" * 43, mime="application/pdf", size=99, filename="document.pdf")
    stub_media.ingest_results.append(served)
    _patch_stream(monkeypatch, [], [_FakeStream(content_length=99)])

    await handler(signed_request(_media_pairs(body="report", MediaUrl0=_MEDIA0, MediaContentType0="application/pdf")))

    assert stub_media.ingest_calls[0]["filename"] is None
    (accept,) = stub_app.conversations.accept_calls
    assert accept["params"]["media_filename"] == served.item.filename == "document.pdf"


async def test_twilio_placeholder_label_uses_sanitised_filename(
    handler, stub_app, stub_media, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
):
    # A blank-Body document → the placeholder carries the sanitised name; an image → the seam
    # gives no filename, so media_filename is absent from the params.
    served_doc = _ingested(media_id="H" * 43, mime="application/pdf", size=99, filename="document.pdf")
    served_img = _ingested(media_id="I" * 43, mime="image/png", size=42, filename=None)
    stub_media.ingest_results.extend([served_doc, served_img])
    _patch_stream(monkeypatch, [], [_FakeStream(content_length=99), _FakeStream(content_length=42)])

    await handler(signed_request(_media_pairs(body="   ", MediaUrl0=_MEDIA0, MediaContentType0="application/pdf")))
    doc_accept = stub_app.conversations.accept_calls[0]
    assert doc_accept["text"] == "[document: document.pdf]"
    assert doc_accept["params"]["media_filename"] == "document.pdf"

    await handler(
        signed_request(_media_pairs(body="   ", sid="SM888", MediaUrl0=_MEDIA1, MediaContentType0="image/png"))
    )
    img_accept = stub_app.conversations.accept_calls[1]
    assert img_accept["text"] == "[image]"
    assert "media_filename" not in img_accept["params"]


# --- Bridge branch: MMS media failures ----------------------------------------


async def test_over_cap_media_blank_body_notifies_too_large_and_acks(
    handler, stub_app, stub_media, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
):
    # A blank-Body over-cap media: the chokepoint raises MediaTooLargeError -> notify TOO_LARGE
    # and ack; no turn is bridged (no caption), the sid is marked seen (permanent, no redeliver).
    stub_media.ingest_results.append(MediaTooLargeError("media exceeds the image cap"))
    _patch_stream(monkeypatch, [], [_FakeStream(content_length=10_000_000)])

    result = await handler(signed_request(_media_pairs(MediaUrl0=_MEDIA0, MediaContentType0="image/jpeg")))

    assert result.status_code == 204
    assert stub_app.conversations.rejected_calls == [
        {
            "channel_id": "twilio",
            "recipient": _HUMAN,
            "sender_identity": _TWILIO,
            "kind": "image",
            "reason": InboundRejectionReason.TOO_LARGE,
        }
    ]
    assert stub_app.conversations.accept_calls == []  # no caption -> no turn, never a silent drop
    assert _SEEN_KEY in fake_redis.store


async def test_permanent_reject_with_caption_bridges_caption_text_turn(
    handler, stub_app, stub_media, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
):
    # A first item over-cap WITH a Body caption: notify TOO_LARGE AND bridge the caption as a
    # text turn carrying only the kind/mime parity params (no served reference — the media is gone).
    stub_media.ingest_results.append(MediaTooLargeError("too big"))
    _patch_stream(monkeypatch, [], [_FakeStream(content_length=10_000_000)])

    result = await handler(
        signed_request(_media_pairs(body="look at this", MediaUrl0=_MEDIA0, MediaContentType0="image/jpeg"))
    )

    assert result.status_code == 204
    assert stub_app.conversations.rejected_calls[0]["reason"] is InboundRejectionReason.TOO_LARGE
    (accept,) = stub_app.conversations.accept_calls
    assert accept["text"] == "look at this"
    assert accept["provider_message_id"] == "SM777-0"
    assert accept["attachments"] is None
    assert accept["params"] == {"media_kind": "image", "media_mime_type": "image/jpeg"}
    assert _SEEN_KEY in fake_redis.store


async def test_media_gone_notifies_could_not_receive_and_acks(
    handler, stub_app, stub_media, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
):
    # A 404 at open (the media is gone) is a permanent, non-transient MediaFetchError -> notify
    # COULD_NOT_RECEIVE and ack; the ingest chokepoint is never reached.
    _patch_stream(monkeypatch, [], [MediaFetchError(host="cdn.example", status_code=404)])

    result = await handler(signed_request(_media_pairs(MediaUrl0=_MEDIA0, MediaContentType0="image/jpeg")))

    assert result.status_code == 204
    assert stub_app.conversations.rejected_calls[0]["reason"] is InboundRejectionReason.COULD_NOT_RECEIVE
    assert stub_media.ingest_calls == []
    assert stub_app.conversations.accept_calls == []
    assert _SEEN_KEY in fake_redis.store


async def test_disallowed_type_notifies_unsupported_and_acks(
    handler, stub_app, stub_media, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
):
    stub_media.ingest_results.append(MediaTypeNotAllowedError("sniffed type not on the allowlist"))
    _patch_stream(monkeypatch, [], [_FakeStream(content_length=42)])

    result = await handler(signed_request(_media_pairs(MediaUrl0=_MEDIA0, MediaContentType0="image/svg+xml")))

    assert result.status_code == 204
    assert stub_app.conversations.rejected_calls[0]["reason"] is InboundRejectionReason.UNSUPPORTED_TYPE
    assert _SEEN_KEY in fake_redis.store


async def test_store_unavailable_notifies_could_not_receive_and_acks(
    handler, stub_app, stub_media, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
):
    stub_media.ingest_results.append(MediaStoreUnavailableError("no blob provider registered"))
    _patch_stream(monkeypatch, [], [_FakeStream(content_length=42)])

    result = await handler(signed_request(_media_pairs(MediaUrl0=_MEDIA0, MediaContentType0="image/jpeg")))

    assert result.status_code == 204
    assert stub_app.conversations.rejected_calls[0]["reason"] is InboundRejectionReason.COULD_NOT_RECEIVE
    assert _SEEN_KEY in fake_redis.store


async def test_url_guard_error_notifies_could_not_receive_and_acks(
    handler, stub_app, stub_media, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
):
    _patch_stream(monkeypatch, [], [UrlGuardError("target host is not public")])

    result = await handler(signed_request(_media_pairs(MediaUrl0=_MEDIA0, MediaContentType0="image/jpeg")))

    assert result.status_code == 204
    assert stub_app.conversations.rejected_calls[0]["reason"] is InboundRejectionReason.COULD_NOT_RECEIVE
    assert _SEEN_KEY in fake_redis.store


async def test_transient_fetch_5xx_raises_and_does_not_dedupe(
    handler, stub_app, stub_media, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
):
    # A 5xx at open is a transient MediaFetchError -> propagate (5xx webhook) so Twilio
    # redelivers; the sid is NOT marked seen and nothing is notified.
    _patch_stream(monkeypatch, [], [MediaFetchError(host="cdn.example", status_code=502)])

    with pytest.raises(MediaFetchError):
        await handler(signed_request(_media_pairs(MediaUrl0=_MEDIA0, MediaContentType0="image/jpeg")))

    assert _SEEN_KEY not in fake_redis.store
    assert stub_app.conversations.rejected_calls == []
    assert stub_app.conversations.accept_calls == []


async def test_source_read_error_raises_and_does_not_dedupe(
    handler, stub_app, stub_media, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
):
    # A body-read fault surfaced by the seam is transient -> propagate, sid unmarked.
    stub_media.ingest_results.append(MediaSourceReadError("torn read from the vendor stream"))
    _patch_stream(monkeypatch, [], [_FakeStream(content_length=42)])

    with pytest.raises(MediaSourceReadError):
        await handler(signed_request(_media_pairs(MediaUrl0=_MEDIA0, MediaContentType0="image/jpeg")))

    assert _SEEN_KEY not in fake_redis.store
    assert stub_app.conversations.rejected_calls == []


async def test_transient_raise_then_redelivery_reprocesses(
    handler, stub_app, stub_media, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
):
    # First delivery: a transient fetch fault raises (sid unmarked). Twilio redelivers the same
    # MessageSid: dedupe does NOT short-circuit (unmarked), and the retry ingests and bridges.
    served = _ingested(media_id="J" * 43)
    stub_media.ingest_results.append(served)
    _patch_stream(
        monkeypatch,
        [],
        [MediaFetchError(host="cdn.example", status_code=503), _FakeStream(content_length=5)],
    )

    with pytest.raises(MediaFetchError):
        await handler(signed_request(_media_pairs(MediaUrl0=_MEDIA0, MediaContentType0="image/jpeg")))
    assert _SEEN_KEY not in fake_redis.store

    result = await handler(signed_request(_media_pairs(MediaUrl0=_MEDIA0, MediaContentType0="image/jpeg")))

    assert result.status_code == 204
    assert len(stub_app.conversations.accept_calls) == 1
    assert _SEEN_KEY in fake_redis.store


async def test_permanent_reject_notice_not_repeated_when_a_sibling_item_faults_transiently(
    handler, stub_app, stub_media, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
):
    # NumMedia=2, blank Body: item 0 is permanently over-cap, item 1 faults transiently on the
    # first pass (the door raises, the sid stays unmarked). Twilio redelivers the same sid: item
    # 0's rejection — already delivered, marked under its own per-item seen key — is NOT sent
    # again, and item 1 now bridges cleanly. Exactly ONE notice across both passes, item 1 once.
    served = _ingested(media_id="L" * 43, mime="image/png", size=5)
    stub_media.ingest_results.extend(
        [MediaTooLargeError("over the image cap"), MediaTooLargeError("over the image cap"), served]
    )
    _patch_stream(
        monkeypatch,
        [],
        [
            _FakeStream(content_length=10_000_000),
            MediaFetchError(host="cdn.example", status_code=503),
            _FakeStream(content_length=10_000_000),
            _FakeStream(content_length=5),
        ],
    )
    pairs = _media_pairs(
        num=2,
        MediaUrl0=_MEDIA0,
        MediaContentType0="image/jpeg",
        MediaUrl1=_MEDIA1,
        MediaContentType1="image/png",
    )

    with pytest.raises(MediaFetchError):
        await handler(signed_request(pairs))
    assert _SEEN_KEY not in fake_redis.store  # sid unmarked -> Twilio redelivers
    assert len(stub_app.conversations.rejected_calls) == 1  # item 0 rejected once on the first pass
    assert stub_app.conversations.rejected_calls[0]["reason"] is InboundRejectionReason.TOO_LARGE

    result = await handler(signed_request(pairs))

    assert result.status_code == 204
    assert len(stub_app.conversations.rejected_calls) == 1  # NOT repeated on the redelivery
    (accept,) = stub_app.conversations.accept_calls
    assert accept["provider_message_id"] == "SM777-1"  # item 1 bridged once
    assert accept["attachments"] == [served.item]
    assert _SEEN_KEY in fake_redis.store


async def test_mms_redelivery_deduped_no_second_fetch(
    handler, stub_app, stub_media, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
):
    # A successful MMS marks the sid seen; a redelivery of the same MessageSid short-circuits on
    # dedupe before any fetch/ingest/accept.
    stub_media.ingest_results.append(_ingested(media_id="K" * 43))
    _patch_stream(monkeypatch, [], [_FakeStream(content_length=5)])

    first = await handler(signed_request(_media_pairs(MediaUrl0=_MEDIA0, MediaContentType0="image/jpeg")))
    second = await handler(signed_request(_media_pairs(MediaUrl0=_MEDIA0, MediaContentType0="image/jpeg")))

    assert first.status_code == 204
    assert second.status_code == 204
    assert len(stub_media.ingest_calls) == 1
    assert len(stub_app.conversations.accept_calls) == 1


async def test_no_body_no_media_is_legit_ack_drop(
    handler, stub_app, fake_redis: FakeRedis, caplog: pytest.LogCaptureFixture
):
    # NumMedia=0 and an empty Body: the text path calls accept(text=""), the seam raises
    # BlankInboundTextError, and the bridge logs and success-acks (no notify, no turn, no retry).
    stub_app.conversations.accept_error = BlankInboundTextError("inbound text is blank")

    with caplog.at_level("WARNING"):
        result = await handler(signed_request(_media_pairs(body="", num=0)))

    assert result.status_code == 204
    assert stub_app.conversations.rejected_calls == []
    assert any("blank" in record.message for record in caplog.records)
    assert _SEEN_KEY in fake_redis.store


async def test_sms_text_only_num_media_zero_bridges_single_text_turn(handler, stub_app, fake_redis: FakeRedis):
    # NumMedia=0 is the unchanged text path: one turn under the bare sid, the Body verbatim,
    # no media_* params.
    pairs = [
        ("MessageSid", "SM777"),
        ("To", _TWILIO),
        ("From", _HUMAN),
        ("Body", "ship it"),
        ("NumMedia", "0"),
    ]

    result = await handler(signed_request(pairs))

    assert result.status_code == 204
    assert stub_app.conversations.accept_calls == [
        {
            "channel": "twilio",
            "our_identity": _TWILIO,
            "client_address": _HUMAN,
            "cap_key": _HUMAN,
            "text": "ship it",
            "provider_message_id": "SM777",
            "params": None,
            "attachments": None,
        }
    ]
    assert _SEEN_KEY in fake_redis.store


async def test_non_integer_num_media_propagates_and_makes_no_turn(handler, stub_app, fake_redis: FakeRedis):
    # A present-but-non-integer NumMedia is a Twilio protocol violation: _media_count raises
    # ValueError, which the door propagates (surfaced as a 5xx) rather than treating the
    # delivery as media-less; no turn is accepted and the sid is not marked seen.
    pairs = [
        ("MessageSid", "SM777"),
        ("To", _TWILIO),
        ("From", _HUMAN),
        ("Body", "ship it"),
        ("NumMedia", "x"),
    ]

    with pytest.raises(ValueError, match="invalid literal for int"):
        await handler(signed_request(pairs))

    assert stub_app.conversations.accept_calls == []
    assert _SEEN_KEY not in fake_redis.store


async def test_pending_question_resolves_before_bridge(handler, stub_app, channels, fake_redis: FakeRedis):
    # a correlated pending question resolves the ask via the ladder and never reaches
    # the caller's fresh-turn bridge.
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED

    result = await handler(signed_request(_pairs()))

    assert result.status_code == 204
    assert len(channels.inbound_calls) == 1
    assert stub_app.conversations.accept_calls == []  # the caller's bridge was NOT reached


async def test_signature_failure_short_circuits_before_bridge(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # Regression: a bad signature is rejected before any correlation or bridge code.
    result = await handler(signed_request(_pairs(), token="other-token"))

    assert result.status_code == 401
    assert stub_app.conversations.accept_calls == []
    assert _SEEN_KEY not in fake_redis.store


async def test_dedupe_hit_short_circuits_before_bridge(handler, stub_app, channels, fake_redis: FakeRedis):
    # Regression: an already-seen MessageSid short-circuits before the ladder and bridge.
    fake_redis.store[_SEEN_KEY] = "1"

    result = await handler(signed_request(_pairs()))

    assert result.status_code == 204
    assert channels.inbound_calls == []  # the ladder was never consulted
    assert stub_app.conversations.accept_calls == []


async def test_bridge_overflow_propagates_and_does_not_dedupe(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A retryable/infrastructure failure from accept() (not a LookupError) propagates
    # as a 5xx so Twilio redelivers; the sid is NOT marked seen.
    stub_app.conversations.accept_error = RuntimeError("per-thread FIFO is full")

    with pytest.raises(RuntimeError, match="FIFO is full"):
        await handler(signed_request(_pairs()))

    assert _SEEN_KEY not in fake_redis.store


async def test_ladder_forward_error_raises_and_does_not_dedupe(handler, channels, fake_redis: FakeRedis):
    # A door 5xx / 401 / transport fault surfaces as AnswerForwardError from the ladder
    # (which keeps the correlation for the retry); the plugin lets it propagate and does
    # NOT mark the sid seen, so Twilio's redelivery re-runs the ladder.
    channels.inbound_error = AnswerForwardError("interactions callback rejected the answer: HTTP 500: oops")

    with pytest.raises(AnswerForwardError, match="HTTP 500"):
        await handler(signed_request(_pairs()))

    assert _SEEN_KEY not in fake_redis.store  # NOT marked seen — the retry must not dedupe away


async def test_ladder_bridged_outcome_acks_and_marks_seen(handler, channels, fake_redis: FakeRedis):
    # A BRIDGED outcome (the ask is gone / a hard mismatch — the ladder already bridged
    # the reply internally, carrying the Body verbatim under the MessageSid dedupe key)
    # acks 204 and marks the sid seen so a redelivery is not re-processed.
    channels.inbound_outcome = InboundAnswerOutcome.BRIDGED

    result = await handler(signed_request(_pairs(Body="ship it")))

    assert result.status_code == 204
    assert len(channels.inbound_calls) == 1
    assert channels.inbound_calls[0].bridge.bridge_text == "ship it"
    assert channels.inbound_calls[0].bridge.provider_message_id == "SM777"
    assert _SEEN_KEY in fake_redis.store

    # Twilio redelivers the same MessageSid: dedupe short-circuits — the ladder is not
    # consulted a second time.
    redelivery = await handler(signed_request(_pairs(Body="ship it")))
    assert redelivery.status_code == 204
    assert len(channels.inbound_calls) == 1  # still once


async def test_ladder_retry_kept_outcome_acks_and_marks_seen(handler, channels, fake_redis: FakeRedis):
    # A RETRY_KEPT outcome (the door rejected a re-answerable ask; the ladder kept the
    # correlation and told the participant what's expected) acks 204 and marks the sid seen —
    # redelivering the same body would be rejected again.
    channels.inbound_outcome = InboundAnswerOutcome.RETRY_KEPT

    result = await handler(signed_request(_pairs()))

    assert result.status_code == 204
    assert len(channels.inbound_calls) == 1
    assert _SEEN_KEY in fake_redis.store


# --- Signed delivery-status route ---------------------------------------------

_STATUS_PATH = "/api/channels/twilio/status"
_STATUS_ROUTE = "/status"


@pytest.fixture
def status_handler(stub_app) -> Callable[..., Awaitable[Response]]:
    routes = [route for route in stub_app.http.routes if route.path == _STATUS_ROUTE]
    assert len(routes) == 1
    route = routes[0]
    assert route.methods == ["POST"]
    assert route.authed is None
    return route.handler


def _status_pairs(status: str, sid: str = "SM777") -> list[tuple[str, str]]:
    return [("MessageSid", sid), ("MessageStatus", status)]


@pytest.mark.parametrize("status", ["failed", "undelivered"])
async def test_status_failed_records_failed(status_handler, stub_app, status: str):
    result = await status_handler(signed_request(_status_pairs(status), path=_STATUS_PATH))

    assert result.status_code == 204
    assert stub_app.conversations.status_calls == [
        {"channel": "twilio", "provider_message_id": "SM777", "status": DeliveryReceipt.FAILED}
    ]


async def test_status_delivered_records_delivered(status_handler, stub_app):
    result = await status_handler(signed_request(_status_pairs("delivered"), path=_STATUS_PATH))

    assert result.status_code == 204
    assert stub_app.conversations.status_calls == [
        {"channel": "twilio", "provider_message_id": "SM777", "status": DeliveryReceipt.DELIVERED}
    ]


@pytest.mark.parametrize("status", ["queued", "sent", "sending"])
async def test_status_intermediate_is_benign_noop(status_handler, stub_app, status: str):
    result = await status_handler(signed_request(_status_pairs(status), path=_STATUS_PATH))

    assert result.status_code == 204
    assert stub_app.conversations.status_calls == []  # nothing recorded for a non-terminal status


async def test_status_unknown_sid_is_benign_204(status_handler, stub_app, caplog: pytest.LogCaptureFixture):
    # record_delivery_status raises LookupError for a sid the bridge does not track;
    # the route acks rather than 5xx-ing the provider for a message we do not own.
    stub_app.conversations.status_error = LookupError("outbound id SM777 maps to no answer record")

    with caplog.at_level("INFO"):
        result = await status_handler(signed_request(_status_pairs("failed"), path=_STATUS_PATH))

    assert result.status_code == 204
    assert any("untracked message SM777" in record.message for record in caplog.records)


async def test_status_send_hit_posts_receipt_no_untracked_log(
    status_handler, stub_app, caplog: pytest.LogCaptureFixture
):
    # The bridge does not own the sid (LookupError), but it is a notify_user send: the
    # send-receipt seam resolves it and posts the receipt onto the trace, so NO untracked log.
    stub_app.conversations.status_error = LookupError("outbound id SM777 maps to no answer record")
    stub_app.channels.send_receipt_result = True

    with caplog.at_level("INFO"):
        result = await status_handler(signed_request(_status_pairs("delivered"), path=_STATUS_PATH))

    assert result.status_code == 204
    (call,) = stub_app.channels.send_receipt_calls
    assert call["channel"] == "twilio"
    assert call["provider_message_id"] == "SM777"
    assert call["status"] is DeliveryReceipt.DELIVERED
    assert not any("untracked message SM777" in record.message for record in caplog.records)


async def test_receipt_while_pending_delivery_is_acked_not_5xx(status_handler, stub_app):
    # A status callback whose record is still pending_delivery (the send loop published the
    # outbound reverse index but has not reached mark_provisional): the seam PARKS the receipt
    # and returns without raising, so the door acks 204 — never a 5xx that would have Twilio
    # redeliver. The receipt is asserted through the record: its pending_receipt is staged to
    # the terminal target while it stays pending_delivery.
    stub_app.conversations.records["SM777"] = SimpleNamespace(delivery_status="pending_delivery", pending_receipt=None)

    result = await status_handler(signed_request(_status_pairs("delivered"), path=_STATUS_PATH))

    assert result.status_code == 204
    record = stub_app.conversations.records["SM777"]
    assert record.delivery_status == "pending_delivery"
    assert record.pending_receipt == "delivered"


async def test_receipt_after_provisional_applies_terminal(status_handler, stub_app):
    # The same door with a record that already reached provisional: the seam applies the
    # terminal in place (the existing handling) and the door still acks 204.
    stub_app.conversations.records["SM777"] = SimpleNamespace(delivery_status="provisional", pending_receipt=None)

    result = await status_handler(signed_request(_status_pairs("delivered"), path=_STATUS_PATH))

    assert result.status_code == 204
    record = stub_app.conversations.records["SM777"]
    assert record.delivery_status == "delivered"
    assert record.pending_receipt is None


async def test_receipt_conflicting_terminal_is_acked(status_handler, stub_app):
    # A receipt that conflicts with an already-terminal record is a benign no-op the seam logs
    # and returns from; the door still acks 204 and the record is left as it stands.
    stub_app.conversations.records["SM777"] = SimpleNamespace(delivery_status="delivered", pending_receipt=None)

    result = await status_handler(signed_request(_status_pairs("failed"), path=_STATUS_PATH))

    assert result.status_code == 204
    assert stub_app.conversations.records["SM777"].delivery_status == "delivered"


async def test_status_bad_signature_is_401(status_handler, stub_app):
    result = await status_handler(signed_request(_status_pairs("failed"), path=_STATUS_PATH, token="other-token"))

    assert result.status_code == 401
    assert stub_app.conversations.status_calls == []


async def test_status_missing_sid_is_400(status_handler, stub_app):
    result = await status_handler(signed_request([("MessageStatus", "delivered")], path=_STATUS_PATH))

    assert result.status_code == 400
    assert stub_app.conversations.status_calls == []


async def test_status_missing_status_is_400(status_handler, stub_app):
    result = await status_handler(signed_request([("MessageSid", "SM777")], path=_STATUS_PATH))

    assert result.status_code == 400
    assert stub_app.conversations.status_calls == []
