"""``TwilioChannel.resolve_address`` and the ``SendOutcome`` ``notify`` / ``deliver`` return.

The outcome names the participant in the form the inbound door passes for the same number
(``To`` as ``our_identity``, ``From`` as ``client_address``), so a send and the reply to it
land in one thread.
"""

from __future__ import annotations

import pytest
from tai42_contract.channels import (
    ChannelAddress,
    ChannelAddressError,
    ChannelDeliveryError,
    ChannelNotification,
    SendOutcome,
)

from tai42_channel_twilio.channel import TwilioChannel

from .conftest import FakeHttpx, FakeRedis, make_delivery, response

pytestmark = pytest.mark.usefixtures("twilio_env")

_FROM = "+15550000001"
_DEFAULT = "+15550000002"
_LISTED = "+15550000003"


def test_resolve_address_with_no_values_gives_the_default_identity_and_recipient():
    assert TwilioChannel().resolve_address(None, None) == ChannelAddress(our_identity=_FROM, address=_DEFAULT)


def test_resolve_address_checks_a_requested_recipient_against_the_allowlist():
    channel = TwilioChannel()

    assert channel.resolve_address(_LISTED, None) == ChannelAddress(our_identity=_FROM, address=_LISTED)
    with pytest.raises(ChannelAddressError, match="not on CHANNEL_TWILIO_ALLOWED_RECIPIENTS"):
        channel.resolve_address("+15559999999", None)


def test_resolve_address_with_a_sender_identity_sends_from_it_to_the_recipient_as_given():
    channel = TwilioChannel()

    assert channel.resolve_address("+15559999999", "+15550000042") == ChannelAddress(
        our_identity="+15550000042", address="+15559999999"
    )
    assert channel.resolve_address(None, "+15550000042") == ChannelAddress(
        our_identity="+15550000042", address=_DEFAULT
    )


@pytest.mark.parametrize("sender_identity", [None, "+15550000042"])
def test_resolve_address_with_no_recipient_and_no_default_is_an_address_error(
    monkeypatch: pytest.MonkeyPatch, sender_identity: str | None
):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_TWILIO_DEFAULT_RECIPIENT")
    reset_all_settings()

    with pytest.raises(ChannelAddressError, match="set CHANNEL_TWILIO_DEFAULT_RECIPIENT"):
        TwilioChannel().resolve_address(None, sender_identity)


def test_resolve_address_with_no_from_number_is_a_delivery_error(monkeypatch: pytest.MonkeyPatch):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_TWILIO_FROM")
    reset_all_settings()

    with pytest.raises(ChannelDeliveryError, match="set CHANNEL_TWILIO_FROM") as refused:
        TwilioChannel().resolve_address(None, None)
    assert not isinstance(refused.value, ChannelAddressError)


async def test_notify_from_a_sender_identity_reports_the_pair_the_inbound_door_passes(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    fake_httpx.responses.append(response(201, json={"sid": "SM_reply"}))
    channel = TwilioChannel()
    notification = ChannelNotification(
        message="Reply.", recipient="whatsapp:+15559999999", sender_identity="+15550000042"
    )

    outcome = await channel.notify(notification)

    # The inbound door passes To (our number) as our_identity and From verbatim as client_address.
    assert outcome == SendOutcome(
        our_identity="+15550000042", address="whatsapp:+15559999999", message_ids=["SM_reply"]
    )
    assert (
        outcome.our_identity
        == channel.resolve_address(notification.recipient, notification.sender_identity).our_identity
    )
    data = fake_httpx.calls[0]["data"]
    assert (data["From"], data["To"]) == (outcome.our_identity, outcome.address)


async def test_notify_to_the_default_reports_the_default_pair(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(response(201, json={"sid": "SM_default"}))

    outcome = await TwilioChannel().notify(ChannelNotification(message="Deploy finished."))

    assert outcome == SendOutcome(our_identity=_FROM, address=_DEFAULT, message_ids=["SM_default"])


@pytest.mark.parametrize(("recipient", "address"), [(None, _DEFAULT), (_LISTED, _LISTED)])
async def test_deliver_reports_the_pair_and_the_question_message_sid(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx, recipient: str | None, address: str
):
    fake_httpx.responses.append(response(201, json={"sid": "SM_question"}))
    channel = TwilioChannel()

    outcome = await channel.deliver(make_delivery(recipient=recipient))

    assert outcome == SendOutcome(our_identity=_FROM, address=address, message_ids=["SM_question"])
    assert outcome.our_identity == channel.resolve_address(recipient, None).our_identity
