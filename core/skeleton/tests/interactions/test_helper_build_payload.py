"""The ``ask_user`` argument-shaping logic: ``_build_payload`` per answer format,
and the unknown-format guard at the top of ``ask_user``.
"""

from __future__ import annotations

from typing import cast

import pytest
from pydantic import BaseModel
from tai42_contract.interactions import AnswerFormat

from tai42_skeleton.interactions import ask_user
from tai42_skeleton.interactions.helper import _build_payload


class _Form(BaseModel):
    name: str


def test_text_format_has_no_payload():
    assert _build_payload(AnswerFormat.TEXT, None, None) is None
    assert _build_payload(AnswerFormat.CONFIRM, None, None) is None


def test_select_requires_options():
    assert _build_payload(AnswerFormat.SELECT, ["a", "b"], None) == {"options": ["a", "b"]}
    with pytest.raises(ValueError, match="requires options"):
        _build_payload(AnswerFormat.SELECT, None, None)


def test_form_from_pydantic_model():
    payload = _build_payload(AnswerFormat.FORM, None, _Form)
    assert payload == {"schema": _Form.model_json_schema()}


def test_form_from_json_schema_dict():
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    assert _build_payload(AnswerFormat.FORM, None, schema) == {"schema": schema}


def test_form_requires_a_schema():
    with pytest.raises(ValueError, match="requires a schema"):
        _build_payload(AnswerFormat.FORM, None, None)


def test_form_rejects_bad_schema_type():
    with pytest.raises(ValueError, match="pydantic model or a JSON-schema dict"):
        # Deliberately feed an invalid runtime type to exercise the
        # bad-schema-type guard; cast satisfies the static schema-param type.
        _build_payload(AnswerFormat.FORM, None, cast("type[BaseModel]", 123))


def test_form_payload_carries_data_and_pages():
    from tai42_contract.interactions import FormData, FormOption, FormPage

    schema = {"type": "object", "properties": {"c": {"type": "string"}}}
    data = FormData(values={"c": "x"}, options={"c": [FormOption(value="x", label="X")]})
    pages = [FormPage(title="A", fields=["c"])]
    payload = _build_payload(AnswerFormat.FORM, None, schema, data=data, pages=pages)
    assert payload is not None
    assert payload["schema"] == schema
    assert payload["data"] == {"values": {"c": "x"}, "options": {"c": [{"value": "x", "label": "X"}]}}
    assert payload["pages"] == [{"title": "A", "fields": ["c"]}]


def test_form_payload_accepts_dict_data_and_pages():
    schema = {"type": "object", "properties": {"c": {"type": "string"}}}
    payload = _build_payload(
        AnswerFormat.FORM,
        None,
        schema,
        data={"values": {"c": "x"}, "options": {}},
        pages=[{"title": "A", "fields": ["c"]}],
    )
    assert payload is not None
    assert payload["data"] == {"values": {"c": "x"}, "options": {}}
    assert payload["pages"] == [{"title": "A", "fields": ["c"]}]


def test_form_payload_omits_absent_data_and_pages():
    schema = {"type": "object", "properties": {"c": {"type": "string"}}}
    assert _build_payload(AnswerFormat.FORM, None, schema) == {"schema": schema}


async def test_ask_user_rejects_data_on_non_form():
    with pytest.raises(ValueError, match="data and pages are not valid"):
        await ask_user("q", answer_format="text", data={"values": {}})
    with pytest.raises(ValueError, match="data and pages are not valid"):
        await ask_user("q", answer_format="select", options=["a"], pages=[{"title": "A", "fields": ["a"]}])


async def test_ask_user_rejects_unknown_format():
    with pytest.raises(ValueError, match="unknown answer_format"):
        await ask_user("q", answer_format="telepathy")
