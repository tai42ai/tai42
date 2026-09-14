"""Tests for the conversation routing row: ``ConversationRouteCreate`` and the stored
``ConversationRoute`` — door-conditional fields, tool exprs, overrides, locale, slug."""

from __future__ import annotations

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


def test_channel_route_round_trips_and_is_frozen():
    from tai42_contract.conversations import ConversationRouteCreate

    route = ConversationRouteCreate(**_route_kwargs())
    assert route.door == "channel"
    assert route.callback_url is None
    with pytest.raises(ValidationError):
        route.target_name = "changed"


def test_api_route_callback_is_optional_https_when_set_and_forbids_channel_fields():
    from tai42_contract.conversations import ConversationRouteCreate

    # A callback is OPTIONAL: a caller reading its answer back from the poll door declares
    # none, and the route is valid.
    no_callback = ConversationRouteCreate(
        route_name="api-desk",
        door="api",
        target_kind="agent",
        target_name="assistant",
        execution_key="svc-bridge",
    )
    assert no_callback.callback_url is None
    ok = ConversationRouteCreate(
        route_name="api-desk",
        door="api",
        target_kind="agent",
        target_name="assistant",
        execution_key="svc-bridge",
        callback_url="https://host.example/hook",
    )
    assert ok.callback_url == "https://host.example/hook"
    # a declared callback must be https
    with pytest.raises(ValidationError, match="https"):
        ConversationRouteCreate(
            route_name="api-desk",
            door="api",
            target_kind="agent",
            target_name="assistant",
            execution_key="svc-bridge",
            callback_url="http://host.example/hook",
        )
    # a credential-form authority is not an acceptable https url
    with pytest.raises(ValidationError, match="https"):
        ConversationRouteCreate(
            route_name="api-desk",
            door="api",
            target_kind="agent",
            target_name="assistant",
            execution_key="svc-bridge",
            callback_url="https://user@evil.example/hook",
        )
    # a malformed authority (unterminated IPv6 literal) makes urlsplit raise → not https
    with pytest.raises(ValidationError, match="https"):
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
    from tai42_contract.template import TemplatedText

    route = ConversationRouteCreate(
        **_route_kwargs(
            target_kind="tool",
            target_name="echo",
            payload_expr={"content": "{message: .message}"},
            reply_expr={"content": ".x"},
        )
    )
    assert route.target_kind == "tool"
    assert route.payload_expr == TemplatedText(content="{message: .message}")
    assert route.reply_expr == TemplatedText(content=".x")


def test_tool_target_carries_exprs_by_id():
    from tai42_contract.conversations import ConversationRouteCreate
    from tai42_contract.template import TemplatedText

    route = ConversationRouteCreate(
        **_route_kwargs(
            target_kind="tool",
            target_name="echo",
            payload_expr={"id": "route-payload", "kwargs": {"k": "v"}},
            reply_expr={"id": "route-reply"},
        )
    )
    assert route.payload_expr == TemplatedText(id="route-payload", kwargs={"k": "v"})
    assert route.reply_expr == TemplatedText(id="route-reply")


@pytest.mark.parametrize("field", ["payload_expr", "reply_expr"])
def test_route_exprs_carry_the_jq_expression_annotation(field: str):
    # ``payload_expr``/``reply_expr`` are jq-typed templated texts, so each declares itself in
    # the generated JSON schema under the shared ``x-tai42-expression`` vendor key (language
    # jq) for a schema-driven UI to auto-render the jq editor.
    from tai42_contract.conversations import ConversationRouteCreate
    from tai42_contract.template import EXPRESSION_ANNOTATION_KEY

    prop = ConversationRouteCreate.model_json_schema()["properties"][field]
    annotation = prop[EXPRESSION_ANNOTATION_KEY]
    assert annotation["language"] == "jq"
    assert annotation["label"]


def test_route_expr_annotation_keeps_the_none_default_additive():
    # CRITICAL api-gate trap: the annotation must ride ``Annotated`` so the attribute default
    # stays the ``None`` literal — a ``Field(default=None, ...)`` redeclaration is flagged as a
    # breaking change by the griffe api-gate. The field stays optional-defaulting-None and the
    # annotation adds ONLY the vendor key to a plain declaration.
    from tai42_contract.conversations import ConversationRouteCreate
    from tai42_contract.template import EXPRESSION_ANNOTATION_KEY

    route = ConversationRouteCreate(**_route_kwargs(target_kind="tool", target_name="echo"))
    assert route.payload_expr is None
    assert route.reply_expr is None

    schema = ConversationRouteCreate.model_json_schema()
    for field in ("payload_expr", "reply_expr"):
        prop = dict(schema["properties"][field])
        prop.pop(EXPRESSION_ANNOTATION_KEY)
        # Once the vendor key is removed, the schema is a plain nullable templated text
        # defaulting None: the annotation added ONLY its key and left nullability + the None
        # default intact.
        assert prop["default"] is None
        assert prop["anyOf"] == [{"$ref": "#/$defs/TemplatedText"}, {"type": "null"}]


@pytest.mark.parametrize("field", ["payload_expr", "reply_expr"])
def test_agent_target_forbids_exprs(field: str):
    from tai42_contract.conversations import ConversationRouteCreate

    with pytest.raises(ValidationError, match="no payload_expr/reply_expr"):
        ConversationRouteCreate(**_route_kwargs(**{field: {"content": ".x"}}))


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
    # The reply is length-bounded so a single participant-facing message cannot be unbounded.
    # Exactly at the 2000-char bound is accepted; one past it is refused.
    assert ConversationRouteCreate(**_route_kwargs(error_reply_text="x" * 2000)).error_reply_text == "x" * 2000
    with pytest.raises(ValidationError):
        ConversationRouteCreate(**_route_kwargs(error_reply_text="x" * 2001))


def test_route_locale_defaults_to_none_round_trips_and_canonicalizes():
    from tai42_contract.conversations import ConversationRoute, ConversationRouteCreate

    # Absent by default: the route declares no default language.
    assert ConversationRouteCreate(**_route_kwargs()).locale is None
    # A supplied tag is stored through the one locale seam, canonicalized like every carrier.
    assert ConversationRouteCreate(**_route_kwargs(locale="he-il")).locale == "he-IL"
    # It round-trips onto the stored row and survives a JSON persist cycle unchanged.
    stored = ConversationRoute(**_route_kwargs(locale="fr"), execution_key_fingerprint="fp")
    assert stored.locale == "fr"
    assert ConversationRoute.model_validate_json(stored.model_dump_json()).locale == "fr"


def test_route_locale_rejects_a_malformed_tag_loudly():
    from tai42_contract.conversations import ConversationRouteCreate

    # A malformed tag is rejected at the boundary — never silently dropped or guessed.
    with pytest.raises(ValidationError):
        ConversationRouteCreate(**_route_kwargs(locale="not a locale"))


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
