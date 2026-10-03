"""The web visitor's inbound-media round trip (no vendor — the plugin's own upload + message doors).

A visitor uploads a file AHEAD of the send: the upload door streams it through the ONE
``app.media.ingest_media`` chokepoint as PENDING (``message_id=None``) and returns a same-origin
served reference whose bytes 404 while pending — a capability id must not leak bytes before the send
binds it. The message send then references the served id in ``attachment_ids``; the door binds each
id to the owning message BEFORE ``accept`` and the bridged turn carries BOTH a typed ``attachments``
MediaItem whose ``url`` is the served reference AND the parity ``media_*`` entry-params, while the
visitor's OWN transcript frame carries the ``media`` items (a caption-less send renders media-only).
A GET of the served url then returns the exact bytes with the sniffed ``Content-Type``,
``Content-Security-Policy: sandbox`` and ``X-Content-Type-Options: nosniff``; a document downloads as
an attachment. Rejections are loud typed refusals; an unreferenced upload expires past its pending
TTL, after which its bytes stay 404 and a bind refuses.

Driven end to end over the live web-media stack (the web channel's public doors + the redis
conversations backend a bridged turn accepts into + storage-local as the blob provider + a real
tool-target turn recording the payload it saw). The turn target is a tool dispatched by expression,
so no LLM is involved.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable
from typing import cast

import pytest
import redis as redis_lib
from tai42_contract.conversations import inbound_media_placeholder

from tai42_e2e.channel_stubs import TINY_PDF, TINY_PNG
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.waiting import wait_for_async
from tai42_e2e.webchat import WebChatClient

from ._bridge_support import MEDIA_REF_RE, BridgeHarness, wait_probe_record

# The web-media stack boots twilio + whatsapp on their in-process stubs and scripts no LLM; a real
# selection breaks the stubs, so the module steps aside on the creds host.
pytestmark = [
    pytest.mark.needs(
        "kind:channels:web",
        "kind:identity",
        "probe-tools",
        "setting:conversations",
        "setting:seeded-access-control",
        "store:redis",
    ),
    pytest.mark.skipif(
        any(HarnessSettings().is_real(seam) for seam in ("twilio", "whatsapp", "llm")),
        reason="the web-media stubs are the mock leg; real legs run on the creds host",
    ),
]

# The tool payload the bridged turn dispatches, recorded as one JSON string: the turn text, the
# parity ``media_*`` params, and the typed attachment's url/kind/filename — the whole ingest result
# the tool saw, keyed on the SERVED id.
_PAYLOAD_EXPR = (
    '{{key: "{probe}", value: ('
    "{{m: .message, "
    'i: (.params.media_id // ""), '
    'k: (.params.media_kind // ""), '
    'mm: (.params.media_mime_type // ""), '
    'sha: (.params.media_sha256 // ""), '
    'sz: (.params.media_size // ""), '
    'fn: (.params.media_filename // ""), '
    'au: (.attachments[0].url // ""), '
    'ak: (.attachments[0].kind // ""), '
    'afn: (.attachments[0].filename // "")}} | tostring)}}'
)


async def _create_tool_web_route(web_media: BridgeHarness, uniq: Callable[[str], str], tag: str) -> tuple[str, str]:
    """Create a tool-target web conversation route recording its turn payload; return
    ``(identity, probe_key)``."""
    identity = uniq(f"{tag}-site").replace("_", "-")
    route_name = uniq(f"{tag}-route").replace("_", "-")
    exec_key = uniq(f"{tag}-exec")
    probe = uniq(f"{tag}-probe")
    await web_media.mint_key(user_id=exec_key, scopes=["e2e-all"])
    await web_media.create_tool_channel_route(
        route_name=route_name,
        tool="e2e_record",
        execution_key=exec_key,
        channel="web",
        our_identity=identity,
        start_expr=_PAYLOAD_EXPR.format(probe=probe),
        reply_expr='"ok"',
    )
    return identity, probe


async def _open_visitor(web_media: BridgeHarness, identity: str) -> WebChatClient:
    """Open the chat page as a first-time visitor and adopt the minted, registered session."""
    base_url = web_media.stack.origin(web_media.stack.port_b)
    web, page = await WebChatClient.open_page(base_url, identity, store_url=web_media.stack.resources.redis_url)
    assert page.status_code == 200, page.text
    return web


def _inbound_media_frame(frames: list[tuple[str, dict]]) -> dict | None:
    """The visitor's own ``chat.message`` frame carrying ``media``, or ``None`` when absent."""
    return next(
        (data for event, data in frames if event == "chat.message" and data["direction"] == "in" and "media" in data),
        None,
    )


def _cid() -> str:
    """A retry key in the door's shape (8-64 chars of ``[A-Za-z0-9_-]``)."""
    return secrets.token_urlsafe(16)


def _expiry_score(web_media: BridgeHarness, media_id: str) -> float | None:
    """The durable pending-horizon score for ``media_id`` (epoch seconds), or ``None`` once the
    reaper has reclaimed the record — the same expiry index the seam's bind check reads."""
    prefix = web_media.stack.config.env["CONVERSATIONS_PREFIX"]
    client = redis_lib.Redis.from_url(web_media.stack.resources.redis_url, decode_responses=True)
    try:
        # redis-py types every command as the sync/async union; this client is sync.
        score = cast("float | None", client.zscore(f"{prefix}:media-meta:expiry", media_id))
    finally:
        client.close()
    return score


@pytest.mark.needs("kind:storage")
async def test_upload_returns_pending_served_ref_that_404s_before_the_send_binds_it(
    web_media: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    identity, _probe = await _create_tool_web_route(web_media, uniq, "up")
    web = await _open_visitor(web_media, identity)

    accepted = await web.upload(TINY_PNG, "photo.png", "image/png")
    assert accepted.status_code == 200, accepted.text
    data = accepted.json()["data"]
    assert data["kind"] == "image"
    assert data["mime"] == "image/png"
    assert data["size"] == len(TINY_PNG)
    # An image carries no download filename (only a document does).
    assert data["filename"] is None
    url = data["url"]
    assert MEDIA_REF_RE.match(url), url
    assert url == f"/api/interactions/media/{data['media_id']}", data

    # The bytes 404 while the item is PENDING — the capability id leaks nothing before the send binds it.
    pending = await web.get_media(url)
    assert pending.status_code == 404, pending.text


@pytest.mark.needs("kind:storage")
async def test_blank_text_send_binds_the_attachment_and_renders_media_only(
    web_media: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    identity, probe = await _create_tool_web_route(web_media, uniq, "blank")
    web = await _open_visitor(web_media, identity)

    up = (await web.upload(TINY_PNG, "photo.png", "image/png")).json()["data"]
    media_id, url = up["media_id"], up["url"]

    sent = await web.send("", attachment_ids=[media_id])
    assert sent.status_code == 200, sent.text

    # The visitor's OWN frame keeps the raw (blank) text and carries the media — a media-only bubble.
    # A fresh SSE connect replays it, so this doubles as the replay assertion.
    replay = await web.frames()
    frame = _inbound_media_frame(replay)
    assert frame is not None, replay
    assert frame["text"] == ""
    assert frame["media"] == [{"kind": "image", "url": url}], frame

    # The bridged turn carries the typed attachment AND the parity params, both keyed on the SERVED id;
    # the turn text is the media placeholder (accept refuses blank text).
    recorded = await wait_probe_record(web_media, probe)
    assert len(recorded) == 1, recorded
    seen = json.loads(recorded[0]["value"])
    assert seen["au"] == url, seen
    assert seen["i"] == media_id, seen
    assert seen["ak"] == "image", seen
    assert seen["k"] == "image", seen
    assert seen["mm"] == "image/png", seen
    assert seen["sz"] == str(len(TINY_PNG)), seen
    assert seen["m"] == inbound_media_placeholder("image"), seen

    # The served bytes now return, with the sniffed mime and the inbound security headers; an image
    # stays inline (no attachment disposition).
    served = await web.get_media(url)
    assert served.status_code == 200, served.text
    assert served.content == TINY_PNG
    assert served.headers["content-type"] == "image/png", served.headers
    assert served.headers["content-security-policy"] == "sandbox", served.headers
    assert served.headers["x-content-type-options"] == "nosniff", served.headers
    assert served.headers.get("content-disposition") is None, served.headers


@pytest.mark.needs("kind:storage")
async def test_captioned_send_carries_the_caption_as_the_turn_text(
    web_media: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    identity, probe = await _create_tool_web_route(web_media, uniq, "cap")
    web = await _open_visitor(web_media, identity)

    caption = uniq("cap-text")
    up = (await web.upload(TINY_PNG, "photo.png", "image/png")).json()["data"]
    media_id, url = up["media_id"], up["url"]

    sent = await web.send(caption, attachment_ids=[media_id])
    assert sent.status_code == 200, sent.text

    frame = _inbound_media_frame(await web.frames())
    assert frame is not None
    assert frame["text"] == caption
    assert frame["media"] == [{"kind": "image", "url": url}], frame

    recorded = await wait_probe_record(web_media, probe)
    seen = json.loads(recorded[0]["value"])
    assert seen["m"] == caption, seen
    assert seen["au"] == url, seen
    assert seen["i"] == media_id, seen


@pytest.mark.needs("kind:storage")
async def test_document_send_serves_as_a_download_attachment(
    web_media: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    identity, _probe = await _create_tool_web_route(web_media, uniq, "doc")
    web = await _open_visitor(web_media, identity)

    up = (await web.upload(TINY_PDF, "document.pdf", "application/pdf")).json()["data"]
    assert up["kind"] == "document"
    assert up["filename"] == "document.pdf", up
    media_id, url = up["media_id"], up["url"]

    sent = await web.send("", attachment_ids=[media_id])
    assert sent.status_code == 200, sent.text

    frame = _inbound_media_frame(await web.frames())
    assert frame is not None
    assert frame["media"] == [{"kind": "document", "url": url, "filename": "document.pdf"}], frame

    served = await web.get_media(url)
    assert served.status_code == 200, served.text
    assert served.content == TINY_PDF
    assert served.headers["content-type"] == "application/pdf", served.headers
    disposition = served.headers.get("content-disposition")
    assert disposition is not None, served.headers
    assert disposition.startswith("attachment"), disposition
    assert "filename*=UTF-8''" in disposition, disposition
    assert "document.pdf" in disposition, disposition


@pytest.mark.needs("setting:MEDIA_INGEST_MAX_IMAGE_BYTES=8192")
async def test_a_body_over_the_route_bound_is_refused_by_the_body_limit(
    web_media: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    """The upload door declares its request-body bound = the seam's ``max_cap`` plus a multipart-framing
    allowance; a body well over it is refused by the body-limit backstop BEFORE the door, with the
    backstop's own 413 (no ``code``, naming the route's declared limit) — distinct from the seam's
    ``media_too_large``."""
    identity, _probe = await _create_tool_web_route(web_media, uniq, "toobig")
    web = await _open_visitor(web_media, identity)

    max_cap = int(web_media.stack.config.env["MEDIA_INGEST_MAX_VIDEO_BYTES"])
    over_bound = await web.upload(b"\x00" * (max_cap * 2), "big.bin", "application/octet-stream")
    assert over_bound.status_code == 413, over_bound.text
    body = over_bound.json()
    assert "declared limit" in body["error"], body
    assert "code" not in body, body


@pytest.mark.needs("kind:storage", "setting:MEDIA_INGEST_MAX_IMAGE_BYTES=8192")
async def test_an_over_image_cap_png_under_the_route_bound_is_refused_by_the_seam(
    web_media: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    """A PNG over the image cap but UNDER the route's body bound reaches the ingest seam, which refuses
    it mid-stream with 413 ``media_too_large`` (the derived kind's own tighter cap)."""
    identity, _probe = await _create_tool_web_route(web_media, uniq, "bigimg")
    web = await _open_visitor(web_media, identity)

    image_cap = int(web_media.stack.config.env["MEDIA_INGEST_MAX_IMAGE_BYTES"])
    oversized_png = TINY_PNG + b"\x00" * (image_cap + 512 - len(TINY_PNG))
    refused = await web.upload(oversized_png, "big.png", "image/png")
    assert refused.status_code == 413, refused.text
    assert refused.json()["code"] == "media_too_large", refused.text


@pytest.mark.needs("kind:storage")
async def test_an_html_body_declared_as_an_image_is_refused_as_a_disallowed_type(
    web_media: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    identity, _probe = await _create_tool_web_route(web_media, uniq, "html")
    web = await _open_visitor(web_media, identity)

    refused = await web.upload(b"<!DOCTYPE html><html><body>x</body></html>", "fake.png", "image/png")
    assert refused.status_code == 415, refused.text
    assert refused.json()["code"] == "media_type_not_allowed", refused.text


@pytest.mark.needs("setting:storage-absent")
async def test_upload_is_unavailable_without_a_blob_provider(
    web_media_no_store: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    identity, _probe = await _create_tool_web_route(web_media_no_store, uniq, "nostore")
    web = await _open_visitor(web_media_no_store, identity)

    refused = await web.upload(TINY_PNG, "photo.png", "image/png")
    assert refused.status_code == 503, refused.text
    assert refused.json()["code"] == "media_store_unavailable", refused.text


async def test_an_unknown_attachment_id_refuses_the_send_as_unbindable(
    web_media: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    identity, _probe = await _create_tool_web_route(web_media, uniq, "unk")
    web = await _open_visitor(web_media, identity)

    # A valid-shape 43-char id nothing minted — binds to nothing.
    unknown = secrets.token_urlsafe(32)
    refused = await web.send("hello", attachment_ids=[unknown])
    assert refused.status_code == 400, refused.text
    assert refused.json()["code"] == "media_unbindable", refused.text


@pytest.mark.needs("kind:storage")
async def test_the_same_attachment_in_a_second_message_is_already_bound(
    web_media: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    identity, _probe = await _create_tool_web_route(web_media, uniq, "reuse")
    web = await _open_visitor(web_media, identity)

    media_id = (await web.upload(TINY_PNG, "photo.png", "image/png")).json()["data"]["media_id"]
    first = await web.send("first", attachment_ids=[media_id], client_message_id=_cid())
    assert first.status_code == 200, first.text

    # A DIFFERENT message (a fresh retry key → a fresh provider message id) cannot re-use the id.
    second = await web.send("second", attachment_ids=[media_id], client_message_id=_cid())
    assert second.status_code == 409, second.text
    assert second.json()["code"] == "media_already_bound", second.text


@pytest.mark.needs("kind:storage")
async def test_a_retry_with_the_same_client_message_id_binds_idempotently_into_one_turn(
    web_media: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    identity, _probe = await _create_tool_web_route(web_media, uniq, "retry")
    web = await _open_visitor(web_media, identity)

    media_id = (await web.upload(TINY_PNG, "photo.png", "image/png")).json()["data"]["media_id"]
    cid = _cid()
    first = await web.send("only once", attachment_ids=[media_id], client_message_id=cid)
    assert first.status_code == 200, first.text
    # The SAME retry key re-derives the same provider message id, so the re-bind is idempotent and
    # resolves to the first attempt's turn rather than a second one.
    second = await web.send("only once", attachment_ids=[media_id], client_message_id=cid)
    assert second.status_code == 200, second.text
    turn_id = first.json()["data"]["message_id"]
    assert turn_id == second.json()["data"]["message_id"]

    # The retry re-appends under the SAME turn id (the page dedups by id on render), so every
    # media-carrying inbound frame in the replay shares that one id — one turn, not two.
    replay = await web.frames()
    inbound_ids = {
        data["id"]
        for event, data in replay
        if event == "chat.message" and data["direction"] == "in" and "media" in data
    }
    assert inbound_ids == {turn_id}, inbound_ids


@pytest.mark.needs(
    "kind:storage", "setting:MEDIA_INGEST_PENDING_TTL_SECONDS=3", "setting:MEDIA_INGEST_REAPER_INTERVAL_SECONDS=1"
)
async def test_an_unreferenced_upload_expires_after_the_pending_ttl(
    web_media_expiry: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    web_media = web_media_expiry
    identity, _probe = await _create_tool_web_route(web_media, uniq, "expire")
    web = await _open_visitor(web_media, identity)

    up = (await web.upload(TINY_PNG, "photo.png", "image/png")).json()["data"]
    media_id, url = up["media_id"], up["url"]
    assert (await web.get_media(url)).status_code == 404  # pending

    # Wait on the durable expiry index (not an ad-hoc sleep): the pending horizon lapses (or the
    # reaper reclaims the record), after which a bind of the id refuses.
    pending_ttl = int(web_media.stack.config.env["MEDIA_INGEST_PENDING_TTL_SECONDS"])
    reaper = int(web_media.stack.config.env["MEDIA_INGEST_REAPER_INTERVAL_SECONDS"])

    async def _lapsed() -> bool | None:
        score = _expiry_score(web_media, media_id)
        return True if (score is None or score < time.time()) else None

    await wait_for_async(
        _lapsed, deadline=pending_ttl + reaper + 10, message="the pending upload's horizon never lapsed"
    )

    # The bytes stay 404, and a send that would bind the lapsed id is refused as unbindable.
    assert (await web.get_media(url)).status_code == 404
    refused = await web.send("late", attachment_ids=[media_id])
    assert refused.status_code == 400, refused.text
    assert refused.json()["code"] == "media_unbindable", refused.text
