"""Tests for the conversation bridge contract types + the ``AppConversations``
facet shape.

Facet membership is covered by the frozen-facade partition test in
``test_contract.py``; here we pin the facet method signatures, the ``DeliveryReceipt``
enum, and the models' frozen + field validation (slug, door-conditional fields, https
callback).
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError


def _route_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "route_name": "chat-sms",
        "door": "channel",
        "target_kind": "agent",
        "target_name": "assistant",
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


# -- validate_entry_params ------------------------------------------------------


def test_validate_entry_params_returns_a_clean_dict_unchanged():
    from tai42_contract.conversations import validate_entry_params

    clean = {"token": "abc-123", "ref": "x_9", "Mixed-CASE": "v"}
    assert validate_entry_params(clean) is clean


def test_validate_entry_params_accepts_the_empty_dict():
    from tai42_contract.conversations import validate_entry_params

    assert validate_entry_params({}) == {}


def test_validate_entry_params_refuses_too_many_keys():
    from tai42_contract.conversations import ENTRY_PARAMS_MAX_COUNT, validate_entry_params

    too_many = {f"k{i}": "v" for i in range(ENTRY_PARAMS_MAX_COUNT + 1)}
    with pytest.raises(ValueError, match=f"over the {ENTRY_PARAMS_MAX_COUNT} allowed"):
        validate_entry_params(too_many)


@pytest.mark.parametrize("bad_key", ["has space", "sym$bol", "dot.dot", "slash/es", "", "café"])
def test_validate_entry_params_refuses_a_bad_key_charset(bad_key: str):
    from tai42_contract.conversations import validate_entry_params

    with pytest.raises(ValueError, match="must match"):
        validate_entry_params({bad_key: "v"})


def test_validate_entry_params_refuses_an_over_long_key():
    from tai42_contract.conversations import validate_entry_params

    with pytest.raises(ValueError, match="must match"):
        validate_entry_params({"k" * 65: "v"})


def test_validate_entry_params_refuses_a_non_str_value():
    from tai42_contract.conversations import validate_entry_params

    with pytest.raises(ValueError, match="must be a string"):
        validate_entry_params({"k": 1})  # type: ignore[dict-item]


def test_validate_entry_params_refuses_an_over_long_value():
    from tai42_contract.conversations import ENTRY_PARAM_VALUE_MAX_CHARS, validate_entry_params

    with pytest.raises(ValueError, match="character limit"):
        validate_entry_params({"k": "v" * (ENTRY_PARAM_VALUE_MAX_CHARS + 1)})


def test_validate_entry_params_refuses_an_over_size_total():
    from tai42_contract.conversations import ENTRY_PARAM_VALUE_MAX_CHARS, validate_entry_params

    # Each value is under the per-value cap, but enough of them push the serialized total
    # over the byte budget — the bound the transport cap is really about.
    over_total = {f"k{i:02d}": "v" * ENTRY_PARAM_VALUE_MAX_CHARS for i in range(8)}
    with pytest.raises(ValueError, match="bytes, over"):
        validate_entry_params(over_total)


def test_validate_entry_params_error_never_carries_a_value():
    from tai42_contract.conversations import validate_entry_params

    secret = "super-secret-token-value"
    with pytest.raises(ValueError, match="character limit") as excinfo:
        validate_entry_params({"k": secret + "x" * 512})
    assert secret not in str(excinfo.value)


def test_conversation_message_accepts_none_and_valid_params():
    from tai42_contract.conversations import ConversationMessage

    assert ConversationMessage(external_user_id="u-1", text="hi").params is None
    msg = ConversationMessage(external_user_id="u-1", text="hi", params={"token": "abc"})
    assert msg.params == {"token": "abc"}


def test_conversation_message_refuses_invalid_params():
    from tai42_contract.conversations import ConversationMessage

    with pytest.raises(ValidationError):
        ConversationMessage(external_user_id="u-1", text="hi", params={"bad key": "v"})


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
        target_name="assistant",
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
            target_name="assistant",
            execution_key="svc-bridge",
            callback_url="http://host.example/hook",
        )
    # a credential-form authority is not an acceptable https url
    with pytest.raises(ValidationError, match="https callback_url"):
        ConversationRouteCreate(
            route_name="api-desk",
            door="api",
            target_kind="agent",
            target_name="assistant",
            execution_key="svc-bridge",
            callback_url="https://user@evil.example/hook",
        )
    # a malformed authority (unterminated IPv6 literal) makes urlsplit raise → not https
    with pytest.raises(ValidationError, match="https callback_url"):
        ConversationRouteCreate(
            route_name="api-desk",
            door="api",
            target_kind="agent",
            target_name="assistant",
            execution_key="svc-bridge",
            callback_url="https://[::1/hook",
        )
    # api rows carry no channel/our_identity
    with pytest.raises(ValidationError, match="no channel"):
        ConversationRouteCreate(
            route_name="api-desk",
            door="api",
            target_kind="agent",
            target_name="assistant",
            execution_key="svc-bridge",
            callback_url="https://host.example/hook",
            channel="twilio",
        )
    with pytest.raises(ValidationError, match="no our_identity"):
        ConversationRouteCreate(
            route_name="api-desk",
            door="api",
            target_kind="agent",
            target_name="assistant",
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


def test_turns_per_hour_override_defaults_to_none_and_must_be_positive():
    from tai42_contract.conversations import ConversationRouteCreate

    # Absent by default: the route runs at the global per-address cap.
    assert ConversationRouteCreate(**_route_kwargs()).turns_per_hour_override is None
    # A positive per-hour override is accepted and preserved.
    assert ConversationRouteCreate(**_route_kwargs(turns_per_hour_override=250)).turns_per_hour_override == 250
    # Non-positive rates are refused.
    for bad in (0, -5):
        with pytest.raises(ValidationError):
            ConversationRouteCreate(**_route_kwargs(turns_per_hour_override=bad))


def test_error_reply_text_defaults_to_none_and_must_be_non_blank_and_bounded():
    from tai42_contract.conversations import ConversationRouteCreate

    # Absent by default: a failed turn falls back to the built-in default reply.
    assert ConversationRouteCreate(**_route_kwargs()).error_reply_text is None
    # A non-blank custom reply is accepted and preserved verbatim.
    text = "Lo sentimos, algo salió mal. Inténtalo de nuevo."
    assert ConversationRouteCreate(**_route_kwargs(error_reply_text=text)).error_reply_text == text
    # An empty reply is refused by the min-length bound.
    with pytest.raises(ValidationError):
        ConversationRouteCreate(**_route_kwargs(error_reply_text=""))
    # A whitespace-only reply is refused as blank by the non-blank validator.
    with pytest.raises(ValidationError, match="non-blank"):
        ConversationRouteCreate(**_route_kwargs(error_reply_text="   "))
    # The reply is length-bounded so a single guest-facing message cannot be unbounded.
    # Exactly at the 2000-char bound is accepted; one past it is refused.
    assert ConversationRouteCreate(**_route_kwargs(error_reply_text="x" * 2000)).error_reply_text == "x" * 2000
    with pytest.raises(ValidationError):
        ConversationRouteCreate(**_route_kwargs(error_reply_text="x" * 2001))


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
        "params",
    ]
    assert inspect.iscoroutinefunction(AppConversations.record_delivery_status)
    assert list(inspect.signature(AppConversations.record_delivery_status).parameters) == [
        "self",
        "channel",
        "provider_message_id",
        "status",
    ]


# -- person linking models ------------------------------------------------------


def _address_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "door": "channel",
        "routes": ["line"],
        "channel": "twilio",
        "our_identity": "+15550001111",
        "address": "+15550002222",
        "linked_at": datetime(2026, 8, 8, 12, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return base


def test_person_address_channel_row_carries_channel_and_identity():
    from tai42_contract.conversations import PersonAddress

    addr = PersonAddress(**_address_kwargs())
    assert addr.channel == "twilio"
    assert addr.our_identity == "+15550001111"
    with pytest.raises(ValidationError):
        addr.address = "changed"  # frozen


def test_person_address_api_row_carries_no_channel_identity():
    from tai42_contract.conversations import PersonAddress

    addr = PersonAddress(**_address_kwargs(door="api", channel=None, our_identity=None, address="svc/end"))
    assert addr.channel is None
    assert addr.our_identity is None
    # An api row that smuggles a channel/identity is refused.
    with pytest.raises(ValidationError):
        PersonAddress(**_address_kwargs(door="api", channel="twilio", our_identity=None, address="svc/end"))


def test_person_address_channel_row_requires_channel_and_identity():
    from tai42_contract.conversations import PersonAddress

    with pytest.raises(ValidationError):
        PersonAddress(**_address_kwargs(channel=None))
    with pytest.raises(ValidationError):
        PersonAddress(**_address_kwargs(our_identity="   "))


@pytest.mark.parametrize("routes", [[], ["", "line"], ["dup", "dup"]])
def test_person_address_routes_must_be_non_empty_non_blank_and_unique(routes: list[str]):
    from tai42_contract.conversations import PersonAddress

    with pytest.raises(ValidationError):
        PersonAddress(**_address_kwargs(routes=routes))


def test_person_address_serializes_linked_at_as_isoformat():
    from tai42_contract.conversations import PersonAddress

    addr = PersonAddress(**_address_kwargs())
    assert '"linked_at":"2026-08-08T12:00:00+00:00"' in addr.model_dump_json()


def test_person_requires_at_least_one_address_and_serializes_sortable_created_at():
    from tai42_contract.conversations import Person, PersonAddress

    with pytest.raises(ValidationError):
        Person(
            person_id="p1",
            target_kind="agent",
            target_name="assistant",
            created_at=datetime(2026, 8, 8, 12, 0, tzinfo=UTC),
            addresses=[],
        )
    person = Person(
        person_id="p1",
        target_kind="agent",
        target_name="assistant",
        created_at=datetime(2026, 8, 8, 12, 0, tzinfo=UTC),
        addresses=[PersonAddress(**_address_kwargs())],
    )
    # ``+00:00`` (isoformat), never the default ``Z`` — the merge survivor rule compares this
    # string lexically.
    assert '"created_at":"2026-08-08T12:00:00+00:00"' in person.model_dump_json()


def test_person_created_at_rejects_naive_and_normalizes_non_utc_to_offset():
    from tai42_contract.conversations import Person, PersonAddress

    # A naive created_at has no offset and would sort before every ``+00:00`` — reject it,
    # so the merge survivor rule never compares an offset-less string.
    with pytest.raises(ValidationError, match="timezone-aware"):
        Person(
            person_id="p1",
            target_kind="agent",
            target_name="assistant",
            created_at=datetime(2026, 8, 8, 12, 0),  # naive
            addresses=[PersonAddress(**_address_kwargs())],
        )
    # A non-UTC aware value is normalized to UTC, so the serialized form still carries the
    # canonical ``+00:00`` offset and stays lexically sortable against every other row.
    eastern = timezone(timedelta(hours=-5))
    person = Person(
        person_id="p1",
        target_kind="agent",
        target_name="assistant",
        created_at=datetime(2026, 8, 8, 7, 0, tzinfo=eastern),  # == 12:00 UTC
        addresses=[PersonAddress(**_address_kwargs())],
    )
    assert person.created_at.utcoffset() == timedelta(0)
    assert '"created_at":"2026-08-08T12:00:00+00:00"' in person.model_dump_json()


def test_person_address_linked_at_rejects_naive_and_normalizes_non_utc_to_offset():
    from tai42_contract.conversations import PersonAddress

    with pytest.raises(ValidationError, match="timezone-aware"):
        PersonAddress(**_address_kwargs(linked_at=datetime(2026, 8, 8, 12, 0)))  # naive
    eastern = timezone(timedelta(hours=-5))
    addr = PersonAddress(**_address_kwargs(linked_at=datetime(2026, 8, 8, 7, 0, tzinfo=eastern)))
    assert addr.linked_at.utcoffset() == timedelta(0)
    assert '"linked_at":"2026-08-08T12:00:00+00:00"' in addr.model_dump_json()


def test_pairing_errors_are_distinct_named_types():
    from tai42_contract.conversations import (
        CrossTargetMergeError,
        MultichannelDisabledError,
        NotLinkedError,
        PairCodeInvalidError,
    )

    for exc in (CrossTargetMergeError, MultichannelDisabledError, NotLinkedError, PairCodeInvalidError):
        assert issubclass(exc, Exception)
    # Each is its own type — a pairing turn scopes them apart from one another and from infra.
    assert len({CrossTargetMergeError, MultichannelDisabledError, NotLinkedError, PairCodeInvalidError}) == 4


# -- TargetConversationConfig ---------------------------------------------------


def test_target_config_defaults_and_frozen():
    from tai42_contract.conversations import TargetConversationConfig

    config = TargetConversationConfig(target_kind="agent", target_name="assistant")
    assert config.multichannel is False
    assert config.greeting_template is None
    with pytest.raises(ValidationError):
        config.multichannel = True  # type: ignore[misc]


def test_target_config_accepts_the_pairing_code_placeholder():
    from tai42_contract.conversations import TargetConversationConfig

    config = TargetConversationConfig(
        target_kind="tool", target_name="lookup", greeting_template="Welcome! Pair with {pairing_code}."
    )
    assert config.greeting_template == "Welcome! Pair with {pairing_code}."


def test_target_config_allows_escaped_braces():
    from tai42_contract.conversations import TargetConversationConfig

    # ``{{`` / ``}}`` are literal braces, not a placeholder, so they are allowed.
    config = TargetConversationConfig(target_kind="agent", target_name="c", greeting_template="use {{curly}} here")
    assert config.greeting_template == "use {{curly}} here"


@pytest.mark.parametrize(
    "template",
    [
        "hi {name}",  # a foreign field
        "hi {}",  # an auto-numbered field
        "hi {pairing_code!r}",  # a conversion
        "hi {pairing_code:>10}",  # a format spec
        "hi {pairing_code.attr}",  # an attribute access
        "hi {unbalanced",  # a malformed template
    ],
)
def test_target_config_refuses_a_disallowed_greeting_template(template: str):
    from tai42_contract.conversations import TargetConversationConfig

    with pytest.raises(ValidationError):
        TargetConversationConfig(target_kind="agent", target_name="c", greeting_template=template)


def test_target_config_refuses_a_blank_greeting_template():
    from tai42_contract.conversations import TargetConversationConfig

    with pytest.raises(ValidationError):
        TargetConversationConfig(target_kind="agent", target_name="c", greeting_template="   ")


def test_target_config_refuses_a_blank_target_name():
    from tai42_contract.conversations import TargetConversationConfig

    with pytest.raises(ValidationError):
        TargetConversationConfig(target_kind="agent", target_name="   ")


def test_target_config_refuses_an_unknown_target_kind():
    from tai42_contract.conversations import TargetConversationConfig

    with pytest.raises(ValidationError):
        TargetConversationConfig(target_kind="robot", target_name="c")  # type: ignore[arg-type]
