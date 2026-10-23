"""The quoted message's id: a message's ``context.id`` reaches the platform as
``reply_to_medium_message_id`` on every arm — the inbound-answer ladder's bridge and the
conversation bridge's ``accept``."""

from __future__ import annotations

from typing import Any

import pytest
from tai42_contract.channels import InboundAnswerOutcome

import tai42_channel_whatsapp.inbound  # noqa: F401  (route registration side-effect)

from .conftest import (
    _WAMID,
    WA_ID,
    FakeRedis,
    _params_envelope,
    _seed_pending,
    _seed_pending_select,
    signed_request,
)

pytestmark = pytest.mark.usefixtures("whatsapp_env")

_QUOTED = "wamid.QUOTED"
_LADDER_ARMS = [InboundAnswerOutcome.FORWARDED, InboundAnswerOutcome.BRIDGED]


def _quoting(message: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """``message`` as a reply to ``_QUOTED`` (or carrying the given raw ``context``)."""
    return {**message, "context": {"from": "15551112222", "id": _QUOTED} if context is None else context}


def _text(body: str = "yes please") -> dict[str, Any]:
    return {"id": _WAMID, "from": WA_ID, "type": "text", "text": {"body": body}}


def _tap(reply_type: str, reply_id: str = "int-1:0", title: str = "staging") -> dict[str, Any]:
    return {
        "id": _WAMID,
        "from": WA_ID,
        "type": "interactive",
        "interactive": {"type": reply_type, reply_type: {"id": reply_id, "title": title}},
    }


def _template_button() -> dict[str, Any]:
    return {"id": _WAMID, "from": WA_ID, "type": "button", "button": {"text": "Confirm", "payload": "CONFIRM_42"}}


# --- the inbound-answer ladder arm ------------------------------------------------------------


@pytest.mark.parametrize("outcome", _LADDER_ARMS)
async def test_a_quoting_text_answer_hands_the_ladder_bridge_the_quoted_id(
    handler, channels, fake_redis: FakeRedis, outcome
):
    await _seed_pending()
    channels.inbound_outcome = outcome

    result = await handler(signed_request(_params_envelope(_quoting(_text()))))

    assert result.status_code == 200
    (call,) = channels.inbound_calls
    assert call.answer == "yes please"
    assert call.bridge.reply_to_medium_message_id == _QUOTED


@pytest.mark.parametrize("outcome", _LADDER_ARMS)
@pytest.mark.parametrize("reply_type", ["button_reply", "list_reply"])
async def test_a_tap_answering_a_pending_select_hands_the_ladder_bridge_the_tapped_message_id(
    handler, channels, fake_redis: FakeRedis, outcome, reply_type
):
    await _seed_pending_select(["staging", "prod"])
    channels.inbound_outcome = outcome

    result = await handler(signed_request(_params_envelope(_quoting(_tap(reply_type)))))

    assert result.status_code == 200
    (call,) = channels.inbound_calls
    assert call.bridge.reply_to_medium_message_id == _QUOTED


@pytest.mark.parametrize("outcome", _LADDER_ARMS)
async def test_a_template_button_answer_hands_the_ladder_bridge_the_quoted_id(
    handler, channels, fake_redis: FakeRedis, outcome
):
    await _seed_pending()
    channels.inbound_outcome = outcome

    result = await handler(signed_request(_params_envelope(_quoting(_template_button()))))

    assert result.status_code == 200
    (call,) = channels.inbound_calls
    assert call.answer == "Confirm"
    assert call.bridge.reply_to_medium_message_id == _QUOTED


async def test_an_answer_quoting_nothing_hands_the_ladder_bridge_no_quoted_id(handler, channels, fake_redis: FakeRedis):
    await _seed_pending()
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED

    await handler(signed_request(_params_envelope(_text())))

    (call,) = channels.inbound_calls
    assert call.bridge.reply_to_medium_message_id is None


# --- the conversation bridge arm --------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        _text(),
        _tap("button_reply", reply_id="int-9:2"),
        _tap("list_reply", reply_id="int-3:0"),
        _template_button(),
        {"id": _WAMID, "from": WA_ID, "type": "location", "location": {"latitude": 1.5, "longitude": 2.5}},
        {"id": _WAMID, "from": WA_ID, "type": "contacts", "contacts": [{"name": {"formatted_name": "Jane Doe"}}]},
        {"id": _WAMID, "from": WA_ID, "type": "reaction", "reaction": {"message_id": "wamid.T", "emoji": "👍"}},
    ],
    ids=["text", "button_reply", "list_reply", "template_button", "location", "contacts", "reaction"],
)
async def test_a_quoting_message_with_no_pending_ask_hands_accept_the_quoted_id(
    handler, stub_app, fake_redis: FakeRedis, message
):
    result = await handler(signed_request(_params_envelope(_quoting(message))))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["reply_to_medium_message_id"] == _QUOTED


async def test_a_text_answer_whose_ask_is_gone_at_the_ladder_bridges_with_the_quoted_id(
    handler, stub_app, channels, fake_redis: FakeRedis
):
    await _seed_pending()
    channels.inbound_outcome = InboundAnswerOutcome.NO_CORRELATION

    await handler(signed_request(_params_envelope(_quoting(_text()))))

    (call,) = stub_app.conversations.accept_calls
    assert call["reply_to_medium_message_id"] == _QUOTED


async def test_a_message_quoting_nothing_hands_accept_no_quoted_id(handler, stub_app, fake_redis: FakeRedis):
    await handler(signed_request(_params_envelope(_text())))

    (call,) = stub_app.conversations.accept_calls
    assert call["reply_to_medium_message_id"] is None


@pytest.mark.parametrize(
    "context",
    [{"id": ""}, {"id": "   "}, {"id": 42}, {"from": "15551112222"}, "wamid.QUOTED", []],
    ids=["empty", "blank", "non_string", "no_id", "not_an_object", "list"],
)
async def test_a_context_without_a_usable_id_quotes_nothing(handler, stub_app, fake_redis: FakeRedis, context):
    result = await handler(signed_request(_params_envelope(_quoting(_text(), context))))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["reply_to_medium_message_id"] is None


async def test_a_quoting_tap_that_answers_nothing_pending_bridges_with_the_quoted_id(
    handler, stub_app, channels, fake_redis: FakeRedis
):
    # A tap whose id maps to no option of the pending select bridges its title, quoting the tapped message.
    await _seed_pending_select(["staging", "prod"])

    await handler(signed_request(_params_envelope(_quoting(_tap("button_reply", reply_id="int-1:7")))))

    assert channels.inbound_calls == []
    (call,) = stub_app.conversations.accept_calls
    assert call["reply_to_medium_message_id"] == _QUOTED
