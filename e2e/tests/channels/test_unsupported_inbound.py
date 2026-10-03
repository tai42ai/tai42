"""Unsupported inbound content replies once and emits the rejection event — every wired channel.

A content a channel recognises but cannot bridge — a telegram poll, a slack file exposing neither a
fetchable reference nor a name, a whatsapp ``errors`` notice naming the unsupported-message-type
code (131051) — is never silently dropped: the participant gets the ONE generic notice
("This content type is not supported here.") on the vendor stub, the platform emits a single
``conversations_inbound_rejected`` event carrying ``reason == "unsupported_type"`` (observed through a
hook fired on that topic), and no conversation turn is accepted. Twilio has no unsupported case — every
MMS content type maps to a generic kind — so it is not exercised here.

Driven over the live media-bridge stack (all four channels + the hooks manager), signed webhook →
inbound decode → ``notify_inbound_rejected`` chokepoint → the vendor reply + the platform event.

The channels mock over their in-process stubs; a real selection sends outbound to the live vendor and
the stub never sees the notice, so the module steps aside on any real channel.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from tai42_e2e.manifests import BRIDGE_WHATSAPP_PHONE_ID
from tai42_e2e.provider_stub import SignedInbound
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async

pytestmark = [
    pytest.mark.skipif(
        any(HarnessSettings().is_real(seam) for seam in ("telegram", "slack", "twilio", "whatsapp")),
        reason="the media-bridge stubs are the mock leg; real legs run on the creds host",
    ),
    pytest.mark.needs(
        "kind:identity",
        "probe-tools",
        "store:redis",
        "setting:conversations:redis",
        "setting:hooks",
        "setting:seeded-access-control",
    ),
]

# The one fixed participant notice the skeleton sends for an UNSUPPORTED_TYPE rejection.
_NOTICE = "This content type is not supported here."
_REJECTED_TOPIC = "conversations_inbound_rejected"


async def _post(stack: TaiStack, path: str, inbound: SignedInbound) -> httpx.Response:
    """POST a synthesized inbound to replica B's channel door; return the raw response."""
    url = f"{stack.origin(stack.port_b)}{path}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await client.post(url, content=inbound.body, headers=inbound.headers)


async def _register_reject_hook(
    stack: TaiStack, uniq: Callable[[str], str], *, probe: str, client_address: str
) -> None:
    """Register a hook that records ``reason`` for a ``conversations_inbound_rejected`` event whose
    ``client_address`` is this test's recipient — so a concurrent rejection never lands in this probe."""
    exec_key = uniq("rej-exec")
    api = stack.api(port=stack.port_b)
    await api.post(
        "/api/auth/api-keys",
        json={"user_id": exec_key, "description": "e2e unsupported-inbound hook key", "scopes": ["e2e-all"]},
    )
    await api.post(
        "/api/hooks",
        json={
            "name": uniq("rej-hook").replace("_", "-"),
            "topic": _REJECTED_TOPIC,
            "tool": "e2e_record",
            "start_expr": {
                "content": f'if .client_address == "{client_address}" '
                f'then {{key: "{probe}", value: .reason}} else null end'
            },
            "execution_key": exec_key,
        },
    )


def _notices_to(fake: Any, recipient: str, recipient_field: str) -> list[dict]:
    """Recorded sends carrying the notice copy addressed to ``recipient`` (scoped so a stale send
    from another suite on the session-shared stub is never counted)."""
    return [record for record in fake.sends_matching(_NOTICE) if record.get(recipient_field) == recipient]


async def _assert_rejects_once(
    stack: TaiStack,
    uniq: Callable[[str], str],
    *,
    path: str,
    inbound: SignedInbound,
    fake: Any,
    recipient: str,
    recipient_field: str,
    redelivery: SignedInbound,
) -> None:
    """Post an unsupported inbound; assert one notice on the stub + one rejection event + no turn,
    then a redelivery (a deduped repeat) produces no second notice."""
    probe = uniq("rej-probe")
    await _register_reject_hook(stack, uniq, probe=probe, client_address=recipient)

    resp = await _post(stack, path, inbound)
    assert resp.status_code in (200, 204), resp.text

    async def _notice_landed() -> list[dict] | None:
        got = _notices_to(fake, recipient, recipient_field)
        return got if got else None

    notices = await wait_for_async(
        _notice_landed, deadline=12.0, message="the unsupported notice never reached the stub"
    )
    assert len(notices) == 1, f"expected exactly one notice, saw {notices!r}"

    async def _event_recorded() -> list[str] | None:
        rows = [json.loads(raw)["value"] for raw in stack.records(probe)]
        return rows if rows else None

    reasons = await wait_for_async(
        _event_recorded, deadline=12.0, message="the conversations_inbound_rejected event never fired the hook"
    )
    assert reasons == ["unsupported_type"], f"expected one unsupported_type event, saw {reasons!r}"

    # A vendor redelivery of the SAME message is deduped at the door (slack event_id / whatsapp wamid)
    # BEFORE any notify/emit, so it produces no second notice and no second event — asserted straight
    # after the ack, which the door only returns once the redelivery is fully (and inertly) processed.
    resp2 = await _post(stack, path, redelivery)
    assert resp2.status_code in (200, 204), resp2.text
    assert len(_notices_to(fake, recipient, recipient_field)) == 1, "a deduped redelivery sent a second notice"
    assert [json.loads(raw)["value"] for raw in stack.records(probe)] == ["unsupported_type"]


@pytest.mark.needs("kind:channels:slack", "helper:channel-fake:slack", "setting:CHANNEL_SLACK_SIGNING_SECRET")
async def test_slack_nameless_file_is_rejected_once(
    media_bridge_stack: tuple[TaiStack, str], fake_slack: Any, uniq: Callable[[str], str]
) -> None:
    stack, _root = media_bridge_stack
    channel = f"C0{uniq('slk').upper().replace('_', '')[:8]}"
    event_id = f"Ev{uniq('slk-ev')}"
    # A file exposing neither a fetchable url_private nor a name cannot be represented as a turn.
    files = [{"mimetype": "image/png"}]
    inbound = fake_slack.build_inbound_files(
        signing_secret=stack.config.env["CHANNEL_SLACK_SIGNING_SECRET"],
        channel=channel,
        files=files,
        event_id=event_id,
    )
    # Slack dedupes the whole event on event_id, so a redelivery reuses it.
    redelivery = fake_slack.build_inbound_files(
        signing_secret=stack.config.env["CHANNEL_SLACK_SIGNING_SECRET"],
        channel=channel,
        files=files,
        event_id=event_id,
    )
    await _assert_rejects_once(
        stack,
        uniq,
        path="/api/channels/slack/inbound",
        inbound=inbound,
        fake=fake_slack,
        recipient=channel,
        recipient_field="channel",
        redelivery=redelivery,
    )


@pytest.mark.needs("kind:channels:whatsapp", "helper:channel-fake:whatsapp", "setting:CHANNEL_WHATSAPP_APP_SECRET")
async def test_whatsapp_unsupported_type_notice_is_rejected_once(
    media_bridge_stack: tuple[TaiStack, str], fake_whatsapp: Any, uniq: Callable[[str], str]
) -> None:
    stack, _root = media_bridge_stack
    wa_id = f"1650{uuid.uuid4().int % 10**9:09d}"
    wamid = f"wamid.{uuid.uuid4().hex[:12]}"
    secret = stack.config.env["CHANNEL_WHATSAPP_APP_SECRET"]
    inbound = fake_whatsapp.build_inbound_error(
        app_secret=secret,
        phone_number_id=BRIDGE_WHATSAPP_PHONE_ID,
        wa_id=wa_id,
        code=131051,
        title="Unsupported message type",
        wamid=wamid,
    )
    # WhatsApp dedupes per wamid, so a redelivery pins the same id.
    redelivery = fake_whatsapp.build_inbound_error(
        app_secret=secret,
        phone_number_id=BRIDGE_WHATSAPP_PHONE_ID,
        wa_id=wa_id,
        code=131051,
        title="Unsupported message type",
        wamid=wamid,
    )
    await _assert_rejects_once(
        stack,
        uniq,
        path="/api/channels/whatsapp/inbound",
        inbound=inbound,
        fake=fake_whatsapp,
        recipient=wa_id,
        recipient_field="to",
        redelivery=redelivery,
    )


@pytest.mark.needs("kind:channels:whatsapp", "helper:channel-fake:whatsapp", "setting:CHANNEL_WHATSAPP_APP_SECRET")
async def test_whatsapp_non_content_notice_sends_no_reply(
    media_bridge_stack: tuple[TaiStack, str], fake_whatsapp: Any
) -> None:
    """A vendor error notice of ANOTHER code is an operator-facing signal, not participant content —
    it stays log-only: no participant reply, no rejection event."""
    stack, _root = media_bridge_stack
    wa_id = f"1650{uuid.uuid4().int % 10**9:09d}"
    secret = stack.config.env["CHANNEL_WHATSAPP_APP_SECRET"]
    inbound = fake_whatsapp.build_inbound_error(
        app_secret=secret,
        phone_number_id=BRIDGE_WHATSAPP_PHONE_ID,
        wa_id=wa_id,
        code=131047,
        title="Re-engagement message",
    )
    # The door awaits inbound processing before it acks, so once 200 lands the message is fully
    # handled: a non-content notice takes the log-only branch, calling neither notify nor the event.
    resp = await _post(stack, "/api/channels/whatsapp/inbound", inbound)
    assert resp.status_code == 200, resp.text
    assert _notices_to(fake_whatsapp, wa_id, "to") == [], "a non-content notice must send no participant reply"


@pytest.mark.needs("kind:channels:telegram", "helper:channel-fake:telegram", "setting:CHANNEL_TELEGRAM_WEBHOOK_SECRET")
async def test_telegram_poll_is_rejected_and_acked(
    media_bridge_stack: tuple[TaiStack, str], fake_telegram: Any, uniq: Callable[[str], str]
) -> None:
    """A telegram poll gets the generic refusal + event and the door acks 200 ``ignored`` — the
    2xx is what stops the real vendor from ever redelivering (telegram redelivers only on a non-2xx,
    and the unsupported path carries no message-level dedup, so the ack IS the redelivery guard)."""
    stack, _root = media_bridge_stack
    chat_id = str(920000000 + uuid.uuid4().int % 10**7)
    secret = stack.config.env["CHANNEL_TELEGRAM_WEBHOOK_SECRET"]
    inbound = fake_telegram.build_inbound_poll(secret=secret, chat_id=chat_id)

    probe = uniq("rej-probe")
    await _register_reject_hook(stack, uniq, probe=probe, client_address=chat_id)

    resp = await _post(stack, "/api/channels/telegram/inbound", inbound)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "ignored"

    async def _notice_landed() -> list[dict] | None:
        got = _notices_to(fake_telegram, chat_id, "chat_id")
        return got if got else None

    notices = await wait_for_async(_notice_landed, deadline=12.0, message="the poll refusal never reached the stub")
    assert len(notices) == 1, f"expected exactly one refusal, saw {notices!r}"

    async def _event_recorded() -> list[str] | None:
        rows = [json.loads(raw)["value"] for raw in stack.records(probe)]
        return rows if rows else None

    reasons = await wait_for_async(
        _event_recorded, deadline=12.0, message="the conversations_inbound_rejected event never fired the hook"
    )
    assert reasons == ["unsupported_type"], f"expected one unsupported_type event, saw {reasons!r}"
