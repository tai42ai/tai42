"""Validator tests for the per-send form models (``FormData``/``FormOption``/``FormPage``)
and their against-schema cross-check on ``InteractionRequest``."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from tai42_contract.interactions.models import (
    AnswerFormat,
    DisplayBlock,
    FormData,
    FormOption,
    FormPage,
    InteractionRequest,
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
