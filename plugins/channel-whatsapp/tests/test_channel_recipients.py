"""Recipient resolution and the template allowlist fence — allowlist normalization,
known-contact admission keyed on the send-from number, and cold-template refusal."""

from __future__ import annotations

import pytest
from tai42_contract.channels import ChannelDeliveryError, ChannelNotification, ChannelTemplate

from tai42_channel_whatsapp.channel import WhatsAppChannel

from .conftest import _UNLISTED, ALLOWED_A, PHONE_NUMBER_ID, FakeHttpx, FakeRedis, _accepted, make_delivery

pytestmark = pytest.mark.usefixtures("whatsapp_env")


def _known_contact_key(wa_id: str, phone_number_id: str = PHONE_NUMBER_ID) -> str:
    return f"channel:whatsapp:known-contact:{phone_number_id}:{wa_id}"


async def test_template_allowlist_padded_entry_matches_unpadded_recipient(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    from tai42_kit.settings import reset_all_settings

    # Allowlist-entry normalization (fail-open) stays meaningful only on the
    # template path — a padded JSON entry matches the unpadded recipient there.
    monkeypatch.setenv("CHANNEL_WHATSAPP_ALLOWED_RECIPIENTS", '[" 15551230001 ", ""]')
    reset_all_settings()

    fake_httpx.responses.append(_accepted())
    await WhatsAppChannel().notify(
        ChannelNotification(
            message="Your item is done.",
            recipient=ALLOWED_A,
            template=ChannelTemplate(name="status_update", language="en_US"),
        )
    )

    assert fake_httpx.calls[0]["json"]["to"] == ALLOWED_A
    assert fake_httpx.calls[0]["json"]["type"] == "template"


async def test_empty_allowlist_freeform_sends_but_cold_template_rejected(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_WHATSAPP_ALLOWED_RECIPIENTS")
    reset_all_settings()

    # Freeform to any recipient succeeds even with an empty allowlist.
    fake_httpx.responses.append(_accepted())
    await WhatsAppChannel().deliver(make_delivery(recipient=ALLOWED_A))
    assert fake_httpx.calls[0]["json"]["to"] == ALLOWED_A

    # The fail-closed policy moved to templates: a cold unlisted number is refused.
    with pytest.raises(ChannelDeliveryError, match=r"template send to .* refused"):
        await WhatsAppChannel().notify(
            ChannelNotification(
                message="Cold ping.",
                recipient=ALLOWED_A,
                template=ChannelTemplate(name="ping", language="en_US"),
            )
        )
    assert len(fake_httpx.calls) == 1  # only the freeform send happened


async def test_notify_template_maps_body_parameters(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted("wamid.TPL"))

    ids = await WhatsAppChannel().notify(
        ChannelNotification(
            message="Item done.",
            recipient=ALLOWED_A,
            template=ChannelTemplate(name="status_update", language="en_US", body_parameters=["Jane", "A-42"]),
        )
    )

    assert ids == ["wamid.TPL"]
    payload = fake_httpx.calls[0]["json"]
    assert payload["type"] == "template"
    assert payload["template"]["components"] == [
        {"type": "body", "parameters": [{"type": "text", "text": "Jane"}, {"type": "text", "text": "A-42"}]}
    ]


async def test_template_to_known_contact_sends(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # An unlisted recipient the webhook saw within the window is a known contact —
    # a template send reaches them without an allowlist entry.
    fake_redis.store[_known_contact_key(_UNLISTED)] = "1"
    fake_httpx.responses.append(_accepted("wamid.KC"))

    ids = await WhatsAppChannel().notify(
        ChannelNotification(
            message="Hi again.",
            recipient=_UNLISTED,
            template=ChannelTemplate(name="reengage", language="en_US"),
        )
    )

    assert ids == ["wamid.KC"]
    assert fake_httpx.calls[0]["json"]["to"] == _UNLISTED


async def test_template_to_cold_unlisted_recipient_rejected(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A cold, unlisted number (no known-contact marker) is refused loudly.
    with pytest.raises(ChannelDeliveryError, match=r"template send to .* refused"):
        await WhatsAppChannel().notify(
            ChannelNotification(
                message="Cold.",
                recipient=_UNLISTED,
                template=ChannelTemplate(name="reengage", language="en_US"),
            )
        )

    assert not fake_httpx.calls


async def test_template_known_contact_keys_on_send_from_number(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # The known-contact lookup keys on the RESOLVED send-from phone_number_id: a
    # marker under a DIFFERENT number does not admit a template send.
    fake_redis.store[_known_contact_key(_UNLISTED, phone_number_id="99999999999999")] = "1"

    with pytest.raises(ChannelDeliveryError, match=r"template send to .* refused"):
        await WhatsAppChannel().notify(
            ChannelNotification(
                message="Cold.",
                recipient=_UNLISTED,
                template=ChannelTemplate(name="reengage", language="en_US"),
            )
        )

    assert not fake_httpx.calls


async def test_channel_advertises_media_and_template_capabilities():
    assert WhatsAppChannel.supports_media_notifications is True
    assert WhatsAppChannel.supports_template_notifications is True
    # Tappable notification options render as native reply buttons/list, so the
    # central notify_user guard dispatches an options notification here instead of 501.
    assert WhatsAppChannel.supports_interactive_notifications is True
    # This channel shares a geographic location natively (send_location).
    assert WhatsAppChannel.supports_location_notifications is True
