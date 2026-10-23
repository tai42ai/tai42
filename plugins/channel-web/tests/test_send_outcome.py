"""What a web send reports about itself: the pre-send address and the completed send's outcome.

``resolve_address`` answers the web route identity and the visitor id a send would use, from
either addressing: the composite ``"<identity>:<visitor-id>"`` recipient, or the bridge's
``sender_identity`` beside a bare visitor id. A completed send's :class:`SendOutcome` carries
that same pair, the one the message door passes to ``accept`` for the visitor, and the
transcript entry id it appended. The message door passes its provider message id as the
inbound's medium id, and the answer door forwards the id of the ``chat.answered`` entry it
writes.
"""

from __future__ import annotations

import json

import pytest
from tai42_contract.channels import ChannelAddress, ChannelAddressError, ChannelDeliveryError, ReplyOption

import tai42_channel_web.routes  # noqa: F401  (route registration side-effect)
from tai42_channel_web.channel import WebChannel

from .conftest import (
    _ANSWER,
    _MESSAGES,
    _TRANSCRIPT_KEY,
    CALLBACK,
    IDENTITY,
    SESSION_TOKEN,
    VISITOR_ID,
    FakeHttpx,
    FakeRedis,
    _answer_request,
    _handler,
    _seed_question,
    build_request,
    make_delivery,
    make_notification,
    response,
)

pytestmark = pytest.mark.usefixtures("web_env")

_COMPOSITE = f"{IDENTITY}:{VISITOR_ID}"


def _entries(fake_redis: FakeRedis) -> list[dict]:
    return [json.loads(fields["data"]) for _, fields in fake_redis.streams[_TRANSCRIPT_KEY]]


# -- resolve_address ---------------------------------------------------------------------------


def test_resolve_address_splits_a_composite_recipient():
    assert WebChannel().resolve_address(_COMPOSITE, None) == ChannelAddress(our_identity=IDENTITY, address=VISITOR_ID)


def test_resolve_address_with_a_sender_identity_takes_the_bare_visitor_id():
    assert WebChannel().resolve_address(VISITOR_ID, f"  {IDENTITY} ") == ChannelAddress(
        our_identity=IDENTITY, address=VISITOR_ID
    )


@pytest.mark.parametrize("sender_identity", [None, IDENTITY])
def test_resolve_address_with_no_recipient_is_an_address_error(sender_identity: str | None):
    with pytest.raises(ChannelAddressError, match="no default recipient"):
        WebChannel().resolve_address(None, sender_identity)


@pytest.mark.parametrize("recipient", [VISITOR_ID, f":{VISITOR_ID}", f"{IDENTITY}:", f"a:b:{VISITOR_ID}"])
def test_resolve_address_refuses_a_malformed_composite_as_an_address_error(recipient: str):
    with pytest.raises(ChannelAddressError):
        WebChannel().resolve_address(recipient, None)


def test_resolve_address_refuses_an_unusable_sender_identity_as_a_delivery_error():
    with pytest.raises(ChannelDeliveryError, match="not a usable web route identity") as refused:
        WebChannel().resolve_address(VISITOR_ID, "a:b")
    assert not isinstance(refused.value, ChannelAddressError)


# -- the outcome of a completed send -----------------------------------------------------------


async def test_a_bridge_notify_reports_the_pair_and_the_entry_it_appended(fake_redis: FakeRedis):
    outcome = await WebChannel().notify(make_notification(message="hello"))

    [entry] = _entries(fake_redis)
    assert (outcome.our_identity, outcome.address, outcome.message_ids) == (IDENTITY, VISITOR_ID, [entry["id"]])


async def test_a_composite_notify_reports_the_pair_the_message_door_passes(stub_app, registered_session: FakeRedis):
    outcome = await WebChannel().notify(make_notification(sender_identity=None, recipient=_COMPOSITE))
    target = WebChannel().resolve_address(_COMPOSITE, None)
    assert (outcome.our_identity, outcome.address) == (target.our_identity, target.address)

    resp = await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "hi"}, token=SESSION_TOKEN)
    )

    assert resp.status_code == 200
    call = stub_app.conversations.accept_calls[0]
    assert (call["our_identity"], call["client_address"]) == (outcome.our_identity, outcome.address)


async def test_a_card_notify_reports_its_one_entry(fake_redis: FakeRedis):
    outcome = await WebChannel().notify(make_notification(message="Pick one", options=[ReplyOption(text="A")]))

    [entry] = _entries(fake_redis)
    assert outcome.message_ids == [entry["id"]]


async def test_a_form_notify_reports_its_one_entry(fake_redis: FakeRedis):
    schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    outcome = await WebChannel().notify(make_notification(message="Fill this in", schema=schema))

    [entry] = _entries(fake_redis)
    assert outcome.message_ids == [entry["id"]]


async def test_a_deliver_reports_the_pair_and_the_question_entry(fake_redis: FakeRedis):
    outcome = await WebChannel().deliver(make_delivery())

    [entry] = _entries(fake_redis)
    assert entry["interaction_id"] == "int-1"
    assert (outcome.our_identity, outcome.address, outcome.message_ids) == (IDENTITY, VISITOR_ID, [entry["id"]])


# -- the medium ids --------------------------------------------------------------------------


async def test_the_message_door_passes_its_provider_message_id_as_the_medium_id(
    stub_app, registered_session: FakeRedis
):
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(
            json_body={"identity": IDENTITY, "text": "hi", "client_message_id": "abc-123_XY"}, token=SESSION_TOKEN
        )
    )

    assert resp.status_code == 200
    call = stub_app.conversations.accept_calls[0]
    assert call["medium_message_id"] == call["provider_message_id"]


async def test_a_widget_message_quotes_no_earlier_message(stub_app, registered_session: FakeRedis):
    # The widget has no reply action, so the message door hands accept no quoted id.
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(
            json_body={"identity": IDENTITY, "text": "hi", "client_message_id": "quote-check-01"}, token=SESSION_TOKEN
        )
    )

    assert resp.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["reply_to_medium_message_id"] is None


async def test_the_answer_door_forwards_the_id_of_the_answered_entry(
    stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    await _seed_question()
    fake_httpx.responses.append(response(200, json={"data": {"status": "answered"}}))

    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="staging"))

    assert resp.status_code == 200
    [answered] = _entries(registered_session)
    assert fake_httpx.calls[0] == {
        "url": CALLBACK,
        "json": {"answer": "staging", "medium_message_id": answered["id"]},
        "headers": None,
    }


# -- a sensitive answer ----------------------------------------------------------------------


async def test_a_sensitive_answer_is_recorded_as_answered_without_its_body(
    stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    await WebChannel().deliver(make_delivery(sensitive=True))
    fake_httpx.responses.append(response(200, json={"data": {"status": "answered"}}))

    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="s3cret-reply"))

    assert resp.status_code == 200
    assert fake_httpx.calls[0]["json"]["answer"] == "s3cret-reply"
    _question, answered = _entries(registered_session)
    assert answered == {
        "id": answered["id"],
        "interaction_id": "int-1",
        "ts": answered["ts"],
        "sensitive": True,
    }
    assert all("s3cret-reply" not in json.dumps(fields) for _, fields in registered_session.streams[_TRANSCRIPT_KEY])


async def test_an_ordinary_answer_keeps_its_body_on_the_answered_entry(
    stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    await WebChannel().deliver(make_delivery(sensitive=False))
    fake_httpx.responses.append(response(200, json={"data": {"status": "answered"}}))

    await _handler(stub_app, _ANSWER)(_answer_request(answer="staging"))

    _question, answered = _entries(registered_session)
    assert answered["answer"] == "staging"
    assert "sensitive" not in answered
