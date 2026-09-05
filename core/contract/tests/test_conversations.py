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


def test_validate_inbound_form_returns_a_clean_dict_unchanged():
    from tai42_contract.conversations import validate_inbound_form

    form = {"size": "L", "count": 2, "nested": {"ok": True, "tags": ["a", "b"], "note": None}}
    assert validate_inbound_form(form) is form


@pytest.mark.parametrize("bad", [["a"], "scalar", 1, 1.5, True, None])
def test_validate_inbound_form_refuses_non_objects(bad: Any):
    # The submission is a JSON OBJECT — a list or scalar top level is refused.
    from tai42_contract.conversations import validate_inbound_form

    with pytest.raises(ValueError, match="must be a JSON object"):
        validate_inbound_form(bad)


@pytest.mark.parametrize("bad_number", [float("nan"), float("inf"), float("-inf")])
def test_validate_inbound_form_refuses_non_finite_numbers(bad_number: float):
    # NaN/Infinity are not JSON; a consumer parsing the stored form must never hit them.
    from tai42_contract.conversations import validate_inbound_form

    with pytest.raises(ValueError, match="must be finite"):
        validate_inbound_form({"x": bad_number})


def test_validate_inbound_form_refuses_non_serializable_values():
    from tai42_contract.conversations import validate_inbound_form

    with pytest.raises(ValueError, match="JSON-serializable"):
        validate_inbound_form({"x": object()})


def test_validate_inbound_form_refuses_non_string_keys():
    # A non-string key would be silently coerced by serialization, altering the submission.
    from tai42_contract.conversations import validate_inbound_form

    with pytest.raises(ValueError, match="keys must be strings"):
        validate_inbound_form({1: "x"})


def test_validate_inbound_form_size_cap_counts_utf8_bytes():
    from tai42_contract.conversations import INBOUND_FORM_MAX_BYTES, validate_inbound_form

    # Just under the cap in ASCII passes; the same character count in a multi-byte
    # alphabet is over it — the bound is UTF-8 BYTES, not characters.
    char_count = INBOUND_FORM_MAX_BYTES - 100
    assert validate_inbound_form({"x": "a" * char_count}) is not None
    with pytest.raises(ValueError, match=f"over the {INBOUND_FORM_MAX_BYTES} allowed"):
        validate_inbound_form({"x": "é" * char_count})


def test_validate_inbound_form_deeply_nested_is_a_clean_refusal():
    # A pathological nesting well under the byte cap must refuse with a plain ValueError
    # naming the depth bound — never surface a RecursionError from the interpreter stack.
    from tai42_contract.conversations import INBOUND_FORM_MAX_DEPTH, validate_inbound_form

    deep: dict[str, Any] = {}
    node = deep
    for _ in range(5000):
        child: dict[str, Any] = {}
        node["a"] = child
        node = child
    with pytest.raises(ValueError, match=f"deeper than the {INBOUND_FORM_MAX_DEPTH}"):
        validate_inbound_form(deep)


def test_validate_inbound_form_accepts_nesting_at_the_depth_bound():
    from tai42_contract.conversations import INBOUND_FORM_MAX_DEPTH, validate_inbound_form

    at_bound: dict[str, Any] = {}
    node = at_bound
    for _ in range(INBOUND_FORM_MAX_DEPTH - 1):
        child: dict[str, Any] = {}
        node["a"] = child
        node = child
    assert validate_inbound_form(at_bound) is at_bound
    over = {"a": at_bound}
    with pytest.raises(ValueError, match="deeper than"):
        validate_inbound_form(over)


def test_validate_inbound_form_error_never_carries_a_value():
    # Form contents are opaque guest data: no refusal message may quote a submitted value.
    from tai42_contract.conversations import validate_inbound_form

    marker = "secret-answer-material"
    for bad in ({marker: object()}, {"k": marker + "!" * (32 * 1024)}, {1: marker}):
        with pytest.raises(ValueError, match="form") as exc_info:
            validate_inbound_form(bad)
        assert marker not in str(exc_info.value)


def test_conversation_message_accepts_none_and_a_valid_form():
    from tai42_contract.conversations import ConversationMessage

    assert ConversationMessage(external_user_id="u1", text="hi").form is None
    message = ConversationMessage(external_user_id="u1", text="size: L", form={"size": "L"})
    assert message.form == {"size": "L"}


def test_conversation_message_form_still_requires_non_blank_text():
    # ``text`` is the carrier every reader consumes; a structured submission never
    # replaces it — a channel submits a faithful text form alongside the data.
    from tai42_contract.conversations import ConversationMessage

    with pytest.raises(ValidationError, match="must be non-blank"):
        ConversationMessage(external_user_id="u1", text="   ", form={"size": "L"})


def test_conversation_message_refuses_an_invalid_form():
    from tai42_contract.conversations import ConversationMessage

    with pytest.raises(ValidationError, match="must be finite"):
        ConversationMessage(external_user_id="u1", text="hi", form={"x": float("nan")})


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
    with pytest.raises(ValidationError, match="carries answer text"):
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


# -- AnswerPart (the rich multi-message part shape) -----------------------------


def test_answer_part_is_a_text_only_part():
    from tai42_contract.conversations import AnswerPart

    part = AnswerPart(message="just text")
    assert part.message == "just text"
    assert part.is_plain_text()


def test_answer_part_carries_media_and_options():
    from tai42_contract.channels import ReplyOption
    from tai42_contract.conversations import AnswerPart
    from tai42_contract.interactions.models import MediaItem, MediaKind

    part = AnswerPart(
        message="here is the chart",
        media=[MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/c.png")],
        options=[ReplyOption(text="Yes"), ReplyOption(text="No")],
    )
    assert not part.is_plain_text()
    assert part.media is not None
    assert part.media[0].url == "https://cdn.example/c.png"
    assert part.options == [ReplyOption(text="Yes"), ReplyOption(text="No")]


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_answer_part_rejects_a_blank_message(blank: str):
    from tai42_contract.conversations import AnswerPart

    with pytest.raises(ValidationError, match="message must be non-blank"):
        AnswerPart(message=blank)


def test_answer_part_is_strict_from_birth_unknown_key_refused():
    from tai42_contract.conversations import AnswerPart

    # extra="forbid": a NEW authoring surface never silently drops an unknown key.
    with pytest.raises(ValidationError):
        # recipient is per-delivery, not per-part
        AnswerPart(message="hi", recipient="+15550001111")  # pyright: ignore[reportCallIssue]


def test_answer_part_media_and_template_are_exclusive():
    from tai42_contract.channels import ChannelTemplate
    from tai42_contract.conversations import AnswerPart
    from tai42_contract.interactions.models import MediaItem, MediaKind

    with pytest.raises(ValidationError, match="media and template are mutually exclusive"):
        AnswerPart(
            message="hi",
            media=[MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/c.png")],
            template=ChannelTemplate(name="t", language="en"),
        )


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_answer_part_media_only_message_may_be_blank(blank: str):
    # A media-only part: a caption-less image carries the content, message may be blank/omitted.
    from tai42_contract.conversations import AnswerPart
    from tai42_contract.interactions.models import MediaItem, MediaKind

    part = AnswerPart(message=blank, media=[MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/c.png")])
    assert part.message == blank
    assert not part.is_plain_text()  # media makes it a rich part, never dropped from parts
    assert AnswerPart(media=[MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/c.png")]).message == ""


def test_answer_part_media_only_carries_no_options():
    from tai42_contract.channels import ReplyOption
    from tai42_contract.conversations import AnswerPart
    from tai42_contract.interactions.models import MediaItem, MediaKind

    with pytest.raises(ValidationError, match=r"content-only .* carries no options"):
        AnswerPart(
            message="",
            media=[MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/c.png")],
            options=[ReplyOption(text="Yes")],
        )


def _part_form_schema() -> dict[str, Any]:
    return {"type": "object", "properties": {"size": {"type": "string"}}}


def test_answer_part_carries_a_form_schema():
    # An ask-less form part: the message is the form's prompt, the schema the fillable
    # form; the guest's submission enters the conversation as a guest message.
    from tai42_contract.conversations import AnswerPart

    part = AnswerPart(message="tell us your size", schema=_part_form_schema())
    assert part.schema == _part_form_schema()
    assert not part.is_plain_text()
    assert AnswerPart(message="hi").schema is None


def test_answer_part_schema_rejects_present_but_empty_dict():
    from tai42_contract.conversations import AnswerPart

    with pytest.raises(ValidationError, match="non-empty dict"):
        AnswerPart(message="hi", schema={})


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_answer_part_schema_requires_a_non_blank_message(blank: str):
    # A form needs a prompt: a media-only (blank-message) part carries no schema.
    from tai42_contract.conversations import AnswerPart
    from tai42_contract.interactions.models import MediaItem, MediaKind

    with pytest.raises(ValidationError, match=r"carries no schema; a form needs a prompt"):
        AnswerPart(
            message=blank,
            media=[MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/c.png")],
            schema=_part_form_schema(),
        )


def test_answer_part_schema_and_template_are_exclusive():
    from tai42_contract.channels import ChannelTemplate
    from tai42_contract.conversations import AnswerPart

    with pytest.raises(ValidationError, match="schema and template are mutually exclusive"):
        AnswerPart(message="hi", schema=_part_form_schema(), template=ChannelTemplate(name="t", language="en"))


def test_answer_part_schema_and_options_are_exclusive():
    # One message carries ONE interactive surface: a form's fields or a tap list, never both.
    from tai42_contract.channels import ReplyOption
    from tai42_contract.conversations import AnswerPart

    with pytest.raises(ValidationError, match="schema and options are mutually exclusive"):
        AnswerPart(message="hi", schema=_part_form_schema(), options=[ReplyOption(text="Yes")])


def test_answer_part_schema_may_combine_with_media():
    from tai42_contract.conversations import AnswerPart
    from tai42_contract.interactions.models import MediaItem, MediaKind

    part = AnswerPart(
        message="pick from the chart",
        media=[MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/c.png")],
        schema=_part_form_schema(),
    )
    assert part.media is not None
    assert part.schema == _part_form_schema()


def test_answer_part_field_set_is_the_notification_content_surface():
    # The mirror invariant, enforced MECHANICALLY: AnswerPart's field set IS
    # ChannelNotification's CONTENT surface — every notification field minus the
    # per-delivery routing fields, which stay on the single delivery and never ride a
    # part. A field added to one model and not the other (or added to the routing set
    # without a decision here) fails this test instead of drifting silently.
    from tai42_contract.channels import ChannelNotification
    from tai42_contract.conversations import AnswerPart

    routing_fields = {"recipient", "sender_identity"}
    assert routing_fields <= set(ChannelNotification.model_fields)
    content_fields = set(ChannelNotification.model_fields) - routing_fields
    assert set(AnswerPart.model_fields) == content_fields


# -- ConversationAnswer.parts (ordered multi-message) ---------------------------


def _parts(*messages: str):
    from tai42_contract.conversations import AnswerPart

    return [AnswerPart(message=m) for m in messages]


def test_conversation_answer_single_message_carries_no_parts():
    from tai42_contract.conversations import ConversationAnswer

    answer = ConversationAnswer(message_id="m-1", thread_id="t", status="answered", answer="just one")
    # A single-message answer carries parts=None; a legacy consumer reads ``answer``.
    assert answer.parts is None


def test_conversation_answer_parts_join_to_the_answer():
    from tai42_contract.conversations import ConversationAnswer

    answer = ConversationAnswer(
        message_id="m-1",
        thread_id="t",
        status="answered",
        answer="one\n\ntwo\n\nthree",
        parts=_parts("one", "two", "three"),
    )
    # Order-significant parts, and ``answer`` is exactly the blank-line join of the part
    # messages so every legacy consumer keeps reading the whole text.
    assert answer.parts is not None
    assert [p.message for p in answer.parts] == ["one", "two", "three"]
    assert answer.answer == "one\n\ntwo\n\nthree"


def test_conversation_answer_parts_must_join_to_the_answer():
    from tai42_contract.conversations import ConversationAnswer

    with pytest.raises(ValidationError, match="answer must equal the non-blank part messages joined"):
        ConversationAnswer(
            message_id="m-1", thread_id="t", status="answered", answer="one\n\ntwo", parts=_parts("one", "different")
        )


def test_conversation_answer_parts_reject_an_empty_list():
    from tai42_contract.conversations import ConversationAnswer

    with pytest.raises(ValidationError, match="parts must be a non-empty list"):
        ConversationAnswer(message_id="m-1", thread_id="t", status="answered", answer="x", parts=[])


def test_conversation_answer_silent_carries_no_parts():
    from tai42_contract.conversations import ConversationAnswer

    with pytest.raises(ValidationError, match="silent answer carries no parts"):
        ConversationAnswer(message_id="m-1", thread_id="t", status="silent", parts=_parts("one", "two"))


def _media_part():
    from tai42_contract.conversations import AnswerPart
    from tai42_contract.interactions.models import MediaItem, MediaKind

    return AnswerPart(media=[MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/c.png")])


def test_conversation_answer_all_media_answer_is_empty_string():
    # Every part media-only: the joined text is "" and that is admissible BECAUSE parts carry
    # the content. A media-only part contributes nothing to ``answer``.
    from tai42_contract.conversations import ConversationAnswer

    answer = ConversationAnswer(
        message_id="m-1", thread_id="t", status="answered", answer="", parts=[_media_part(), _media_part()]
    )
    assert answer.answer == ""
    assert answer.parts is not None
    assert len(answer.parts) == 2


def test_conversation_answer_mixed_media_only_joins_only_text_parts():
    # [text, media-only, text]: the media-only part drops out of the joined answer.
    from tai42_contract.conversations import ConversationAnswer

    answer = ConversationAnswer(
        message_id="m-1",
        thread_id="t",
        status="answered",
        answer="one\n\ntwo",
        parts=[*_parts("one"), _media_part(), *_parts("two")],
    )
    assert answer.answer == "one\n\ntwo"
    assert answer.parts is not None
    assert len(answer.parts) == 3


def test_conversation_answer_blank_answer_without_parts_is_refused():
    from tai42_contract.conversations import ConversationAnswer

    with pytest.raises(ValidationError, match="blank text must carry media-only parts"):
        ConversationAnswer(message_id="m-1", thread_id="t", status="answered", answer="")


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
def test_route_exprs_carry_the_jq_expression_annotation(field: str):
    # ``payload_expr``/``reply_expr`` are jq-typed strings, so each declares itself in the
    # generated JSON schema under the shared ``x-tai42-expression`` vendor key (language jq)
    # for a schema-driven UI to auto-render the jq editor.
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
        # Once the vendor key is removed, the schema is a plain nullable-string defaulting None:
        # the annotation added ONLY its key and left nullability + the None default intact.
        assert prop["default"] is None
        assert prop["anyOf"] == [{"type": "string"}, {"type": "null"}]


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
        "form",
        "attachments",
        "location",
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
