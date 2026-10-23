"""Validator tests for the per-send form models (``FormData``/``FormOption``/``FormPage``)
and their against-schema cross-check on ``InteractionRequest``."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import pytest

from tai42_contract.interactions.models import (
    AnswerFormat,
    DisplayBlock,
    FormControl,
    FormData,
    FormOption,
    FormPage,
    InteractionRequest,
    check_format_payload,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _interaction(**overrides: Any) -> InteractionRequest:
    base: dict[str, Any] = {
        "interaction_id": "i1",
        "group_id": "g1",
        "question": "?",
        "reply_to": "ch",
        "created_at": _now(),
        "timeout_at": _now(),
    }
    base.update(overrides)
    return InteractionRequest(**base)


_FORM_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "color": {"type": "string", "enum": ["red", "blue"]},
        "count": {"type": "integer"},
        "agree": {"type": "boolean"},
    },
}


def _form(**payload_extra: Any) -> InteractionRequest:
    return _interaction(answer_format=AnswerFormat.FORM, format_payload={"schema": _FORM_SCHEMA, **payload_extra})


def test_form_data_and_pages_valid():
    data = FormData(
        values={"name": "Al", "color": "red", "count": 3, "agree": True},
        options={"color": [FormOption(value="red", label="Red"), FormOption(value="green")]},
    )
    req = _form(
        data=data.model_dump(),
        pages=[
            FormPage(title="Who", fields=["name", "color"]).model_dump(),
            {"title": "More", "fields": ["count", "agree"]},
        ],
    )
    assert req.answer_format is AnswerFormat.FORM


def test_form_option_value_and_label_non_blank():
    with pytest.raises(ValueError, match="option value must be non-blank"):
        FormOption(value="  ")
    with pytest.raises(ValueError, match="option label must be non-blank"):
        FormOption(value="x", label="  ")


def test_form_option_description_is_optional_and_non_blank_when_present():
    # The second line is optional (absent by default, no length bound) and, like the label,
    # must be non-blank when set.
    assert FormOption(value="x").description is None
    assert FormOption(value="x", label="X", description="2 hours, 88.28").description == "2 hours, 88.28"
    with pytest.raises(ValueError, match="option description must be non-blank"):
        FormOption(value="x", description="  ")


def test_form_option_description_survives_the_dump_rehydrate_seam():
    # Options cross the send seam as a dumped dict and are rehydrated for delivery; the second
    # line must ride that seam by key.
    option = FormOption(value="green", label="Green", description="in stock")
    assert FormOption.model_validate(option.model_dump()).description == "in stock"


def test_form_data_value_replaced_by_per_send_option():
    # A per-send option list REPLACES the property's enum, so a value outside the enum
    # but inside the per-send list is accepted.
    data = FormData(values={"color": "green"}, options={"color": [FormOption(value="green")]})
    assert _form(data=data.model_dump()).answer_format is AnswerFormat.FORM


def test_form_data_unknown_value_property_raises():
    with pytest.raises(ValueError, match="values names unknown property 'ghost'"):
        _form(data={"values": {"ghost": "x"}})


def test_form_data_value_fails_property_schema_raises():
    with pytest.raises(ValueError, match="value for 'count' must be an integer"):
        _form(data={"values": {"count": "three"}})
    with pytest.raises(ValueError, match="value for 'color' must be one of"):
        _form(data={"values": {"color": "purple"}})


def test_form_data_options_on_non_string_property_raises():
    with pytest.raises(ValueError, match="options for 'count' require a string"):
        _form(data={"options": {"count": [{"value": "1"}]}})


def test_form_data_empty_option_list_raises():
    with pytest.raises(ValueError, match="options for 'color' must be a non-empty list"):
        _form(data={"options": {"color": []}})


def test_form_pages_missing_property_raises():
    with pytest.raises(ValueError, match="form pages omit properties"):
        _form(pages=[{"title": "Only", "fields": ["name", "color", "count"]}])


def test_form_pages_duplicate_property_raises():
    with pytest.raises(ValueError, match="appears on more than one page"):
        _form(
            pages=[
                {"title": "A", "fields": ["name", "color"]},
                {"title": "B", "fields": ["name", "count", "agree"]},
            ]
        )


def test_form_pages_unknown_property_raises():
    with pytest.raises(ValueError, match="names unknown property 'ghost'"):
        _form(pages=[{"title": "A", "fields": ["name", "color", "count", "agree", "ghost"]}])


def test_form_page_empty_fields_raises():
    with pytest.raises(ValueError, match="fields must be a non-empty list"):
        FormPage(title="A", fields=[])


def test_form_select_refuses_data_and_pages():
    with pytest.raises(ValueError, match="carries only options"):
        _interaction(answer_format=AnswerFormat.SELECT, format_payload={"options": ["a"], "data": {"values": {}}})


def test_form_text_refuses_data_and_pages():
    with pytest.raises(ValueError, match="carries only optional options"):
        _interaction(answer_format=AnswerFormat.TEXT, format_payload={"pages": []})


# === reaction_tool declaration =============================================


def _async_overrides() -> dict[str, Any]:
    return {
        "mode": "async",
        "continuation_tool": "resume",
        "continuation_identity": "svc-1",
        "expiry_at": _now(),
    }


def test_reaction_tool_on_async_form_valid():
    req = _interaction(
        answer_format=AnswerFormat.FORM,
        format_payload={"schema": _FORM_SCHEMA, "reactions": {"field_changed": ["name"]}},
        reaction_tool="react_handler",
        **_async_overrides(),
    )
    assert req.reaction_tool == "react_handler"
    restored = InteractionRequest.model_validate_json(req.model_dump_json())
    assert restored.reaction_tool == "react_handler"


def test_reaction_tool_defaults_none_for_a_static_form():
    assert _form().reaction_tool is None


def test_reaction_tool_on_non_form_raises():
    with pytest.raises(ValueError, match="reaction_tool requires answer_format FORM"):
        _interaction(answer_format=AnswerFormat.TEXT, reaction_tool="react_handler", **_async_overrides())


def test_reaction_tool_on_sync_form_raises():
    with pytest.raises(ValueError, match="reaction_tool requires mode='async'"):
        _interaction(answer_format=AnswerFormat.FORM, format_payload={"schema": _FORM_SCHEMA}, reaction_tool="react")


def test_reaction_tool_blank_raises():
    with pytest.raises(ValueError, match="reaction_tool must be non-blank"):
        _interaction(
            answer_format=AnswerFormat.FORM,
            format_payload={"schema": _FORM_SCHEMA},
            reaction_tool="   ",
            **_async_overrides(),
        )


# === reaction triggers (format_payload["reactions"]) =======================


def test_form_reactions_valid():
    req = _interaction(
        answer_format=AnswerFormat.FORM,
        format_payload={
            "schema": _FORM_SCHEMA,
            "pages": [
                {"title": "Who", "fields": ["name", "color"]},
                {"title": "More", "fields": ["count", "agree"]},
            ],
            "reactions": {"field_changed": ["name"], "page_advanced": ["Who"], "submitted": True},
        },
        reaction_tool="react_handler",
        **_async_overrides(),
    )
    assert req.answer_format is AnswerFormat.FORM


def test_form_reactions_unknown_field_raises():
    with pytest.raises(ValueError, match="field_changed names unknown property 'ghost'"):
        _form(reactions={"field_changed": ["ghost"]})


def test_form_reactions_unknown_page_raises():
    with pytest.raises(ValueError, match="page_advanced names unknown page 'Nope'"):
        _form(
            pages=[{"title": "Who", "fields": ["name", "color", "count", "agree"]}],
            reactions={"page_advanced": ["Nope"]},
        )


def test_form_reactions_page_advanced_without_pages_raises():
    # No pages means there is no page to advance from; a named page cannot exist.
    with pytest.raises(ValueError, match="page_advanced names unknown page 'Who'"):
        _form(reactions={"page_advanced": ["Who"]})


def test_form_reactions_page_advanced_ambiguous_title_raises():
    # A ``page_advanced`` trigger keys a page by its title, so a title shared by two pages is
    # ambiguous (the advance would fire on both) — refused loudly, naming the duplicated title.
    with pytest.raises(ValueError, match="page_advanced names ambiguous page 'Who'"):
        _form(
            pages=[
                {"title": "Who", "fields": ["name", "color"]},
                {"title": "Who", "fields": ["count", "agree"]},
            ],
            reactions={"page_advanced": ["Who"]},
        )


def test_form_reactions_page_advanced_unique_title_passes():
    # Two pages with DISTINCT titles: a ``page_advanced`` naming one is unambiguous and valid.
    req = _interaction(
        answer_format=AnswerFormat.FORM,
        format_payload={
            "schema": _FORM_SCHEMA,
            "pages": [
                {"title": "Who", "fields": ["name", "color"]},
                {"title": "More", "fields": ["count", "agree"]},
            ],
            "reactions": {"page_advanced": ["Who"]},
        },
        reaction_tool="react_handler",
        **_async_overrides(),
    )
    assert req.answer_format is AnswerFormat.FORM


def test_form_reactions_bad_event_kind_raises():
    # An event kind that is not one of field_changed/page_advanced/submitted is refused.
    with pytest.raises(ValueError, match="wiggled"):
        _form(reactions={"wiggled": ["name"]})


# === reaction-fed choice fields (reactions.choices) ========================


_ARRAY_FORM_SCHEMA = {
    "type": "object",
    "properties": {
        "slot": {"type": "string", "enum": ["9am", "10am"]},
        "tags": {"type": "array", "items": {"type": "string"}},
        "count": {"type": "integer"},
    },
}


def _reacting(schema: dict[str, Any], reactions: dict[str, Any]) -> InteractionRequest:
    return _interaction(
        answer_format=AnswerFormat.FORM,
        format_payload={"schema": schema, "reactions": reactions},
        reaction_tool="react_handler",
        **_async_overrides(),
    )


def test_form_reactions_choices_on_string_enum_valid():
    req = _reacting(_FORM_SCHEMA, {"choices": ["color"], "submitted": True})
    assert req.reaction_tool == "react_handler"


def test_form_reactions_choices_on_array_of_strings_valid():
    req = _reacting(_ARRAY_FORM_SCHEMA, {"choices": ["tags"], "submitted": True})
    assert req.answer_format is AnswerFormat.FORM


def test_form_reactions_choices_unknown_property_raises():
    with pytest.raises(ValueError, match="choices names unknown property 'ghost'"):
        _reacting(_FORM_SCHEMA, {"choices": ["ghost"], "submitted": True})


def test_form_reactions_choices_on_plain_string_raises():
    # ``name`` is a bare string with no enum — not option-bearing, so a reaction cannot feed it.
    with pytest.raises(ValueError, match="choices for 'name' require a string-enum or array-of-strings"):
        _reacting(_FORM_SCHEMA, {"choices": ["name"], "submitted": True})


def test_form_reactions_choices_on_integer_raises():
    with pytest.raises(ValueError, match="choices for 'count' require a string-enum or array-of-strings"):
        _reacting(_ARRAY_FORM_SCHEMA, {"choices": ["count"], "submitted": True})


def test_form_reactions_choices_without_submitted_raises():
    with pytest.raises(ValueError, match=r"reactions\.choices requires reactions\.submitted=True"):
        _reacting(_FORM_SCHEMA, {"choices": ["color"], "field_changed": ["color"]})


# === reaction_tool <-> reactions cross-invariants ==========================


def test_reaction_tool_without_a_reactions_block_raises():
    with pytest.raises(ValueError, match="reaction_tool requires format_payload\\['reactions'\\]"):
        _interaction(
            answer_format=AnswerFormat.FORM,
            format_payload={"schema": _FORM_SCHEMA},
            reaction_tool="react_handler",
            **_async_overrides(),
        )


def test_reaction_tool_with_an_empty_reactions_block_raises():
    # A reactions block declaring no trigger could never fire the handler.
    with pytest.raises(ValueError, match="at least one trigger"):
        _interaction(
            answer_format=AnswerFormat.FORM,
            format_payload={"schema": _FORM_SCHEMA, "reactions": {}},
            reaction_tool="react_handler",
            **_async_overrides(),
        )


def test_reactions_block_without_a_reaction_tool_raises():
    with pytest.raises(ValueError, match="requires a reaction_tool"):
        _form(reactions={"field_changed": ["name"]})


# === DisplayBlock + FormPage display/kind ==================================


def test_display_block_static_and_slotted_valid():
    heading = DisplayBlock(kind="heading", text="Welcome")
    image = DisplayBlock(kind="image", src="https://cdn.example/a.png", alt="A picture")
    total = DisplayBlock(kind="body", slot="total")
    assert heading.text == "Welcome"
    assert image.src == "https://cdn.example/a.png"
    assert total.slot == "total"
    assert total.text is None


def test_display_block_text_kind_rejects_src_or_alt():
    with pytest.raises(ValueError, match="carries text, not src/alt"):
        DisplayBlock(kind="heading", text="Hi", src="https://x")


def test_display_block_image_rejects_text():
    with pytest.raises(ValueError, match="carries src/alt, not text"):
        DisplayBlock(kind="image", text="nope")


def test_display_block_static_requires_its_content():
    with pytest.raises(ValueError, match="requires text"):
        DisplayBlock(kind="body")
    with pytest.raises(ValueError, match="requires a src"):
        DisplayBlock(kind="image")


def test_display_block_slot_non_blank():
    with pytest.raises(ValueError, match="slot must be non-blank"):
        DisplayBlock(kind="body", slot="  ")


def test_display_block_present_content_non_blank():
    with pytest.raises(ValueError, match="text must be non-blank"):
        DisplayBlock(kind="body", text="  ", slot="total")
    with pytest.raises(ValueError, match="src must be non-blank"):
        DisplayBlock(kind="image", src="  ", slot="pic")
    with pytest.raises(ValueError, match="alt must be non-blank"):
        DisplayBlock(kind="image", src="https://cdn.example/a.png", alt="  ")


def test_form_review_page_allows_empty_fields():
    page = FormPage(title="Review", fields=[], kind="review", display=[DisplayBlock(kind="heading", text="Check")])
    assert page.kind == "review"
    assert page.fields == []


def test_form_input_page_empty_fields_still_raises():
    with pytest.raises(ValueError, match="fields must be a non-empty list"):
        FormPage(title="A", fields=[])


def test_form_review_page_with_fields_raises():
    with pytest.raises(ValueError, match="review form page 'Review' carries no input fields"):
        FormPage(title="Review", fields=["name"], kind="review")


def test_form_pages_display_slot_uniqueness_enforced():
    with pytest.raises(ValueError, match="display slot 'total' appears on more than one block"):
        _form(
            pages=[
                {
                    "title": "A",
                    "fields": ["name", "color", "count", "agree"],
                    "display": [{"kind": "body", "slot": "total"}, {"kind": "heading", "slot": "total"}],
                }
            ]
        )


def test_form_pages_with_review_page_cover_the_input_properties():
    req = _form(
        pages=[
            {"title": "Fill", "fields": ["name", "color", "count", "agree"]},
            {"title": "Review", "fields": [], "kind": "review", "display": [{"kind": "heading", "text": "Check"}]},
        ]
    )
    assert req.answer_format is AnswerFormat.FORM


def test_form_tag_bound_accepts_unreserved_set_and_full_length():
    from tai42_contract.interactions.models import FORM_TAG_MAX_CHARS, check_form_tag

    tag = "AZaz09-._~"
    assert check_form_tag(tag) == tag
    at_cap = "a" * FORM_TAG_MAX_CHARS
    assert check_form_tag(at_cap) == at_cap


@pytest.mark.parametrize("bad", ["", "   ", "has space", "with:colon", "slash/here", "emoji\U0001f600"])
def test_form_tag_bound_refuses_blank_and_reserved_chars(bad: str):
    from tai42_contract.interactions.models import check_form_tag

    with pytest.raises(ValueError, match="form_tag must be 1 to"):
        check_form_tag(bad)


def test_form_tag_bound_refuses_over_length_and_never_names_the_value():
    from tai42_contract.interactions.models import FORM_TAG_MAX_CHARS, check_form_tag

    secret = "s" * (FORM_TAG_MAX_CHARS + 1)
    with pytest.raises(ValueError, match="form_tag must be 1 to") as excinfo:
        check_form_tag(secret)
    # The error names the BOUND, never the (opaque, possibly sensitive) value.
    assert str(FORM_TAG_MAX_CHARS) in str(excinfo.value)
    assert secret not in str(excinfo.value)


def test_form_tag_colon_excluded_so_it_rides_a_colon_token_as_one_segment():
    from tai42_contract.interactions.models import FORM_TAG_RE

    # ``:`` is excluded so a channel packing the tag into a colon-delimited token keeps it one
    # segment; the unreserved separators are admitted.
    assert FORM_TAG_RE.fullmatch("order.42-rev_1~b") is not None
    assert FORM_TAG_RE.fullmatch("a:b") is None


# === FormControl + FormPage.button_label =====================


def test_form_control_members():
    assert FormControl.CHIPS == "chips"
    assert FormControl.TEXTAREA == "textarea"
    assert [control.value for control in FormControl] == ["chips", "textarea"]


def test_form_page_button_label_non_blank():
    page = FormPage(title="Who", fields=["name"], button_label="Next step")
    assert page.button_label == "Next step"
    assert FormPage(title="Who", fields=["name"]).button_label is None
    with pytest.raises(ValueError, match="form page button_label must be non-blank when set"):
        FormPage(title="Who", fields=["name"], button_label="   ")


_PROTO_PROPERTY = {"type": "object", "properties": {"__proto__": {"type": "string"}, "name": {"type": "string"}}}
_PROTO_PROPERTY_REFUSAL = (
    "form schema property '__proto__' is not an allowed name: JavaScript reads it as an object's "
    "prototype, so a form drawn in a browser cannot keep a field by that name"
)
_PROTO_SLOT_PAGES = [
    {"title": "A", "fields": ["name", "color", "count", "agree"], "display": [{"kind": "body", "slot": "__proto__"}]}
]
_PROTO_SLOT_REFUSAL = (
    "form display slot '__proto__' is not an allowed name: JavaScript reads it as an object's "
    "prototype, so a form drawn in a browser cannot keep a slot by that name"
)


def test_a_form_property_named_proto_is_refused():
    with pytest.raises(ValueError, match=re.escape(_PROTO_PROPERTY_REFUSAL)):
        _interaction(answer_format=AnswerFormat.FORM, format_payload={"schema": _PROTO_PROPERTY})


def test_a_form_display_slot_named_proto_is_refused():
    with pytest.raises(ValueError, match=re.escape(_PROTO_SLOT_REFUSAL)):
        _form(pages=_PROTO_SLOT_PAGES)


def test_the_answer_check_view_refuses_a_proto_property_and_slot():
    with pytest.raises(ValueError, match=re.escape(_PROTO_PROPERTY_REFUSAL)):
        check_format_payload(AnswerFormat.FORM, {"schema": _PROTO_PROPERTY}, durable=False)
    with pytest.raises(ValueError, match=re.escape(_PROTO_SLOT_REFUSAL)):
        check_format_payload(AnswerFormat.FORM, {"schema": _FORM_SCHEMA, "pages": _PROTO_SLOT_PAGES}, durable=False)


def test_a_form_property_named_like_another_object_member_is_accepted():
    schema = {"type": "object", "properties": {"constructor": {"type": "string"}, "toString": {"type": "string"}}}
    request = _interaction(answer_format=AnswerFormat.FORM, format_payload={"schema": schema})
    assert request.format_payload == {"schema": schema}


# === the build-time date rules (one seam every form-carrying door reaches) ===


def _date_schema(**props: Any) -> dict[str, Any]:
    return {"type": "object", "properties": props}


_DATE = {"type": "string", "format": "date"}


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        (_date_schema(n={"type": "integer", "minDate": "2024-01-01"}), "property 'n' carries date constraints"),
        (_date_schema(d={**_DATE, "minDate": "soon"}), "property 'd' minDate must be a YYYY-MM-DD date, got 'soon'"),
        (
            _date_schema(d={**_DATE, "minDate": "2024-12-31", "maxDate": "2024-01-01"}),
            "property 'd' has maxDate '2024-01-01' before minDate '2024-12-31'",
        ),
        (_date_schema(d={**_DATE, "unavailableDates": "monday"}), "property 'd' unavailableDates must be a list"),
        (_date_schema(d={**_DATE, "unavailableDates": ["funday"]}), "property 'd' unavailableDates entries"),
        (_date_schema(d={**_DATE, "rangeStart": " "}), "property 'd' rangeStart must name a start date property"),
        (_date_schema(d={**_DATE, "minDays": 1}), "property 'd' carries minDays but declares no rangeStart"),
        (
            _date_schema(s=_DATE, d={**_DATE, "rangeStart": "s", "maxDays": -1}),
            "property 'd' maxDays must be a non-negative integer, got -1",
        ),
        (
            _date_schema(s=_DATE, d={**_DATE, "rangeStart": "s", "minDays": 5, "maxDays": 2}),
            "property 'd' has minDays 5 above maxDays 2",
        ),
        (_date_schema(d={**_DATE, "rangeStart": "d"}), "property 'd' rangeStart cannot name itself"),
        (_date_schema(d={**_DATE, "rangeStart": "ghost"}), "property 'd' rangeStart names undeclared property 'ghost'"),
        (
            _date_schema(s={"type": "string"}, d={**_DATE, "rangeStart": "s"}),
            "property 'd' rangeStart names non-date property 's'",
        ),
        (
            _date_schema(c=_DATE, b={**_DATE, "rangeStart": "c"}, a={**_DATE, "rangeStart": "b"}),
            "property 'a' rangeStart names 'b', which is already paired with 'c'",
        ),
        (
            _date_schema(s=_DATE, e1={**_DATE, "rangeStart": "s"}, e2={**_DATE, "rangeStart": "s"}),
            "property 'e2' rangeStart names 's', which is already paired with 'e1'",
        ),
    ],
)
def test_check_form_date_rules_refuses_each_nonsensical_date_key(schema: dict[str, Any], message: str) -> None:
    from tai42_contract.interactions.models import check_form_date_rules

    with pytest.raises(ValueError, match=re.escape(message)):
        check_form_date_rules(schema)


def test_check_form_date_rules_admits_a_sound_range_and_bounds() -> None:
    from tai42_contract.interactions.models import check_form_date_rules

    check_form_date_rules(
        _date_schema(
            start={**_DATE, "minDate": "2024-01-01", "unavailableDates": ["2024-07-04", "sunday"]},
            end={**_DATE, "rangeStart": "start", "minDays": 1, "maxDays": 30},
        )
    )


def test_the_date_vocabulary_is_exported_by_the_contract() -> None:
    from tai42_contract.interactions import DATE_CONSTRAINT_KEYS, FORM_WEEKDAYS

    assert DATE_CONSTRAINT_KEYS == ("minDate", "maxDate", "unavailableDates", "rangeStart", "minDays", "maxDays")
    assert FORM_WEEKDAYS[0] == "monday"
    assert len(FORM_WEEKDAYS) == 7


_TIME = {"type": "string", "format": "time"}
_DATE_TIME = {"type": "string", "format": "date-time"}


def _timed_range(**extra: Any) -> dict[str, Any]:
    props: dict[str, Any] = {
        "start": _DATE,
        "start_time": {**_TIME, "timeOf": "start"},
        "end": {**_DATE, "rangeStart": "start"},
        "end_time": {**_TIME, "timeOf": "end"},
    }
    props.update(extra)
    return _date_schema(**props)


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        (
            _timed_range(note={"type": "string", "timeOf": "start"}),
            "property 'note' carries timeOf but is not a time property (a 'string' with format 'time')",
        ),
        (
            _timed_range(at={"type": "string", "format": "date", "timeOf": "start"}),
            "property 'at' carries timeOf but is not a time property",
        ),
        (_timed_range(at={**_TIME, "timeOf": " "}), "property 'at' timeOf must name a range end date property"),
        (_timed_range(at={**_TIME, "timeOf": 3}), "property 'at' timeOf must name a range end date property"),
        (_timed_range(at={**_TIME, "timeOf": "ghost"}), "property 'at' timeOf names undeclared property 'ghost'"),
        (
            _timed_range(note={"type": "string"}, at={**_TIME, "timeOf": "note"}),
            "property 'at' timeOf names 'note', which is not a 'date' property",
        ),
        (
            _timed_range(lone=_DATE, at={**_TIME, "timeOf": "lone"}),
            "property 'at' timeOf names 'lone', which is not an end of a date range",
        ),
        (
            _date_schema(start=_DATE_TIME, end={**_DATE_TIME, "rangeStart": "start"}, at={**_TIME, "timeOf": "end"}),
            "property 'at' timeOf names 'end', a 'date-time' property that already carries its own time",
        ),
        (
            _date_schema(start=_DATE, end={**_DATE_TIME, "rangeStart": "start"}, at={**_TIME, "timeOf": "start"}),
            "property 'at' timeOf names 'start', whose range's other end 'end' is a 'date-time' property",
        ),
        (
            _date_schema(start=_DATE_TIME, end={**_DATE, "rangeStart": "start"}, at={**_TIME, "timeOf": "end"}),
            "property 'at' timeOf names 'end', whose range's other end 'start' is a 'date-time' property",
        ),
        (
            _timed_range(again={**_TIME, "timeOf": "end"}),
            "property 'again' timeOf names 'end', which already has the time 'end_time'",
        ),
        (
            _timed_range(end_time={**_TIME, "timeOf": "end", "visibleWhen": {"field": "start", "notEmpty": True}}),
            "property 'end_time' must carry the same visibleWhen as its date 'end'",
        ),
    ],
)
def test_check_form_date_rules_refuses_each_time_of_mistake(schema: dict[str, Any], message: str) -> None:
    from tai42_contract.interactions.models import check_form_date_rules

    with pytest.raises(ValueError, match=re.escape(message)):
        check_form_date_rules(schema)


def test_check_form_date_rules_admits_a_timed_range() -> None:
    from tai42_contract.interactions.models import check_form_date_rules

    shown = {"field": "start", "notEmpty": True}
    check_form_date_rules(
        _date_schema(
            start=_DATE,
            start_time={**_TIME, "timeOf": "start"},
            end={**_DATE, "rangeStart": "start", "visibleWhen": shown},
            end_time={**_TIME, "timeOf": "end", "visibleWhen": shown},
        )
    )
    # Either end may carry its time alone.
    check_form_date_rules(
        _date_schema(start=_DATE, end={**_DATE, "rangeStart": "start"}, at={**_TIME, "timeOf": "end"})
    )


def test_check_form_pages_refuses_a_time_on_another_page_than_its_date() -> None:
    from tai42_contract.interactions.models import check_form_pages

    pages = [
        FormPage(title="Dates", fields=["start", "start_time", "end"]),
        FormPage(title="Times", fields=["end_time"]),
    ]
    with pytest.raises(ValueError, match=re.escape("property 'end_time' is the time of 'end' and must sit on")):
        check_form_pages(_timed_range(), pages)


def test_check_form_pages_refuses_a_time_on_another_page_sharing_its_date_page_title() -> None:
    from tai42_contract.interactions.models import check_form_pages

    # Page titles may repeat: the two pages are still two pages.
    pages = [
        FormPage(title="Step", fields=["start", "end"]),
        FormPage(title="Step", fields=["start_time", "end_time"]),
    ]
    with pytest.raises(
        ValueError,
        match=re.escape(
            "property 'start_time' is the time of 'start' and must sit on the same page "
            "(it is on page 2 'Step', its date on page 1 'Step')"
        ),
    ):
        check_form_pages(_timed_range(), pages)


def test_check_form_pages_accepts_a_time_on_the_page_of_its_date() -> None:
    from tai42_contract.interactions.models import check_form_pages

    pages = [
        FormPage(title="From", fields=["start", "start_time"]),
        FormPage(title="To", fields=["end_time", "end"]),
        FormPage(title="Check", fields=[], kind="review"),
    ]
    check_form_pages(_timed_range(), pages)


def test_the_time_of_key_is_exported_by_the_contract() -> None:
    from tai42_contract.interactions import TIME_OF_KEY
    from tai42_contract.interactions.models import form_dates

    assert TIME_OF_KEY == "timeOf"
    assert form_dates.TIME_OF_KEY is TIME_OF_KEY
