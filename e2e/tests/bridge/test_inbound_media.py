"""Inbound media bridges to one turn on every channel — served attachment + media params.

A participant sends media (a telegram photo, a slack file share, a twilio MMS, a whatsapp image) to
the channel's signed inbound door. The channel fetches the vendor bytes, ingests them through the seam,
and bridges ONE conversation turn whose text is the caption (or the contract's non-blank ``[kind]``
placeholder), whose typed ``attachments`` MediaItem carries the served capability url
(``/api/interactions/media/<id>``), and whose opaque ``media_*`` entry-params carry the SERVED media
id / kind / (sniffed) mime — surfaced verbatim onto the routed tool's payload. The turn runs to
completion and its reply is delivered back to the vendor stub. Driven end to end over the live
media-bridge stack (all four channels + storage-local + the redis conversations backend + a real turn):
signed webhook → inbound decode → vendor fetch → ``ingest_media`` → ``conversations.accept`` → route →
tool dispatch → the tool records what it received, then the reply is sent.

The scripted-LLM is not involved (a tool target dispatches deterministically); the four channels mock
over their in-process stubs, so any real selection breaks the stubs and the module steps aside.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

import pytest

from tai42_e2e.channel_stubs import TINY_PNG, MediaBlob
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
    SLACK_INBOUND_PATH,
    TELEGRAM_INBOUND_PATH,
    TWILIO_INBOUND_PATH,
    WHATSAPP_INBOUND_PATH,
    BridgeHarness,
    post_inbound,
    wait_channel_send_count,
    wait_probe_record,
)

# The four channels mock over their in-process stubs; a real selection sends outbound to the live
# vendor (never the stub) and breaks the deterministic scripted round-trip, so the module steps
# aside — the real legs run on the dedicated creds host. Inert on the all-mock default.
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

# The start_expr records the bridged turn's text, the media params the tool saw, and the typed
# attachment's served url — all as one JSON string.
_RECORD_EXPR = (
    '{{key: "{probe}", value: ('
    '{{m: .message, k: (.params.media_kind // ""), '
    'i: (.params.media_id // ""), mm: (.params.media_mime_type // ""), '
    'au: (.attachments[0].url // "")}} | tostring)}}'
)


async def _bridge_media(
    bridge: BridgeHarness,
    uniq: Callable[[str], str],
    *,
    channel: str,
    our_identity: str,
    inbound: SignedInbound,
    inbound_path: str,
    fake: object,
    expected_text: str,
    expected_mime: str,
) -> None:
    """Route a media inbound to a tool target, assert the ONE turn's text + served attachment + media
    params (``media_id`` now the served id, a typed attachment present), and that the tool's reply
    reaches the vendor stub."""
    exec_key = uniq("media-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    probe = uniq("media-probe")
    reply_marker = uniq("media-reply")
    await bridge.create_tool_channel_route(
        route_name=uniq("media-route").replace("_", "-"),
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
    assert seen["m"] == expected_text, seen
    assert seen["k"] == "image", seen
    assert seen["mm"] == expected_mime, seen
    # media_id is now the SERVED capability id (43 urlsafe-base64 chars), and a typed attachment
    # carrying the same served reference rides the turn.
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", seen["i"]), seen
    assert seen["au"] == f"/api/interactions/media/{seen['i']}", seen

    # The turn ran and its reply was delivered back to the participant on the vendor stub.
    await wait_channel_send_count(fake, reply_marker, 1)


@pytest.mark.needs("kind:channels:telegram", "helper:telegram")
async def test_telegram_photo_with_caption_bridges_one_turn(
    media_bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    chat_id = "910001"
    file_id = uniq("tg-file")
    caption = uniq("tg-caption")
    media_bridge.telegram.media[file_id] = MediaBlob(body=TINY_PNG, content_type="image/png")
    inbound = media_bridge.telegram_inbound_photo(chat_id=chat_id, file_id=file_id, caption=caption)
    await _bridge_media(
        media_bridge,
        uniq,
        channel="telegram",
        our_identity=media_bridge.telegram_our_identity,
        inbound=inbound,
        inbound_path=TELEGRAM_INBOUND_PATH,
        fake=media_bridge.fake_telegram,
        expected_text=caption,
        expected_mime="image/png",
    )


@pytest.mark.needs("kind:channels:slack", "helper:slack")
async def test_slack_file_share_bridges_one_turn(media_bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    channel = f"C0{uniq('slk').upper().replace('_', '')[:8]}"
    ref = uniq("slk-file")
    media_bridge.slack.media[ref] = MediaBlob(body=TINY_PNG, content_type="image/png")
    inbound = media_bridge.slack_inbound_files(
        channel=channel,
        files=[
            {
                "url_private": media_bridge.slack.download_url(ref),
                "name": "photo.png",
                "mimetype": "image/png",
                "size": len(TINY_PNG),
            }
        ],
        event_id=f"Ev{uniq('slk-ev')}",
    )
    await _bridge_media(
        media_bridge,
        uniq,
        channel="slack",
        our_identity=SLACK_BOT_USER_ID,
        inbound=inbound,
        inbound_path=SLACK_INBOUND_PATH,
        fake=media_bridge.fake_slack,
        expected_text="[image]",
        expected_mime="image/png",
    )


@pytest.mark.needs("kind:channels:twilio", "helper:twilio")
async def test_twilio_mms_bridges_one_turn(media_bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    ref = uniq("mms")
    media_bridge.fake_twilio.media[ref] = MediaBlob(body=TINY_PNG, content_type="image/png")
    inbound = media_bridge.twilio_inbound_mms(
        our_identity=BRIDGE_TWILIO_FROM,
        client=BRIDGE_TWILIO_CLIENT,
        media_url=media_bridge.fake_twilio.media_url(ref),
        content_type="image/png",
    )
    await _bridge_media(
        media_bridge,
        uniq,
        channel="twilio",
        our_identity=BRIDGE_TWILIO_FROM,
        inbound=inbound,
        inbound_path=TWILIO_INBOUND_PATH,
        fake=media_bridge.fake_twilio,
        expected_text="[image]",
        expected_mime="image/png",
    )


@pytest.mark.needs("kind:channels:whatsapp", "helper:whatsapp")
async def test_whatsapp_image_with_caption_bridges_one_turn(
    media_bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    media_id = uniq("wa-media")
    caption = uniq("wa-caption")
    media_bridge.fake_whatsapp.media_meta[media_id] = {"mime_type": "image/png", "file_size": len(TINY_PNG)}
    media_bridge.fake_whatsapp.media[media_id] = MediaBlob(body=TINY_PNG, content_type="image/png")
    inbound = media_bridge.whatsapp_inbound_image(
        phone_number_id=BRIDGE_WHATSAPP_PHONE_ID,
        wa_id=BRIDGE_WHATSAPP_CLIENT,
        media_id=media_id,
        mime_type="image/png",
        caption=caption,
    )
    await _bridge_media(
        media_bridge,
        uniq,
        channel="whatsapp",
        our_identity=BRIDGE_WHATSAPP_PHONE_ID,
        inbound=inbound,
        inbound_path=WHATSAPP_INBOUND_PATH,
        fake=media_bridge.fake_whatsapp,
        expected_text=caption,
        expected_mime="image/png",
    )
