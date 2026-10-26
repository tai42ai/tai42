"""Tests for the conversation routing row: the door-discriminated create union and the stored
row — door-exclusive fields, the published patterns, tool exprs, overrides, locale, slug."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from pydantic import ValidationError

from tai42_contract.conversations import (
    HTTPS_CALLBACK_PATTERN,
    ApiConversationRoute,
    ApiConversationRouteCreate,
    ChannelConversationRoute,
    ChannelConversationRouteCreate,
    ConversationRouteBody,
    ConversationRouteCreateBody,
    ReplyTexts,
)
from tai42_contract.conversations.routes import _require_https_url  # pyright: ignore[reportPrivateUsage]


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


def _api_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "route_name": "api-assistant",
        "door": "api",
        "target_kind": "agent",
        "target_name": "assistant",
        "execution_key": "svc-bridge",
    }
    base.update(overrides)
    return base


def _create(body: dict[str, Any]) -> ApiConversationRouteCreate | ChannelConversationRouteCreate:
    return ConversationRouteCreateBody.model_validate(body).root


def _errors(caught: pytest.ExceptionInfo[ValidationError]) -> list[tuple[str, tuple[Any, ...]]]:
    return [(error["type"], error["loc"]) for error in caught.value.errors()]


def test_channel_route_round_trips_and_is_frozen():
    route = _create(_route_kwargs())
    assert isinstance(route, ChannelConversationRouteCreate)
    assert route.door == "channel"
    assert route.callback_url is None
    with pytest.raises(ValidationError):
        route.target_name = "changed"


def test_api_route_callback_is_optional_https_when_set_and_forbids_channel_fields():
    # A callback is OPTIONAL: a caller reading its answer back from the poll door declares
    # none, and the route is valid.
    no_callback = _create(_api_kwargs())
    assert isinstance(no_callback, ApiConversationRouteCreate)
    assert no_callback.callback_url is None
    assert no_callback.channel is None
    assert no_callback.our_identity is None
    ok = _create(_api_kwargs(callback_url="https://host.example/hook"))
    assert ok.callback_url == "https://host.example/hook"
    # a declared callback must be https, with no credential-form authority
    for bad in ("http://host.example/hook", "https://user@evil.example/hook"):
        with pytest.raises(ValidationError) as caught:
            _create(_api_kwargs(callback_url=bad))
        assert _errors(caught) == [("string_pattern_mismatch", ("api", "callback_url"))]
    # an authority the pattern admits but urlsplit refuses (an unterminated IPv6 literal, an
    # empty hostname) is refused by the server-side check
    for residual in ("https://[::1/hook", "https://:443"):
        with pytest.raises(ValidationError, match="callback_url must be an absolute https URL with no userinfo"):
            _create(_api_kwargs(callback_url=residual))
    # api rows carry no channel/our_identity
    for field, value in (("channel", "twilio"), ("our_identity", "+15550001111")):
        with pytest.raises(ValidationError) as caught:
            _create(_api_kwargs(**{field: value}))
        assert _errors(caught) == [("none_required", ("api", field))]


def test_channel_route_requires_channel_and_identity_forbids_callback():
    for field in ("channel", "our_identity"):
        with pytest.raises(ValidationError) as caught:
            _create(_route_kwargs(**{field: None}))
        assert _errors(caught) == [("string_type", ("channel", field))]
    for field, blank in (("channel", "  "), ("our_identity", " \t")):
        with pytest.raises(ValidationError) as caught:
            _create(_route_kwargs(**{field: blank}))
        assert _errors(caught) == [("string_pattern_mismatch", ("channel", field))]
    with pytest.raises(ValidationError) as caught:
        _create(_route_kwargs(callback_url="https://host.example/hook"))
    assert _errors(caught) == [("none_required", ("channel", "callback_url"))]


def test_an_unknown_door_is_refused_by_the_discriminator():
    with pytest.raises(ValidationError) as caught:
        _create(_route_kwargs(door="email"))
    assert _errors(caught) == [("union_tag_invalid", ())]


_DOOR_EXPR_FIELDS = ("start_expr", "cancel_expr", "resume_expr", "extras_expr", "reply_expr")


def test_tool_target_carries_optional_exprs():
    from tai42_contract.template import TemplatedText

    route = _create(
        _route_kwargs(
            target_kind="tool",
            target_name="echo",
            start_expr={"content": "{message: .message}"},
            cancel_expr={"content": "$parked | map(.id)"},
            resume_expr={"content": "null"},
            extras_expr={"content": "{k: 1}"},
            reply_expr={"content": ".x"},
        )
    )
    assert route.target_kind == "tool"
    assert route.start_expr == TemplatedText(content="{message: .message}")
    assert route.cancel_expr == TemplatedText(content="$parked | map(.id)")
    assert route.extras_expr == TemplatedText(content="{k: 1}")
    assert route.reply_expr == TemplatedText(content=".x")


def test_tool_target_carries_exprs_by_id():
    from tai42_contract.template import TemplatedText

    route = _create(
        _route_kwargs(
            target_kind="tool",
            target_name="echo",
            start_expr={"id": "route-start", "kwargs": {"k": "v"}},
            reply_expr={"id": "route-reply"},
        )
    )
    assert route.start_expr == TemplatedText(id="route-start", kwargs={"k": "v"})
    assert route.reply_expr == TemplatedText(id="route-reply")


def test_agent_target_carries_the_door_exprs():
    # An agent target may carry the parkable-door jqs and a reply_expr: the run's start kwargs, its
    # cancel/resume over parked interactions, its extras, and the reply mapping are all valid on an
    # agent route (the turn engine evaluates them the same way it does for a tool target).
    from tai42_contract.template import TemplatedText

    route = _create(
        _route_kwargs(
            target_kind="agent",
            target_name="assistant",
            start_expr={"content": "{user_message: {content: .message}}"},
            reply_expr={"content": ".answer"},
        )
    )
    assert route.start_expr == TemplatedText(content="{user_message: {content: .message}}")
    assert route.reply_expr == TemplatedText(content=".answer")


@pytest.mark.parametrize("field", _DOOR_EXPR_FIELDS)
def test_route_exprs_carry_the_jq_expression_annotation(field: str):
    # Each door jq (and the reply_expr) is a jq-typed templated text, so each declares itself in the
    # generated JSON schema under the shared ``x-tai42-expression`` vendor key (language jq) for a
    # schema-driven UI to auto-render the jq editor.
    from tai42_contract.template import EXPRESSION_ANNOTATION_KEY

    prop = ChannelConversationRouteCreate.model_json_schema()["properties"][field]
    annotation = prop[EXPRESSION_ANNOTATION_KEY]
    assert annotation["language"] == "jq"
    assert annotation["label"]


def test_start_expr_catalogue_documents_the_route_key():
    # The start_expr input catalogue documents ``route`` — the name of the conversation route the
    # turn entered through — so a schema-driven UI offers it and a tool bound to several routes can
    # tell them apart. It sits right after ``channel`` in the key order.
    from tai42_contract.template import EXPRESSION_ANNOTATION_KEY

    prop = ChannelConversationRouteCreate.model_json_schema()["properties"]["start_expr"]
    keys = prop[EXPRESSION_ANNOTATION_KEY]["keys"]
    names = [entry["name"] for entry in keys]
    assert "route" in names
    assert names[names.index("channel") + 1] == "route"
    route_entry = next(entry for entry in keys if entry["name"] == "route")
    assert route_entry["gloss"] == "the name of the conversation route the turn entered through"


def test_start_expr_catalogue_documents_the_reply_to_key():
    from tai42_contract.template import EXPRESSION_ANNOTATION_KEY

    prop = ChannelConversationRouteCreate.model_json_schema()["properties"]["start_expr"]
    entries = {entry["name"]: entry["gloss"] for entry in prop[EXPRESSION_ANNOTATION_KEY]["keys"]}
    assert entries["reply_to"] == (
        "the earlier message this one replies to, {message_id, status, text}; present only when the inbound quoted one"
    )
    assert "reply_to" in entries["messages"]


def test_route_expr_annotation_keeps_the_none_default_additive():
    # CRITICAL api-gate trap: the annotation must ride ``Annotated`` so the attribute default
    # stays the ``None`` literal — a ``Field(default=None, ...)`` redeclaration is flagged as a
    # breaking change by the griffe api-gate. The field stays optional-defaulting-None and the
    # annotation adds ONLY the vendor key to a plain declaration.
    from tai42_contract.template import EXPRESSION_ANNOTATION_KEY

    route = _create(_route_kwargs(target_kind="tool", target_name="echo"))
    assert route.start_expr is None
    assert route.reply_expr is None

    schema = ChannelConversationRouteCreate.model_json_schema()
    for field in _DOOR_EXPR_FIELDS:
        prop = dict(schema["properties"][field])
        prop.pop(EXPRESSION_ANNOTATION_KEY)
        # Once the vendor key is removed, the schema is a plain nullable templated text
        # defaulting None: the annotation added ONLY its key and left nullability + the None
        # default intact.
        assert prop["default"] is None
        assert prop["anyOf"] == [{"$ref": "#/$defs/TemplatedText"}, {"type": "null"}]


def test_turns_per_hour_override_defaults_to_none_and_must_be_positive():

    # Absent by default: the route runs at the global per-address cap.
    assert _create(_route_kwargs()).turns_per_hour_override is None
    # A positive per-hour override is accepted and preserved.
    assert _create(_route_kwargs(turns_per_hour_override=250)).turns_per_hour_override == 250
    # Non-positive rates are refused.
    for bad in (0, -5):
        with pytest.raises(ValidationError):
            _create(_route_kwargs(turns_per_hour_override=bad))


_REPLY_MEMBERS = (
    "error",
    "slow_down",
    "pairing_linked",
    "pairing_unlinked",
    "pairing_not_linked",
    "pairing_code_invalid",
    "pairing_code",
    "pairing_link_unavailable",
    "pairing_unlink_unavailable",
    "rejected_unsupported_type",
    "rejected_too_large",
    "rejected_could_not_receive",
)


def test_reply_texts_names_every_fixed_reply_member():
    assert tuple(ReplyTexts.model_fields) == _REPLY_MEMBERS


@pytest.mark.parametrize("member", _REPLY_MEMBERS)
def test_reply_texts_admits_each_member_alone(member: str):
    text = "Tu código es {pairing_code}" if member == "pairing_code" else "Lo sentimos, algo salió mal."
    texts = ReplyTexts.model_validate({member: text})
    assert getattr(texts, member) == text
    assert all(getattr(texts, other) is None for other in _REPLY_MEMBERS if other != member)


@pytest.mark.parametrize("member", _REPLY_MEMBERS)
@pytest.mark.parametrize("bad", ["", "   ", "x" * 2001, 5])
def test_reply_texts_refuses_a_blank_over_long_or_non_string_member(member: str, bad: object):
    with pytest.raises(ValidationError):
        ReplyTexts.model_validate({member: bad})


def test_reply_texts_admits_a_member_at_the_bound():
    assert ReplyTexts(error="x" * 2000).error == "x" * 2000


def test_reply_texts_refuses_an_unknown_key_and_an_empty_catalogue():
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ReplyTexts.model_validate({"bogus": "hello"})
    with pytest.raises(ValidationError, match="reply_texts must set at least one member"):
        ReplyTexts.model_validate({})


def test_reply_texts_draws_a_brace_in_a_literal_member_as_written():
    assert ReplyTexts(error="Oops {not a token}").error == "Oops {not a token}"


@pytest.mark.parametrize(
    "template",
    ["Code: {pairing_code}", "Code {pairing_code}, valid until {expires_at}", "{{literal}} {pairing_code}"],
)
def test_reply_texts_pairing_code_admits_its_two_fields(template: str):
    assert ReplyTexts(pairing_code=template).pairing_code == template


@pytest.mark.parametrize(
    ("template", "message"),
    [
        ("Valid until {expires_at}", "reply_texts.pairing_code must contain the {pairing_code} field"),
        ("Code {pairing_code} {who}", "reply_texts.pairing_code may reference only"),
        ("Code {pairing_code!r}", "reply_texts.pairing_code may reference only"),
        ("Code {pairing_code:>10}", "reply_texts.pairing_code may reference only"),
        ("Code {} {pairing_code}", "reply_texts.pairing_code may reference only"),
        ("Code {pairing_code", "reply_texts.pairing_code is not a valid template"),
    ],
)
def test_reply_texts_pairing_code_refuses_a_bad_template(template: str, message: str):
    with pytest.raises(ValidationError, match=re.escape(message)):
        ReplyTexts(pairing_code=template)


def test_a_route_carries_no_reply_texts_by_default_and_round_trips_a_catalogue():
    assert _create(_route_kwargs()).reply_texts is None
    for kwargs in (_route_kwargs, _api_kwargs):
        route = _create(kwargs(reply_texts={"error": "Lo sentimos.", "slow_down": "Más despacio."}))
        assert route.reply_texts == ReplyTexts(error="Lo sentimos.", slow_down="Más despacio.")
        assert _create(route.model_dump()).reply_texts == route.reply_texts
    with pytest.raises(ValidationError):
        _create(_route_kwargs(reply_texts={}))


def test_route_locale_defaults_to_none_round_trips_and_canonicalizes():

    # Absent by default: the route declares no default language.
    assert _create(_route_kwargs()).locale is None
    # A supplied tag is stored through the one locale seam, canonicalized like every carrier.
    assert _create(_route_kwargs(locale="he-il")).locale == "he-IL"
    # It round-trips onto the stored row and survives a JSON persist cycle unchanged.
    stored = ConversationRouteBody.model_validate(
        {**_route_kwargs(locale="fr"), "execution_key_fingerprint": "fp"}
    ).root
    assert stored.locale == "fr"
    assert ConversationRouteBody.model_validate_json(stored.model_dump_json()).root.locale == "fr"


def test_route_locale_rejects_a_malformed_tag_loudly():

    # A malformed tag is rejected at the boundary — never silently dropped or guessed.
    with pytest.raises(ValidationError):
        _create(_route_kwargs(locale="not a locale"))


def test_channel_route_rejects_a_colon_in_the_channel_name():

    # The channel name prefixes the dedupe/outbound-index keys; a ``:`` in it would let
    # one (channel, provider id) pair read another's entry.
    with pytest.raises(ValidationError) as caught:
        _create(_route_kwargs(channel="twi:lio"))
    assert _errors(caught) == [("string_pattern_mismatch", ("channel", "channel"))]


@pytest.mark.parametrize("bad", ["Relay", "relay sms", "relay:sms", "relay/sms", "", "café"])
def test_route_name_rejects_non_slug(bad: str):

    with pytest.raises(ValidationError) as caught:
        _create(_route_kwargs(route_name=bad))
    assert _errors(caught) == [("string_pattern_mismatch", ("channel", "route_name"))]


def test_stored_route_adds_the_two_server_derived_fields():
    stored = ConversationRouteBody.model_validate({**_route_kwargs(), "execution_key_fingerprint": "fp-abc"}).root
    assert isinstance(stored, ChannelConversationRoute)
    assert stored.execution_key_fingerprint == "fp-abc"
    assert stored.callback_secret is None
    # each stored variant is its create variant plus exactly the two derived fields
    for stored_cls, create_cls in (
        (ApiConversationRoute, ApiConversationRouteCreate),
        (ChannelConversationRoute, ChannelConversationRouteCreate),
    ):
        assert set(stored_cls.model_fields) - set(create_cls.model_fields) == {
            "callback_secret",
            "execution_key_fingerprint",
        }
    # the fingerprint is required on the stored row
    with pytest.raises(ValidationError):
        ConversationRouteBody.model_validate(_route_kwargs())
    # a channel row never carries a callback secret
    with pytest.raises(ValidationError):
        ConversationRouteBody.model_validate(
            {**_route_kwargs(), "execution_key_fingerprint": "fp", "callback_secret": "s3cret"}
        )


# The stored JSON of one row per door, as the manager persists and backup exports it.
_STORED_API_ROW = (
    '{"cancel_expr":null,"resume_expr":null,"start_expr":null,"extras_expr":null,"route_name":"relay-api",'
    '"door":"api","target_kind":"tool","target_name":"echo","reply_expr":null,"initial_mode":"agent",'
    '"execution_key":"svc","channel":null,"our_identity":null,"callback_url":"https://host.example/hook",'
    '"turns_per_hour_override":5,"reply_texts":null,"locale":"fr","overlap":{"running":"continue",'
    '"deliver":"one","settle_seconds":0},"callback_secret":"s3cret","execution_key_fingerprint":"fp-1"}'
)
_STORED_CHANNEL_ROW = (
    '{"cancel_expr":null,"resume_expr":null,"start_expr":{"content":".message"},"extras_expr":null,'
    '"route_name":"relay-sms","door":"channel","target_kind":"agent","target_name":"assistant",'
    '"reply_expr":null,"initial_mode":"agent","execution_key":"svc","channel":"twilio",'
    '"our_identity":"+15550001111","callback_url":null,"turns_per_hour_override":null,'
    '"reply_texts":{"error":"sorry","slow_down":null,"pairing_linked":null,"pairing_unlinked":null,'
    '"pairing_not_linked":null,"pairing_code_invalid":null,"pairing_code":null,'
    '"pairing_link_unavailable":null,"pairing_unlink_unavailable":null,'
    '"rejected_unsupported_type":null,"rejected_too_large":null,"rejected_could_not_receive":null},'
    '"locale":null,"overlap":{"running":"continue","deliver":"one",'
    '"settle_seconds":0},"callback_secret":null,"execution_key_fingerprint":"fp-2"}'
)


@pytest.mark.parametrize(
    ("stored", "variant"),
    [(_STORED_API_ROW, ApiConversationRoute), (_STORED_CHANNEL_ROW, ChannelConversationRoute)],
)
def test_a_stored_row_of_either_door_round_trips_unchanged(stored: str, variant: type) -> None:
    # The same fields with the same values: an existing row reads into its door's variant and
    # writes back as the same JSON object.
    row = ConversationRouteBody.model_validate_json(stored).root
    assert isinstance(row, variant)
    assert json.loads(row.model_dump_json()) == json.loads(stored)


# -- the exported schema -------------------------------------------------------


def _create_document() -> dict[str, Any]:
    return ConversationRouteCreateBody.model_json_schema(ref_template="#/$defs/{model}")


def test_the_create_document_is_a_door_discriminated_union():
    document = _create_document()
    assert document["oneOf"] == [
        {"$ref": "#/$defs/ApiConversationRouteCreate"},
        {"$ref": "#/$defs/ChannelConversationRouteCreate"},
    ]
    assert document["discriminator"] == {
        "propertyName": "door",
        "mapping": {
            "api": "#/$defs/ApiConversationRouteCreate",
            "channel": "#/$defs/ChannelConversationRouteCreate",
        },
    }


def test_each_variant_publishes_its_door_rules():
    defs = _create_document()["$defs"]
    api, channel = defs["ApiConversationRouteCreate"], defs["ChannelConversationRouteCreate"]
    for variant in (api, channel):
        assert variant["properties"]["route_name"]["pattern"] == "^[a-z0-9-]+$"
        assert variant["properties"]["reply_texts"]["anyOf"][0] == {"$ref": "#/$defs/ReplyTexts"}
    error_text = defs["ReplyTexts"]["properties"]["error"]["anyOf"][0]
    assert error_text == {"maxLength": 2000, "minLength": 1, "pattern": "\\S", "type": "string"}
    assert api["properties"]["door"]["const"] == "api"
    assert channel["properties"]["door"]["const"] == "channel"
    assert api["properties"]["channel"]["type"] == "null"
    assert api["properties"]["our_identity"]["type"] == "null"
    assert api["properties"]["callback_url"]["anyOf"][0] == {"pattern": HTTPS_CALLBACK_PATTERN, "type": "string"}
    assert channel["properties"]["callback_url"]["type"] == "null"
    assert channel["properties"]["channel"]["pattern"] == "^[^:]*[^:\\s][^:]*$"
    assert channel["properties"]["our_identity"]["pattern"] == "\\S"
    assert {"door", "channel", "our_identity"} <= set(channel["required"])
    assert "door" in api["required"]
    assert not {"channel", "our_identity", "callback_url"} & set(api["required"])


def test_the_stored_document_is_a_door_discriminated_union():
    document = ConversationRouteBody.model_json_schema(ref_template="#/$defs/{model}")
    assert document["discriminator"]["propertyName"] == "door"
    assert document["oneOf"] == [
        {"$ref": "#/$defs/ApiConversationRoute"},
        {"$ref": "#/$defs/ChannelConversationRoute"},
    ]


def _server_accepts(value: str) -> bool:
    try:
        _require_https_url(value)
    except ValueError:
        return False
    return True


@pytest.mark.parametrize(
    "value",
    [
        "https://h",
        "https://user@h",
        "https://h/p@x",
        "https://",
        "https://:443",
        "https://h:99999",
        "https://[::1",
        "HTTPS://h",
        "https://h?x",
        "https://u:p@h/x",
        "https://h#f",
        "https://h x",
        "https://h:8443/cb?x=1",
        "http://h",
        "ftp://h",
        "/relative",
        " https://h",
        "\x01https://h",
        "ht\ttps://h",
        "https:/\t/h",
        "https://h/\npath",
    ],
)
def test_the_published_callback_pattern_never_refuses_what_the_server_accepts(value: str):
    if _server_accepts(value):
        assert re.search(HTTPS_CALLBACK_PATTERN, value), value


@pytest.mark.parametrize("value", [" https://h", "\x01https://h", "ht\ttps://h", "https:/\t/h", "https://h/\npath"])
def test_the_server_refuses_a_callback_url_that_urlsplit_would_silently_clean(value: str):
    with pytest.raises(ValueError, match="callback_url must be an absolute https URL with no userinfo"):
        _require_https_url(value)
