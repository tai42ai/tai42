"""The :class:`TemplatedText` value type: one renderable text, inline or stored.

A templated text names its source exactly once (``content`` OR ``id``) and carries the
``kwargs`` its render takes either way. Its generated JSON schema stamps the
:data:`TEMPLATED_TEXT_ANNOTATION_KEY` marker on the TYPE, so a schema-driven editor keys
on the type rather than on field names.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from tai42_contract.template import (
    TEMPLATED_TEXT_ANNOTATION,
    TEMPLATED_TEXT_ANNOTATION_KEY,
    ConditionMixin,
    TemplatedText,
)

# -- the exactly-one-source rule ----------------------------------------------


def test_inline_content_is_a_valid_source() -> None:
    text = TemplatedText(content="Hi {{ who }}", kwargs={"who": "Ada"})
    assert text.content == "Hi {{ who }}"
    assert text.id is None
    assert text.kwargs == {"who": "Ada"}


def test_stored_id_is_a_valid_source() -> None:
    text = TemplatedText(id="greeting.j2", kwargs={"who": "Ada"})
    assert text.id == "greeting.j2"
    assert text.content is None


def test_empty_content_is_a_source() -> None:
    # Present-but-empty text is a real (empty) source, distinct from supplying nothing.
    assert TemplatedText(content="").content == ""


def test_both_sources_are_refused_and_the_message_names_both() -> None:
    with pytest.raises(ValidationError) as excinfo:
        TemplatedText(content=".a", id="stored")
    message = str(excinfo.value)
    assert "'.a'" in message
    assert "'stored'" in message


def test_no_source_is_refused() -> None:
    with pytest.raises(ValidationError, match="neither was supplied"):
        TemplatedText()


def test_unknown_field_is_refused() -> None:
    with pytest.raises(ValidationError):
        TemplatedText(content="x", template_id="y")  # pyright: ignore[reportCallIssue]


# -- render parameters ---------------------------------------------------------


def test_kwargs_default_to_an_empty_map_per_instance() -> None:
    first = TemplatedText(content="x")
    second = TemplatedText(content="y")
    assert first.kwargs == {}
    first.kwargs["a"] = 1
    assert second.kwargs == {}


def test_round_trips_through_model_dump() -> None:
    for text in (TemplatedText(content="Hi {{ who }}", kwargs={"who": "Ada"}), TemplatedText(id="greeting.j2")):
        assert TemplatedText(**text.model_dump()) == text


# -- the schema marker ---------------------------------------------------------


def test_type_carries_the_marker() -> None:
    assert TemplatedText.model_json_schema()[TEMPLATED_TEXT_ANNOTATION_KEY] == TEMPLATED_TEXT_ANNOTATION


def test_marker_reaches_a_declaring_model_through_the_type_definition() -> None:
    # A field of this type is recognizable from the schema alone: the property
    # references the type's definition, which carries the marker.
    schema = ConditionMixin.model_json_schema()
    assert schema["properties"]["condition"]["anyOf"][0] == {"$ref": "#/$defs/TemplatedText"}
    assert schema["$defs"]["TemplatedText"][TEMPLATED_TEXT_ANNOTATION_KEY] == TEMPLATED_TEXT_ANNOTATION


def test_marker_survives_a_json_round_trip() -> None:
    # The marker must reach a client through the JSON an OpenAPI document takes.
    schema = TemplatedText.model_json_schema()
    assert json.loads(json.dumps(schema)) == schema
