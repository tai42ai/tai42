"""Validator tests for the per-send form models (``FormData``/``FormOption``/``FormPage``)
and their against-schema cross-check on ``InteractionRequest``."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from tai42_contract.interactions.models import (
    AnswerFormat,
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
