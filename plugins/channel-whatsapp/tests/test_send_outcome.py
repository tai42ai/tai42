"""What a WhatsApp send reports about itself: the pre-send address and the completed send's outcome.

``resolve_address`` answers the ``phone_number_id`` and the ``wa_id`` a send would use, the
recipient as the caller names it. A completed send's :class:`SendOutcome` carries the ``wa_id``
Meta resolved the send to (``contacts[0].wa_id``), the same ``client_address`` the inbound door
passes for a message from that recipient, and every ``wamid`` sent, in order. The sends the
inbound form path makes itself go through the recorded-send seam.
"""

from __future__ import annotations

import httpx
import pytest
from tai42_contract.channels import (
    ChannelAddress,
    ChannelAddressError,
    ChannelDeliveryError,
    ChannelNotification,
    InboundAnswerOutcome,
)
from tai42_contract.conversations import AnswerPart
from tai42_contract.interactions.models import LocationElement, MediaItem, MediaKind
from tai42_kit.settings import reset_all_settings

import tai42_channel_whatsapp.inbound  # noqa: F401  (route registration side-effect)
from tai42_channel_whatsapp.channel import WhatsAppChannel
from tai42_channel_whatsapp.client import SentMessage, send_outcome
from tai42_channel_whatsapp.inbound.forms import _FORM_REJECTION_LEAD, _FORM_UNPROCESSABLE, _MAX_FORM_REJECTIONS

from .conftest import (
    _FORM_QUESTION,
    _FORM_SCHEMA,
    _WAMID,
    ALLOWED_A,
    PHONE_NUMBER_ID,
    WA_ID,
    FakeHttpx,
    FakeRedis,
    _accepted,
    _seed_pending,
    _seed_pending_form,
    form_reply_payload,
    make_delivery,
    message_payload,
    response,
    signed_request,
)
from .test_inbound_forms import (
    _flow_accepted,
    _seed_flow_cache,
    _set_stored_rejections,
    waba_env,  # noqa: F401  # the WABA id fixture the form re-send resolves its Flow under
)

pytestmark = pytest.mark.usefixtures("whatsapp_env")

_PLUS = "+15551234567"
_RESOLVED = "15551234567"
_SENDER = "20000000000002"


# -- resolve_address ---------------------------------------------------------------------------


def test_resolve_address_is_the_default_number_and_the_recipient():
    assert WhatsAppChannel().resolve_address(ALLOWED_A, None) == ChannelAddress(
        our_identity=PHONE_NUMBER_ID, address=ALLOWED_A
    )


def test_resolve_address_keeps_the_recipient_as_the_caller_named_it():
    assert WhatsAppChannel().resolve_address(_PLUS, None).address == _PLUS


def test_resolve_address_with_a_sender_identity_sends_from_it():
    assert WhatsAppChannel().resolve_address(ALLOWED_A, _SENDER) == ChannelAddress(
        our_identity=_SENDER, address=ALLOWED_A
    )


@pytest.mark.parametrize("sender_identity", [None, _SENDER])
def test_resolve_address_with_no_recipient_is_an_address_error(sender_identity: str | None):
    with pytest.raises(ChannelAddressError):
        WhatsAppChannel().resolve_address(None, sender_identity)


def test_resolve_address_with_no_default_number_is_a_delivery_error(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID")
    reset_all_settings()
    with pytest.raises(ChannelDeliveryError, match="CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID") as refused:
        WhatsAppChannel().resolve_address(ALLOWED_A, None)
    assert not isinstance(refused.value, ChannelAddressError)


# -- the outcome of a completed send -----------------------------------------------------------


async def test_a_notify_reports_the_wa_id_the_inbound_door_names(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    fake_httpx.responses.append(_accepted("wamid.N", wa_id=_RESOLVED))

    outcome = await WhatsAppChannel().notify(ChannelNotification(message="ping", recipient=_PLUS))

    assert fake_httpx.calls[0]["json"]["to"] == _PLUS
    assert (outcome.our_identity, outcome.address, outcome.message_ids) == (PHONE_NUMBER_ID, _RESOLVED, ["wamid.N"])
    await handler(signed_request(message_payload(wamid="wamid.IN", wa_id=_RESOLVED, text="hi")))
    (call,) = stub_app.conversations.accept_calls
    assert (call["our_identity"], call["client_address"]) == (outcome.our_identity, outcome.address)


async def test_a_multi_part_notify_reports_every_wamid_in_send_order(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.extend([_accepted("wamid.BODY"), _accepted("wamid.LOC"), _accepted("wamid.IMG")])
    notification = ChannelNotification(
        message="Here:",
        recipient=ALLOWED_A,
        location=LocationElement(latitude=1.5, longitude=2.5),
        media=[MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/a.png")],
    )

    outcome = await WhatsAppChannel().notify(notification)

    assert outcome.address == ALLOWED_A
    assert outcome.message_ids == ["wamid.BODY", "wamid.LOC", "wamid.IMG"]


async def test_a_bridge_reply_notify_reports_its_sending_number(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted("wamid.BR"))

    outcome = await WhatsAppChannel().notify(
        ChannelNotification(message="reply", recipient=ALLOWED_A, sender_identity=_SENDER)
    )

    assert (outcome.our_identity, outcome.address, outcome.message_ids) == (_SENDER, ALLOWED_A, ["wamid.BR"])


async def test_a_send_whose_response_names_no_contact_raises(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(response(200, json={"messages": [{"id": "wamid.X"}]}))

    with pytest.raises(ChannelDeliveryError, match=r"accepted the send \(wamid\.X\) but returned no contact wa_id"):
        await WhatsAppChannel().notify(ChannelNotification(message="ping", recipient=ALLOWED_A))


def test_an_outcome_of_no_message_raises():
    with pytest.raises(ChannelDeliveryError, match="produced no message"):
        send_outcome(PHONE_NUMBER_ID, [])


def test_an_outcome_is_addressed_by_the_first_message():
    sent = [SentMessage(wamid="wamid.A", wa_id=_RESOLVED), SentMessage(wamid="wamid.B", wa_id=_RESOLVED)]

    outcome = send_outcome(PHONE_NUMBER_ID, sent)

    assert (outcome.our_identity, outcome.address, outcome.message_ids) == (
        PHONE_NUMBER_ID,
        _RESOLVED,
        ["wamid.A", "wamid.B"],
    )


async def test_a_text_deliver_reports_the_question(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted("wamid.Q", wa_id=_RESOLVED))

    outcome = await WhatsAppChannel().deliver(make_delivery(recipient=_PLUS))

    assert (outcome.our_identity, outcome.address, outcome.message_ids) == (PHONE_NUMBER_ID, _RESOLVED, ["wamid.Q"])


async def test_a_select_deliver_reports_the_choice_message(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted("wamid.SEL"))

    outcome = await WhatsAppChannel().deliver(make_delivery(answer_format="select", options=["yes", "no"]))

    assert outcome.message_ids == ["wamid.SEL"]


@pytest.mark.parametrize("answer_format", ["confirm", "external"])
async def test_a_link_answered_deliver_reports_the_media_then_the_link(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx, answer_format: str
):
    fake_httpx.responses.extend([_accepted("wamid.IMG"), _accepted("wamid.LINK")])
    media = [MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/a.png")]

    outcome = await WhatsAppChannel().deliver(make_delivery(answer_format=answer_format, media=media))

    assert (outcome.address, outcome.message_ids) == (ALLOWED_A, ["wamid.IMG", "wamid.LINK"])


async def test_a_form_deliver_reports_the_flow_message(
    waba_env,  # noqa: F811
    fake_redis: FakeRedis,
    fake_httpx: FakeHttpx,
):
    _seed_flow_cache(fake_redis)
    fake_httpx.responses.append(_flow_accepted("wamid.FLOW"))

    outcome = await WhatsAppChannel().deliver(
        make_delivery(recipient=WA_ID, answer_format="form", schema=_FORM_SCHEMA, question=_FORM_QUESTION)
    )

    assert (outcome.our_identity, outcome.address, outcome.message_ids) == (PHONE_NUMBER_ID, WA_ID, ["wamid.FLOW"])


# -- the inbound message ids the platform keeps ------------------------------------------------


async def test_a_reply_to_a_pending_ask_hands_the_ladder_its_wamid_as_the_medium_id(
    handler, channels, fake_redis: FakeRedis
):
    await _seed_pending()

    await handler(signed_request(message_payload(text="yes")))

    (call,) = channels.inbound_calls
    assert call.bridge.medium_message_id == _WAMID


# -- the sends the inbound form path makes itself -----------------------------------------------


async def test_a_form_re_send_goes_through_the_recorded_send(
    waba_env,  # noqa: F811
    stub_app,
    handler,
    channels,
    fake_redis: FakeRedis,
    fake_httpx: FakeHttpx,
):
    await _seed_pending_form()
    _seed_flow_cache(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.RETRY_KEPT
    channels.inbound_retry_reason = "note: must not be blank"
    fake_httpx.responses.append(_flow_accepted("wamid.RESEND"))

    await handler(signed_request(form_reply_payload({"flow_token": "int-1", "note": "x"})))

    (call,) = stub_app.conversations.send_recorded_calls
    body = f"{_FORM_QUESTION}\n\n{_FORM_REJECTION_LEAD} note: must not be blank"
    assert (call["channel"], call["our_identity"], call["client_address"]) == ("whatsapp", PHONE_NUMBER_ID, WA_ID)
    assert call["content"] == AnswerPart(message=body)
    outcome = call["outcome"]
    assert (outcome.our_identity, outcome.address, outcome.message_ids) == (PHONE_NUMBER_ID, WA_ID, ["wamid.RESEND"])


async def test_the_closing_message_at_the_rejection_cap_goes_through_the_recorded_send(
    waba_env,  # noqa: F811
    stub_app,
    handler,
    channels,
    fake_redis: FakeRedis,
    fake_httpx: FakeHttpx,
):
    await _seed_pending_form()
    _set_stored_rejections(fake_redis, _MAX_FORM_REJECTIONS)
    channels.inbound_outcome = InboundAnswerOutcome.RETRY_KEPT
    fake_httpx.responses.append(_flow_accepted("wamid.FINAL"))

    await handler(signed_request(form_reply_payload({"flow_token": "int-1", "note": "x"})))

    (call,) = stub_app.conversations.send_recorded_calls
    assert (call["our_identity"], call["client_address"]) == (PHONE_NUMBER_ID, WA_ID)
    assert call["content"] == AnswerPart(message=_FORM_UNPROCESSABLE)
    assert call["outcome"].message_ids == ["wamid.FINAL"]


async def test_a_failed_form_re_send_raises_through_the_recorded_send(
    waba_env,  # noqa: F811
    stub_app,
    handler,
    channels,
    fake_redis: FakeRedis,
    fake_httpx: FakeHttpx,
):
    await _seed_pending_form()
    _seed_flow_cache(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.RETRY_KEPT
    fake_httpx.responses.append(httpx.Response(500, text="graph down", request=httpx.Request("POST", "https://x/")))

    with pytest.raises(ChannelDeliveryError, match="HTTP 500"):
        await handler(signed_request(form_reply_payload({"flow_token": "int-1", "note": "x"})))

    (call,) = stub_app.conversations.send_recorded_calls
    assert isinstance(call["error"], ChannelDeliveryError)
