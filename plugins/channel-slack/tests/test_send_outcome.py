"""``SlackChannel.resolve_address`` and the ``SendOutcome`` ``notify`` / ``deliver`` return.

The outcome names the participant by the conversation id ``chat.postMessage`` reports it
posted into, the id the events door passes as ``client_address`` for a message from there:
a send to a user id lands in a direct-message conversation whose id only the response gives.
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
from tai42_kit.settings import reset_all_settings

from tai42_channel_slack.channel import SlackChannel

from .conftest import (
    TEST_ALLOWED_RECIPIENT,
    TEST_BOT_USER_ID,
    TEST_DEFAULT_RECIPIENT,
    _ok_response,
    make_delivery,
)

pytestmark = pytest.mark.usefixtures("slack_env")

_DIRECT = "D0DIRECT1"
_PERSON = "U0PERSON1"
_FORM_SCHEMA = {"type": "object", "properties": {"name": {"type": "string", "title": "Name"}}}


def test_resolve_address_with_no_values_gives_the_bot_and_the_default_conversation():
    assert SlackChannel().resolve_address(None, None) == ChannelAddress(
        our_identity=TEST_BOT_USER_ID, address=TEST_DEFAULT_RECIPIENT
    )


def test_resolve_address_checks_a_requested_recipient_against_the_allowlist():
    channel = SlackChannel()

    assert channel.resolve_address(TEST_ALLOWED_RECIPIENT, None) == ChannelAddress(
        our_identity=TEST_BOT_USER_ID, address=TEST_ALLOWED_RECIPIENT
    )
    with pytest.raises(ChannelAddressError, match="not on CHANNEL_SLACK_ALLOWED_RECIPIENTS"):
        channel.resolve_address("C0UNLISTED", None)


def test_resolve_address_with_the_bot_identity_takes_the_recipient_as_given():
    channel = SlackChannel()

    assert channel.resolve_address(_PERSON, TEST_BOT_USER_ID) == ChannelAddress(
        our_identity=TEST_BOT_USER_ID, address=_PERSON
    )
    assert channel.resolve_address(None, TEST_BOT_USER_ID) == ChannelAddress(
        our_identity=TEST_BOT_USER_ID, address=TEST_DEFAULT_RECIPIENT
    )


def test_resolve_address_with_another_identity_is_refused():
    with pytest.raises(ChannelDeliveryError, match="is not this channel's identity"):
        SlackChannel().resolve_address(None, "U0OTHERBOT")


@pytest.mark.parametrize("sender_identity", [None, TEST_BOT_USER_ID])
def test_resolve_address_with_no_recipient_and_no_default_is_an_address_error(
    monkeypatch: pytest.MonkeyPatch, sender_identity: str | None
):
    monkeypatch.delenv("CHANNEL_SLACK_DEFAULT_RECIPIENT")
    reset_all_settings()

    with pytest.raises(ChannelAddressError, match="CHANNEL_SLACK_DEFAULT_RECIPIENT"):
        SlackChannel().resolve_address(None, sender_identity)


def test_resolve_address_with_no_bot_user_id_is_a_delivery_error(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CHANNEL_SLACK_BOT_USER_ID")
    reset_all_settings()

    with pytest.raises(ChannelDeliveryError, match="CHANNEL_SLACK_BOT_USER_ID") as refused:
        SlackChannel().resolve_address(None, None)
    assert not isinstance(refused.value, ChannelAddressError)


async def test_notify_to_a_user_reports_the_direct_conversation_the_response_names(http_script, fake_redis):
    http_script.results.append(_ok_response(ts="5.5", channel=_DIRECT))
    channel = SlackChannel()
    notification = ChannelNotification(message="Reply.", recipient=_PERSON, sender_identity=TEST_BOT_USER_ID)

    outcome = await channel.notify(notification)

    assert outcome == SendOutcome(our_identity=TEST_BOT_USER_ID, address=_DIRECT, message_ids=["5.5"])
    pair = channel.resolve_address(notification.recipient, notification.sender_identity)
    assert (pair.our_identity, pair.address) == (outcome.our_identity, _PERSON)


async def test_notify_ok_true_without_channel_raises(http_script, fake_redis):
    http_script.results.append(_ok_response(ts="5.5", channel=None))

    with pytest.raises(ChannelDeliveryError, match="no channel"):
        await SlackChannel().notify(ChannelNotification(message="hi"))


async def test_deliver_reports_the_conversation_and_the_question_ts(http_script, fake_redis):
    http_script.results.append(_ok_response(ts="6.6", channel=TEST_ALLOWED_RECIPIENT))
    channel = SlackChannel()

    outcome = await channel.deliver(make_delivery(recipient=TEST_ALLOWED_RECIPIENT))

    assert outcome == SendOutcome(our_identity=TEST_BOT_USER_ID, address=TEST_ALLOWED_RECIPIENT, message_ids=["6.6"])
    assert outcome.our_identity == channel.resolve_address(TEST_ALLOWED_RECIPIENT, None).our_identity


@pytest.mark.parametrize(("ts", "message_ids"), [("7.7", ["7.7"]), (None, [])])
async def test_a_link_answered_deliver_reports_the_ts_when_the_response_has_one(
    http_script, fake_redis, ts: str | None, message_ids: list[str]
):
    http_script.results.append(_ok_response(ts=ts))

    outcome = await SlackChannel().deliver(make_delivery(answer_format="confirm"))

    assert outcome == SendOutcome(
        our_identity=TEST_BOT_USER_ID, address=TEST_DEFAULT_RECIPIENT, message_ids=message_ids
    )


async def test_a_form_deliver_reports_the_open_button_message(http_script, fake_redis):
    http_script.results.append(_ok_response(ts="8.8"))

    outcome = await SlackChannel().deliver(make_delivery(answer_format="form", schema=_FORM_SCHEMA))

    assert outcome == SendOutcome(our_identity=TEST_BOT_USER_ID, address=TEST_DEFAULT_RECIPIENT, message_ids=["8.8"])
