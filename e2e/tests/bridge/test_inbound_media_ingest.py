"""Inbound media is FETCHED from the vendor, ingested through the seam, and bridged as served media.

A participant sends media (a telegram photo, a slack file share, a twilio MMS document, a whatsapp
image) to the channel's signed inbound door. The channel fetches the vendor bytes, runs them through
the ONE ``app.media.ingest_media`` chokepoint (cap + sniff + store), and bridges ONE conversation
turn carrying BOTH a typed ``attachments`` MediaItem whose ``url`` is a served capability reference
(``/api/interactions/media/<id>``) AND the parity ``media_*`` entry-params — with ``media_id`` now the
SERVED id, not the raw vendor reference. A GET of the served url returns the exact bytes with the
sniffed ``Content-Type`` and ``Content-Security-Policy: sandbox``; a document additionally serves
``Content-Disposition: attachment``.

Driven end to end over the live media-bridge stack (all four channels + the redis conversations
backend + storage-local as the blob provider + a real turn): signed webhook → inbound decode → vendor
fetch → ``ingest_media`` → ``conversations.accept`` with the typed attachment → route → tool dispatch
→ the tool records what it received; then the served route hands back the stored bytes.

The scripted-LLM is not involved (a tool target dispatches deterministically); the four channels mock
over their in-process stubs, so any real selection breaks the stubs and the module steps aside.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable

import pytest

from tai42_e2e.channel_stubs import TINY_PDF, TINY_PNG, MediaBlob
from tai42_e2e.manifests import (
    BRIDGE_TWILIO_CLIENT,
    BRIDGE_TWILIO_FROM,
    BRIDGE_WHATSAPP_CLIENT,
    BRIDGE_WHATSAPP_PHONE_ID,
    SLACK_BOT_USER_ID,
)
from tai42_e2e.provider_stub import SignedInbound
from tai42_e2e.settings import HarnessSettings

from ._bridge_support import (
    MEDIA_REF_RE,
    SLACK_INBOUND_PATH,
    TELEGRAM_INBOUND_PATH,
    TWILIO_INBOUND_PATH,
    WHATSAPP_INBOUND_PATH,
    BridgeHarness,
    get_served_media,
    post_inbound,
    wait_channel_send_count,
    wait_probe_record,
)

pytestmark = [
    pytest.mark.needs(
        "kind:identity",
        "kind:storage",
        "probe-tools",
        "setting:conversations",
        "setting:seeded-access-control",
        "store:redis",
    ),
    pytest.mark.skipif(
        any(HarnessSettings().is_real(seam) for seam in ("telegram", "slack", "twilio", "whatsapp", "llm")),
        reason="the media-bridge stubs are the mock leg; real legs run on the creds host",
    ),
]

# The tool payload the bridged turn dispatches: the text, the parity ``media_*`` params, and the
# typed attachment's url/kind/filename — the whole ingest result the tool saw, as one JSON string.
_RECORD_EXPR = (
    '{{key: "{probe}", value: ('
    "{{m: .message, "
    'k: (.params.media_kind // ""), '
    'i: (.params.media_id // ""), '
    'mm: (.params.media_mime_type // ""), '
    'sha: (.params.media_sha256 // ""), '
    'sz: (.params.media_size // ""), '
    'fn: (.params.media_filename // ""), '
    'au: (.attachments[0].url // ""), '
    'ak: (.attachments[0].kind // ""), '
    'afn: (.attachments[0].filename // "")}} | tostring)}}'
)


async def _ingest_and_serve(
    bridge: BridgeHarness,
    uniq: Callable[[str], str],
    *,
    channel: str,
    our_identity: str,
    inbound: SignedInbound,
    inbound_path: str,
    fake: object,
    expected_text: str,
    expected_kind: str,
    served_bytes: bytes,
    served_mime: str,
    expected_filename: str,
    document: bool,
) -> None:
    """Route a media inbound to a tool target, assert the ONE turn's served attachment + parity
    params, then GET the served reference and assert the stored bytes + security headers."""
    exec_key = uniq("ingest-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    probe = uniq("ingest-probe")
    reply_marker = uniq("ingest-reply")
    await bridge.create_tool_channel_route(
        route_name=uniq("ingest-route").replace("_", "-"),
        tool="e2e_record",
        execution_key=exec_key,
        channel=channel,
        our_identity=our_identity,
        start_expr=_RECORD_EXPR.format(probe=probe),
        reply_expr=f'"{reply_marker}"',
    )

    resp = await post_inbound(bridge.stack, inbound_path, inbound, port=bridge.stack.port_b)
    assert resp.status_code in (200, 204), resp.text

    recorded = await wait_probe_record(bridge, probe)
    assert len(recorded) == 1, f"expected exactly one bridged turn, saw {recorded!r}"
    seen = json.loads(recorded[0]["value"])

    # The bridged turn carries the served attachment AND the parity params, both keyed on the SERVED
    # id (a 43-char capability ref), never the raw vendor reference.
    served_url = seen["au"]
    assert MEDIA_REF_RE.match(served_url), served_url
    assert served_url == f"/api/interactions/media/{seen['i']}", seen
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", seen["i"]), seen
    assert seen["m"] == expected_text, seen
    assert seen["k"] == expected_kind, seen
    assert seen["ak"] == expected_kind, seen
    assert seen["mm"] == served_mime, seen
    assert seen["sha"] == hashlib.sha256(served_bytes).hexdigest(), seen
    assert seen["sz"] == str(len(served_bytes)), seen
    assert seen["fn"] == expected_filename, seen
    assert seen["afn"] == expected_filename, seen

    # The reply ran and reached the participant on the vendor stub — the turn completed.
    await wait_channel_send_count(fake, reply_marker, 1)

    # The served-media door hands back the exact stored bytes with the sniffed mime and the inbound
    # security headers; a document downloads as an attachment, an image stays inline.
    served = await get_served_media(bridge.stack, served_url)
    assert served.status_code == 200, served.text
    assert served.headers["content-type"] == served_mime, served.headers
    assert served.content == served_bytes
    assert served.headers["content-security-policy"] == "sandbox"
    assert served.headers["x-content-type-options"] == "nosniff"
    disposition = served.headers.get("content-disposition")
    if document:
        assert disposition is not None, served.headers
        assert disposition.startswith("attachment"), disposition
        assert "filename*=UTF-8''" in disposition, disposition
        assert expected_filename in disposition, disposition
    else:
        assert disposition is None, disposition


@pytest.mark.needs("kind:channels:telegram", "helper:telegram")
async def test_telegram_photo_ingests_to_served_image(media_bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    chat_id = "930001"
    file_id = uniq("tg-ing")
    caption = uniq("tg-cap")
    media_bridge.telegram.media[file_id] = MediaBlob(body=TINY_PNG, content_type="image/png")
    inbound = media_bridge.telegram_inbound_photo(chat_id=chat_id, file_id=file_id, caption=caption)
    await _ingest_and_serve(
        media_bridge,
        uniq,
        channel="telegram",
        our_identity=media_bridge.telegram_our_identity,
        inbound=inbound,
        inbound_path=TELEGRAM_INBOUND_PATH,
        fake=media_bridge.fake_telegram,
        expected_text=caption,
        expected_kind="image",
        served_bytes=TINY_PNG,
        served_mime="image/png",
        expected_filename="",
        document=False,
    )


@pytest.mark.needs("kind:channels:slack", "helper:slack")
async def test_slack_file_share_ingests_to_served_image(
    media_bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    channel = f"C0{uniq('slk').upper().replace('_', '')[:8]}"
    ref = uniq("slk-ing")
    media_bridge.slack.media[ref] = MediaBlob(body=TINY_PNG, content_type="image/png")
    files = [
        {
            "url_private": media_bridge.slack.download_url(ref),
            "name": "photo.png",
            "mimetype": "image/png",
            "size": len(TINY_PNG),
        }
    ]
    inbound = media_bridge.slack_inbound_files(channel=channel, files=files, event_id=f"Ev{uniq('slk-ev')}")
    await _ingest_and_serve(
        media_bridge,
        uniq,
        channel="slack",
        our_identity=SLACK_BOT_USER_ID,
        inbound=inbound,
        inbound_path=SLACK_INBOUND_PATH,
        fake=media_bridge.fake_slack,
        expected_text="[image]",
        expected_kind="image",
        served_bytes=TINY_PNG,
        served_mime="image/png",
        expected_filename="",
        document=False,
    )


@pytest.mark.needs("kind:channels:twilio", "helper:twilio")
async def test_twilio_mms_document_ingests_to_served_attachment(
    media_bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    ref = uniq("tw-ing")
    media_bridge.fake_twilio.media[ref] = MediaBlob(body=TINY_PDF, content_type="application/pdf")
    inbound = media_bridge.twilio_inbound_mms(
        our_identity=BRIDGE_TWILIO_FROM,
        client=BRIDGE_TWILIO_CLIENT,
        media_url=media_bridge.fake_twilio.media_url(ref),
        content_type="application/pdf",
    )
    await _ingest_and_serve(
        media_bridge,
        uniq,
        channel="twilio",
        our_identity=BRIDGE_TWILIO_FROM,
        inbound=inbound,
        inbound_path=TWILIO_INBOUND_PATH,
        fake=media_bridge.fake_twilio,
        expected_text="[document: document.pdf]",
        expected_kind="document",
        served_bytes=TINY_PDF,
        served_mime="application/pdf",
        expected_filename="document.pdf",
        document=True,
    )
    # The bytes were served from the CDN origin the MediaUrl 307'd to, and the account Basic auth was
    # DROPPED on that cross-origin hop — never re-sent to the third-party CDN.
    assert media_bridge.fake_twilio.cdn_authorizations == [None], media_bridge.fake_twilio.cdn_authorizations


@pytest.mark.needs("kind:channels:whatsapp", "helper:whatsapp")
async def test_whatsapp_image_ingests_to_served_image(media_bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    media_id = uniq("wa-ing")
    caption = uniq("wa-cap")
    media_bridge.fake_whatsapp.media_meta[media_id] = {
        "mime_type": "image/png",
        "sha256": hashlib.sha256(TINY_PNG).hexdigest(),
        "file_size": len(TINY_PNG),
    }
    media_bridge.fake_whatsapp.media[media_id] = MediaBlob(body=TINY_PNG, content_type="image/png")
    inbound = media_bridge.whatsapp_inbound_image(
        phone_number_id=BRIDGE_WHATSAPP_PHONE_ID,
        wa_id=BRIDGE_WHATSAPP_CLIENT,
        media_id=media_id,
        mime_type="image/png",
        caption=caption,
        sha256=hashlib.sha256(TINY_PNG).hexdigest(),
    )
    await _ingest_and_serve(
        media_bridge,
        uniq,
        channel="whatsapp",
        our_identity=BRIDGE_WHATSAPP_PHONE_ID,
        inbound=inbound,
        inbound_path=WHATSAPP_INBOUND_PATH,
        fake=media_bridge.fake_whatsapp,
        expected_text=caption,
        expected_kind="image",
        served_bytes=TINY_PNG,
        served_mime="image/png",
        expected_filename="",
        document=False,
    )
