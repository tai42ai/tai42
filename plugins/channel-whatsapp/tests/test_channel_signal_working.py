"""The WhatsApp ``signal_working`` seam — the combined read+typing refresh the
skeleton's working-signal loop calls, its identity resolution, and its no-ops."""

from __future__ import annotations

import pytest
from tai42_contract.channels import ChannelDeliveryError

from tai42_channel_whatsapp.channel import WhatsAppChannel

from .conftest import PHONE_NUMBER_ID, WA_ID, FakeHttpx, FakeRedis, response

pytestmark = pytest.mark.usefixtures("whatsapp_env")

_OTHER_PHONE_NUMBER_ID = "20000000000002"


async def test_signal_working_active_sends_combined_body_from_default_identity(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # active=True with no sender_identity resolves the configured default phone number
    # id and posts the combined read+typing body referencing the inbound wamid.
    await WhatsAppChannel().signal_working(recipient=WA_ID, provider_message_id="wamid.IN")

    assert len(fake_httpx.typing_calls) == 1
    signal = fake_httpx.typing_calls[0]
    assert signal["url"] == f"https://graph.facebook.com/v23.0/{PHONE_NUMBER_ID}/messages"
    assert signal["json"] == {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": "wamid.IN",
        "typing_indicator": {"type": "text"},
    }
    assert signal["headers"]["Authorization"].startswith("Bearer ")


async def test_signal_working_active_sends_from_sender_identity(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # sender_identity set → the refresh is sent FROM that phone number id.
    await WhatsAppChannel().signal_working(
        recipient=WA_ID, sender_identity=_OTHER_PHONE_NUMBER_ID, provider_message_id="wamid.IN"
    )

    assert len(fake_httpx.typing_calls) == 1
    assert fake_httpx.typing_calls[0]["url"] == f"https://graph.facebook.com/v23.0/{_OTHER_PHONE_NUMBER_ID}/messages"


async def test_signal_working_inactive_is_a_no_op(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # Meta's indicator self-dismisses on the reply or after its expiry; there is no
    # stop API, so active=False sends nothing.
    await WhatsAppChannel().signal_working(recipient=WA_ID, provider_message_id="wamid.IN", active=False)

    assert fake_httpx.typing_calls == []


async def test_signal_working_without_provider_message_id_is_a_no_op(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # With no inbound wamid there is no message to attach typing to.
    await WhatsAppChannel().signal_working(recipient=WA_ID)

    assert fake_httpx.typing_calls == []


async def test_signal_working_rejection_raises_channel_delivery_error(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A send failure raises ChannelDeliveryError for the loop to log and count.
    fake_httpx.typing_response = response(500, text="messages endpoint down")
    with pytest.raises(ChannelDeliveryError):
        await WhatsAppChannel().signal_working(recipient=WA_ID, provider_message_id="wamid.IN")
