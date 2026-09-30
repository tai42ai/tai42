"""Inbound media that cannot be ingested is rejected with a reason-typed notice + event — never dropped.

Every media-ingest failure a channel can hit maps to ONE reason and, when permanent, one participant
notice + one ``conversations_inbound_rejected`` event + a 2xx ack (so the vendor stops redelivering):
- TOO_LARGE — a body over the per-kind cap (telegram photo above ``MEDIA_INGEST_MAX_IMAGE_BYTES``);
- UNSUPPORTED_TYPE — a body that does not sniff to the declared kind (whatsapp image whose bytes are
  HTML);
- COULD_NOT_RECEIVE — a permanent vendor miss (twilio MMS whose media 404s), and a store-unavailable
  outcome (no blob provider registered) — both reject with the generic could-not-receive notice.

A TRANSIENT fetch fault (a 5xx at the byte stream) instead RAISES so the webhook returns non-2xx and
the vendor redelivers; the redelivery (the fake healthy on the retry) then ingests, and a further
redelivery of the now-accepted message is deduped — no duplicate turn.

Driven over the live media-bridge stack (all four channels + storage-local + the hooks manager);
a dedicated no-provider stack drives the store-unavailable leg. The channels mock over their
in-process stubs, so any real selection breaks the stubs and the module steps aside.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from tai42_e2e.channel_stubs import HTML_BODY, TINY_PNG, MediaBlob
from tai42_e2e.manifests import (
    BRIDGE_MEDIA_INGEST_MAX_IMAGE_BYTES,
    BRIDGE_TWILIO_CLIENT,
    BRIDGE_TWILIO_FROM,
    BRIDGE_WHATSAPP_CLIENT,
    BRIDGE_WHATSAPP_PHONE_ID,
)
from tai42_e2e.provider_stub import SignedInbound
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.waiting import wait_for_async

from ._bridge_support import (
    TELEGRAM_INBOUND_PATH,
    TWILIO_INBOUND_PATH,
    WHATSAPP_INBOUND_PATH,
    BridgeHarness,
    post_inbound,
    wait_channel_send_count,
    wait_probe_record,
)

pytestmark = [
    pytest.mark.needs(
        "kind:identity", "probe-tools", "setting:conversations", "setting:seeded-access-control", "store:redis"
    ),
    pytest.mark.skipif(
        any(HarnessSettings().is_real(seam) for seam in ("telegram", "slack", "twilio", "whatsapp", "llm")),
        reason="the media-bridge stubs are the mock leg; real legs run on the creds host",
    ),
]

# The one fixed participant notice per rejection reason (mirrors the skeleton chokepoint's copy).
_NOTICES = {
    "too_large": "That file is too large to accept here.",
    "unsupported_type": "This content type is not supported here.",
    "could_not_receive": "That attachment could not be received.",
}
_REJECTED_TOPIC = "conversations_inbound_rejected"


async def _register_reject_hook(
    bridge: BridgeHarness, uniq: Callable[[str], str], *, probe: str, recipient: str
) -> None:
    """Register a hook recording ``reason`` for a ``conversations_inbound_rejected`` event whose
    ``client_address`` is this test's recipient — so a concurrent rejection never lands in the probe."""
    exec_key = uniq("mrej-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    await bridge.api().post(
        "/api/hooks",
        json={
            "name": uniq("mrej-hook").replace("_", "-"),
            "topic": _REJECTED_TOPIC,
            "tool": "e2e_record",
            "start_expr": {
                "content": f'if .client_address == "{recipient}" then {{key: "{probe}", value: .reason}} else null end'
            },
            "execution_key": exec_key,
        },
    )


def _notices_to(fake: Any, reason: str, recipient: str, recipient_field: str) -> list[dict]:
    """Recorded sends carrying the ``reason`` notice copy addressed to ``recipient`` (scoped so a
    stale send from another test on the session-shared stub is never counted)."""
    return [record for record in fake.sends_matching(_NOTICES[reason]) if record.get(recipient_field) == recipient]


async def _assert_rejected(
    bridge: BridgeHarness,
    uniq: Callable[[str], str],
    *,
    inbound_path: str,
    inbound: SignedInbound,
    fake: Any,
    recipient: str,
    recipient_field: str,
    reason: str,
) -> None:
    """Post a permanently-failing media inbound; assert the 2xx ack + one reason notice + one event."""
    probe = uniq("mrej-probe")
    await _register_reject_hook(bridge, uniq, probe=probe, recipient=recipient)

    resp = await post_inbound(bridge.stack, inbound_path, inbound, port=bridge.stack.port_b)
    assert resp.status_code in (200, 204), resp.text

    async def _notice_landed() -> list[dict] | None:
        got = _notices_to(fake, reason, recipient, recipient_field)
        return got if got else None

    notices = await wait_for_async(_notice_landed, deadline=12.0, message=f"the {reason} notice never reached the stub")
    assert len(notices) == 1, f"expected exactly one {reason} notice, saw {notices!r}"

    async def _event_recorded() -> list[str] | None:
        rows = [json.loads(raw)["value"] for raw in bridge.stack.records(probe)]
        return rows if rows else None

    reasons = await wait_for_async(_event_recorded, deadline=12.0, message="the rejection event never fired the hook")
    assert reasons == [reason], f"expected one {reason} event, saw {reasons!r}"


@pytest.mark.needs("kind:channels:telegram", "helper:telegram", "setting:MEDIA_INGEST_MAX_IMAGE_BYTES=8192")
async def test_telegram_oversize_image_rejected_too_large(
    media_bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    chat_id = "940001"
    file_id = uniq("tg-big")
    # A body over the image cap — the seam rejects it TOO_LARGE (declared/streamed size over cap).
    media_bridge.telegram.media[file_id] = MediaBlob(
        body=TINY_PNG + b"\x00" * (BRIDGE_MEDIA_INGEST_MAX_IMAGE_BYTES + 1024), content_type="image/png"
    )
    inbound = media_bridge.telegram_inbound_photo(chat_id=chat_id, file_id=file_id)
    await _assert_rejected(
        media_bridge,
        uniq,
        inbound_path=TELEGRAM_INBOUND_PATH,
        inbound=inbound,
        fake=media_bridge.fake_telegram,
        recipient=chat_id,
        recipient_field="chat_id",
        reason="too_large",
    )


@pytest.mark.needs("kind:channels:whatsapp", "helper:whatsapp")
async def test_whatsapp_html_under_image_rejected_unsupported_type(
    media_bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    media_id = uniq("wa-html")
    # The metadata declares an image, but the lookaside bytes are HTML — no positive image sniff, so
    # the seam rejects UNSUPPORTED_TYPE (active content is on no allowlist).
    media_bridge.fake_whatsapp.media_meta[media_id] = {"mime_type": "image/png", "file_size": len(HTML_BODY)}
    media_bridge.fake_whatsapp.media[media_id] = MediaBlob(body=HTML_BODY, content_type="text/html")
    inbound = media_bridge.whatsapp_inbound_image(
        phone_number_id=BRIDGE_WHATSAPP_PHONE_ID,
        wa_id=BRIDGE_WHATSAPP_CLIENT,
        media_id=media_id,
        mime_type="image/png",
    )
    await _assert_rejected(
        media_bridge,
        uniq,
        inbound_path=WHATSAPP_INBOUND_PATH,
        inbound=inbound,
        fake=media_bridge.fake_whatsapp,
        recipient=BRIDGE_WHATSAPP_CLIENT,
        recipient_field="to",
        reason="unsupported_type",
    )


@pytest.mark.needs("kind:channels:twilio", "helper:twilio")
async def test_twilio_permanent_miss_rejected_could_not_receive(
    media_bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    ref = uniq("tw-404")
    # The CDN answers 404 — a permanent vendor miss (a 4xx at open), rejected COULD_NOT_RECEIVE.
    media_bridge.fake_twilio.media[ref] = MediaBlob(status=404)
    inbound = media_bridge.twilio_inbound_mms(
        our_identity=BRIDGE_TWILIO_FROM,
        client=BRIDGE_TWILIO_CLIENT,
        media_url=media_bridge.fake_twilio.media_url(ref),
        content_type="image/jpeg",
    )
    await _assert_rejected(
        media_bridge,
        uniq,
        inbound_path=TWILIO_INBOUND_PATH,
        inbound=inbound,
        fake=media_bridge.fake_twilio,
        recipient=BRIDGE_TWILIO_CLIENT,
        recipient_field="to",
        reason="could_not_receive",
    )


@pytest.mark.needs("kind:channels:telegram", "helper:telegram", "setting:storage-absent")
async def test_telegram_store_unavailable_rejected_could_not_receive(
    media_bridge_no_store: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    """With NO blob provider registered, a healthy fetch still fails at the store: the seam raises
    ``MediaStoreUnavailableError`` and the channel rejects COULD_NOT_RECEIVE."""
    chat_id = "950001"
    file_id = uniq("tg-nostore")
    media_bridge_no_store.telegram.media[file_id] = MediaBlob(body=TINY_PNG, content_type="image/png")
    inbound = media_bridge_no_store.telegram_inbound_photo(chat_id=chat_id, file_id=file_id)
    await _assert_rejected(
        media_bridge_no_store,
        uniq,
        inbound_path=TELEGRAM_INBOUND_PATH,
        inbound=inbound,
        fake=media_bridge_no_store.fake_telegram,
        recipient=chat_id,
        recipient_field="chat_id",
        reason="could_not_receive",
    )


@pytest.mark.needs("kind:channels:telegram", "kind:storage", "helper:telegram")
async def test_telegram_transient_5xx_redelivers_then_dedupes(
    media_bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    """A transient 5xx at the byte stream RAISES (non-2xx webhook → the vendor redelivers); the
    redelivery — the fake healthy on the retry — ingests and bridges ONE turn, and a further
    redelivery of the accepted update is deduped (no second turn)."""
    chat_id = "960001"
    file_id = uniq("tg-5xx")
    caption = uniq("tg-5xx-cap")
    update_id = 770001
    # Fail the first byte fetch with a 5xx, serve the healthy PNG on every fetch thereafter.
    media_bridge.telegram.media[file_id] = MediaBlob(body=TINY_PNG, content_type="image/png", fail_times=1)

    exec_key = uniq("tg5-exec")
    await media_bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    probe = uniq("tg5-probe")
    reply_marker = uniq("tg5-reply")
    await media_bridge.create_tool_channel_route(
        route_name=uniq("tg5-route").replace("_", "-"),
        tool="e2e_record",
        execution_key=exec_key,
        channel="telegram",
        our_identity=media_bridge.telegram_our_identity,
        start_expr=f'{{key: "{probe}", value: .message}}',
        reply_expr=f'"{reply_marker}"',
    )

    def _inbound() -> SignedInbound:
        return media_bridge.telegram_inbound_photo(
            chat_id=chat_id, file_id=file_id, caption=caption, update_id=update_id
        )

    # Delivery #1: the 5xx makes the fetch transient → the webhook returns non-2xx (redeliver).
    resp1 = await post_inbound(media_bridge.stack, TELEGRAM_INBOUND_PATH, _inbound(), port=media_bridge.stack.port_b)
    assert resp1.status_code not in (200, 204), resp1.status_code

    # Delivery #2 (the redelivery, same update_id): the fake is healthy now → ingest + one bridged turn.
    resp2 = await post_inbound(media_bridge.stack, TELEGRAM_INBOUND_PATH, _inbound(), port=media_bridge.stack.port_b)
    assert resp2.status_code in (200, 204), resp2.text
    recorded = await wait_probe_record(media_bridge, probe)
    assert [entry["value"] for entry in recorded] == [caption], recorded
    await wait_channel_send_count(media_bridge.fake_telegram, reply_marker, 1)

    # Delivery #3 (a further redelivery of the accepted update): deduped at accept — no second turn.
    resp3 = await post_inbound(media_bridge.stack, TELEGRAM_INBOUND_PATH, _inbound(), port=media_bridge.stack.port_b)
    assert resp3.status_code in (200, 204), resp3.text
    assert [json.loads(raw)["value"] for raw in media_bridge.stack.records(probe)] == [caption]
