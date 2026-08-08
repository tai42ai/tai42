"""Tests for the conversation bridge contract types + the ``AppConversations``
facet shape.

Facet membership is covered by the frozen-facade partition test in
``test_contract.py``; here we pin the facet method signatures, the ``DeliveryReceipt``
enum, and the models' frozen + field validation (slug, door-conditional fields, https
callback).
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from pydantic import ValidationError


def _route_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "route_name": "support-sms",
        "door": "channel",
        "target_kind": "agent",
        "target_name": "concierge",
        "execution_key": "svc-bridge",
        "channel": "twilio",
        "our_identity": "+15550001111",
    }
    base.update(overrides)
    return base


# -- DeliveryReceipt enum -------------------------------------------------------


def test_delivery_receipt_has_the_two_terminal_outcomes():
    from tai42_contract.conversations import DeliveryReceipt

    assert {m.value for m in DeliveryReceipt} == {"delivered", "failed"}
    assert DeliveryReceipt.DELIVERED == "delivered"


# -- ConversationMessage --------------------------------------------------------


def test_conversation_message_is_frozen_and_carries_the_inbound_body():
    from tai42_contract.conversations import ConversationMessage

    msg = ConversationMessage(external_user_id="u-1", text="hello there")
    assert msg.external_user_id == "u-1"
    with pytest.raises(ValidationError):
        msg.text = "changed"


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_conversation_message_rejects_blank_fields(blank: str):
    from tai42_contract.conversations import ConversationMessage

    with pytest.raises(ValidationError):
        ConversationMessage(external_user_id=blank, text="hi")
    with pytest.raises(ValidationError):
        ConversationMessage(external_user_id="u-1", text=blank)


# -- ConversationAnswer ---------------------------------------------------------


def test_conversation_answer_is_frozen_and_carries_status():
    from tai42_contract.conversations import ConversationAnswer

    answer = ConversationAnswer(message_id="m-1", thread_id="bridge:r:u", status="answered", answer="the reply")
    assert answer.status == "answered"
    with pytest.raises(ValidationError):
        answer.answer = "changed"


def test_conversation_answer_rejects_unknown_status():
    from tai42_contract.conversations import ConversationAnswer

    bad: dict[str, Any] = {"message_id": "m-1", "thread_id": "t", "status": "maybe", "answer": "x"}
    with pytest.raises(ValidationError):
        ConversationAnswer(**bad)


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_conversation_answer_rejects_blank_answer(blank: str):
    from tai42_contract.conversations import ConversationAnswer

    with pytest.raises(ValidationError):
        ConversationAnswer(message_id="m-1", thread_id="t", status="error", answer=blank)


@pytest.mark.parametrize("status", ["answered", "error"])
def test_conversation_answer_requires_answer_text_for_non_silent(status: str):
    from tai42_contract.conversations import ConversationAnswer

    body: dict[str, Any] = {"message_id": "m-1", "thread_id": "t", "status": status, "answer": None}
    with pytest.raises(ValidationError, match="non-blank"):
        ConversationAnswer(**body)


def test_conversation_answer_silent_carries_no_answer_text():
    from tai42_contract.conversations import ConversationAnswer

    silent = ConversationAnswer(message_id="m-1", thread_id="t", status="silent")
    assert silent.status == "silent"
    assert silent.answer is None


@pytest.mark.parametrize("answer", ["some text", "", "   "])
def test_conversation_answer_silent_rejects_any_answer_field(answer: str):
    from tai42_contract.conversations import ConversationAnswer

    with pytest.raises(ValidationError, match="silent answer carries no answer text"):
        ConversationAnswer(message_id="m-1", thread_id="t", status="silent", answer=answer)


# -- Route rows -----------------------------------------------------------------


def test_channel_route_round_trips_and_is_frozen():
    from tai42_contract.conversations import ConversationRouteCreate

    route = ConversationRouteCreate(**_route_kwargs())
    assert route.door == "channel"
    assert route.callback_url is None
    with pytest.raises(ValidationError):
        route.target_name = "changed"


def test_api_route_requires_https_callback_and_forbids_channel_fields():
    from tai42_contract.conversations import ConversationRouteCreate

    ok = ConversationRouteCreate(
        route_name="api-desk",
        door="api",
        target_kind="agent",
        target_name="concierge",
        execution_key="svc-bridge",
        callback_url="https://host.example/hook",
    )
    assert ok.callback_url == "https://host.example/hook"
    # non-https callback rejected
    with pytest.raises(ValidationError, match="https callback_url"):
        ConversationRouteCreate(
            route_name="api-desk",
            door="api",
            target_kind="agent",
            target_name="concierge",
            execution_key="svc-bridge",
            callback_url="http://host.example/hook",
        )
    # a credential-form authority is not an acceptable https url
    with pytest.raises(ValidationError, match="https callback_url"):
        ConversationRouteCreate(
            route_name="api-desk",
            door="api",
            target_kind="agent",
            target_name="concierge",
            execution_key="svc-bridge",
            callback_url="https://user@evil.example/hook",
        )
    # a malformed authority (unterminated IPv6 literal) makes urlsplit raise → not https
    with pytest.raises(ValidationError, match="https callback_url"):
        ConversationRouteCreate(
            route_name="api-desk",
            door="api",
            target_kind="agent",
            target_name="concierge",
            execution_key="svc-bridge",
            callback_url="https://[::1/hook",
        )
    # api rows carry no channel/our_identity
    with pytest.raises(ValidationError, match="no channel"):
        ConversationRouteCreate(
            route_name="api-desk",
            door="api",
            target_kind="agent",
            target_name="concierge",
            execution_key="svc-bridge",
            callback_url="https://host.example/hook",
            channel="twilio",
        )
    with pytest.raises(ValidationError, match="no our_identity"):
        ConversationRouteCreate(
            route_name="api-desk",
            door="api",
            target_kind="agent",
            target_name="concierge",
            execution_key="svc-bridge",
            callback_url="https://host.example/hook",
            our_identity="+15550001111",
        )


def test_channel_route_requires_channel_and_identity_forbids_callback():
    from tai42_contract.conversations import ConversationRouteCreate

    with pytest.raises(ValidationError, match="non-blank channel"):
        ConversationRouteCreate(**_route_kwargs(channel=None))
    with pytest.raises(ValidationError, match="non-blank our_identity"):
        ConversationRouteCreate(**_route_kwargs(our_identity=None))
    with pytest.raises(ValidationError, match="no callback_url"):
        ConversationRouteCreate(**_route_kwargs(callback_url="https://host.example/hook"))


def test_tool_target_carries_optional_exprs():
    from tai42_contract.conversations import ConversationRouteCreate

    route = ConversationRouteCreate(
        **_route_kwargs(target_kind="tool", target_name="echo", payload_expr="{message: .message}", reply_expr=".x")
    )
    assert route.target_kind == "tool"
    assert route.payload_expr == "{message: .message}"
    assert route.reply_expr == ".x"


@pytest.mark.parametrize("field", ["payload_expr", "reply_expr"])
def test_agent_target_forbids_exprs(field: str):
    from tai42_contract.conversations import ConversationRouteCreate

    with pytest.raises(ValidationError, match="no payload_expr/reply_expr"):
        ConversationRouteCreate(**_route_kwargs(**{field: ".x"}))


def test_blank_inbound_text_error_is_a_value_error():
    from tai42_contract.conversations import BlankInboundTextError

    assert issubclass(BlankInboundTextError, ValueError)


def test_channel_route_rejects_a_colon_in_the_channel_name():
    from tai42_contract.conversations import ConversationRouteCreate

    # The channel name prefixes the dedupe/outbound-index keys; a ``:`` in it would let
    # one (channel, provider id) pair read another's entry.
    with pytest.raises(ValidationError, match="free of ':'"):
        ConversationRouteCreate(**_route_kwargs(channel="twi:lio"))


@pytest.mark.parametrize("bad", ["Support", "support sms", "support:sms", "support/sms", "", "café"])
def test_route_name_rejects_non_slug(bad: str):
    from tai42_contract.conversations import ConversationRouteCreate

    with pytest.raises(ValidationError, match="route_name"):
        ConversationRouteCreate(**_route_kwargs(route_name=bad))


def test_stored_route_adds_the_two_server_derived_fields():
    from tai42_contract.conversations import ConversationRoute, ConversationRouteCreate

    stored = ConversationRoute(**_route_kwargs(), execution_key_fingerprint="fp-abc")
    assert stored.execution_key_fingerprint == "fp-abc"
    assert stored.callback_secret is None
    # the stored row is the create shape plus exactly the two derived fields
    assert set(ConversationRoute.model_fields) - set(ConversationRouteCreate.model_fields) == {
        "callback_secret",
        "execution_key_fingerprint",
    }
    # the fingerprint is required on the stored row
    with pytest.raises(ValidationError):
        ConversationRoute(**_route_kwargs())


# -- AppConversations facet shape ----------------------------------------------


def test_app_conversations_is_runtime_checkable_and_shaped():
    from tai42_contract.app import AppConversations

    class _Ok:
        async def accept(
            self, channel: str, our_identity: str, client_address: str, text: str, provider_message_id: str
        ) -> str:
            return "m-1"

        async def record_delivery_status(self, channel: str, provider_message_id: str, status: object) -> None:
            return None

    class _Missing:
        async def accept(
            self, channel: str, our_identity: str, client_address: str, text: str, provider_message_id: str
        ) -> str:
            return "m-1"

    assert isinstance(_Ok(), AppConversations)
    assert not isinstance(_Missing(), AppConversations)


def test_facet_methods_are_coroutines_with_the_expected_parameters():
    from tai42_contract.app import AppConversations

    assert inspect.iscoroutinefunction(AppConversations.accept)
    assert list(inspect.signature(AppConversations.accept).parameters) == [
        "self",
        "channel",
        "our_identity",
        "client_address",
        "cap_key",
        "text",
        "provider_message_id",
    ]
    assert inspect.iscoroutinefunction(AppConversations.record_delivery_status)
    assert list(inspect.signature(AppConversations.record_delivery_status).parameters) == [
        "self",
        "channel",
        "provider_message_id",
        "status",
    ]
