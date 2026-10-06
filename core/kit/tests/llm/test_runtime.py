"""build_agent_input / build_system_message / build_user_output /
structured-output extraction: pure message-shaping and
structured-output-validation helpers."""

import json
from datetime import datetime
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import BaseModel, ValidationError

pytest.importorskip("langgraph")

from langchain_core.messages import SystemMessage

from tai42_kit.llm.runtime import (
    build_agent_input,
    build_system_message,
    build_user_output,
    extract_structured_output,
    validate_structured_output,
)
from tai42_kit.utils.data.json_schema_util import (
    INT64_MAX,
    InvalidJsonSchemaError,
    JsonSchemaValidationError,
    inject_int64_bounds,
    json_schema_to_pydantic_model,
)


def test_build_agent_input_plain_user_messages():
    out = build_agent_input("hi", "there")
    assert out == {"messages": [{"role": "user", "content": "hi"}, {"role": "user", "content": "there"}]}


def test_build_agent_input_coerces_content_to_str():
    # Pass a non-str to exercise the str() coercion of message content.
    out = build_agent_input(cast(str, 42))
    assert out["messages"] == [{"role": "user", "content": "42"}]


def test_build_agent_input_user_content_kwargs_marks_only_last_message():
    # The cache breakpoint marks the end of the stable prefix, so only the final
    # user turn becomes a structured content block; earlier turns stay plain.
    out = build_agent_input("first", "last", user_content_kwargs={"cache_control": {"type": "ephemeral"}})
    assert out == {
        "messages": [
            {"role": "user", "content": "first"},
            {
                "role": "user",
                "content": [{"type": "text", "text": "last", "cache_control": {"type": "ephemeral"}}],
            },
        ]
    }


def test_build_agent_input_no_user_content_kwargs_stays_plain_strings():
    out = build_agent_input("a", "b", user_content_kwargs=None)
    assert out == {"messages": [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]}


def test_build_agent_input_user_content_kwargs_without_messages_raises():
    with pytest.raises(ValueError, match="no user messages to carry them on"):
        build_agent_input(user_content_kwargs={"cache_control": {"type": "ephemeral"}})


def test_build_system_message_plain():
    out = build_system_message("be brief")
    assert isinstance(out, SystemMessage)
    assert out.content == "be brief"


def test_build_system_message_with_content_kwargs():
    out = build_system_message("be brief", {"cache_control": {"type": "ephemeral"}})
    assert isinstance(out, SystemMessage)
    assert out.content == [{"type": "text", "text": "be brief", "cache_control": {"type": "ephemeral"}}]


def test_build_system_message_empty_is_none():
    assert build_system_message("") is None
    assert build_system_message(None) is None


def test_build_system_message_content_kwargs_without_message_raises():
    # An empty system message with system_content_kwargs is a caller config error —
    # the keys have no carrier — so it raises loudly rather than silently discarding
    # them (symmetric with build_agent_input's kwargs-with-no-carrier raise).
    with pytest.raises(ValueError, match="no system message to carry them on"):
        build_system_message("", {"cache_control": {"type": "ephemeral"}})
    with pytest.raises(ValueError, match="no system message to carry them on"):
        build_system_message(None, {"cache_control": {"type": "ephemeral"}})


def test_build_user_output_empty():
    assert build_user_output({}) == ""
    assert build_user_output({"messages": []}) == ""


def test_build_user_output_no_content_attr():
    # A final message without a .content attribute yields "".
    assert build_user_output({"messages": ["plain-string-no-content-attr"]}) == ""


def test_build_user_output_string_content():
    msg = SimpleNamespace(content="hello")
    assert build_user_output({"messages": [msg]}) == "hello"


def test_build_user_output_list_of_strings():
    msg = SimpleNamespace(content=["a", "b"])
    assert build_user_output({"messages": [msg]}) == "a\nb"


def test_build_user_output_list_of_dicts_text_and_content_keys():
    msg = SimpleNamespace(content=[{"text": "t1"}, {"content": "c2"}, {"other": "x"}])
    out = build_user_output({"messages": [msg]})
    # text key preferred, then content key, else the whole dict stringified.
    assert out.split("\n")[0] == "t1"
    assert out.split("\n")[1] == "c2"
    assert "other" in out.split("\n")[2]


def test_build_user_output_mixed_list_serializes_json():
    msg = SimpleNamespace(content=["a", {"b": 1}])
    out = build_user_output({"messages": [msg]})
    assert json.loads(out) == ["a", {"b": 1}]


def test_build_user_output_unknown_type_coerced():
    msg = SimpleNamespace(content=123)
    assert build_user_output({"messages": [msg]}) == "123"


# --- structured-output extraction/validation -------------------------------
# The dict schema + instances below exercise a *constraint keyword*
# (``minimum``), which a shallow structural check would miss but the faithful
# draft-2020-12 validate catches.

_SCHEMA = {
    "title": "Person",
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "age": {"type": "integer", "minimum": 0},
    },
    "required": ["name", "age"],
}
_VALID = {"name": "ada", "age": 36}
_INVALID = {"name": "ada", "age": -1}  # violates the ``minimum`` constraint


class _Person(BaseModel):
    name: str
    age: int


def test_extract_present_returns_structured_response():
    out = extract_structured_output({"structured_response": _VALID}, response_format=object())
    assert out == _VALID


def test_extract_missing_raises_loudly():
    with pytest.raises(RuntimeError, match="no structured_response"):
        extract_structured_output({"messages": []}, response_format=object())


def test_extract_none_structured_raises_loudly():
    with pytest.raises(RuntimeError, match="no structured_response"):
        extract_structured_output({"structured_response": None}, response_format=object())


def test_dict_schema_validates_conforming_value():
    out = extract_structured_output({"structured_response": _VALID}, response_format=_SCHEMA)
    assert out == _VALID


def test_dict_schema_mismatch_raises():
    with pytest.raises(JsonSchemaValidationError):
        extract_structured_output({"structured_response": _INVALID}, response_format=_SCHEMA)


def test_dict_schema_emits_a_basemodel_value_as_a_plain_dict():
    """A produced pydantic instance (the shape the tool tier parses to) is dumped to
    JSON-native types for a dict-authored schema, so the extracted value is a plain dict —
    the shape a dict schema promises its consumers — not the model instance."""
    out = extract_structured_output({"structured_response": _Person(name="ada", age=36)}, response_format=_SCHEMA)
    assert out == {"name": "ada", "age": 36}
    assert not isinstance(out, _Person)


def test_dict_schema_keeps_an_omitted_optional_absent_not_null():
    """The tool tier binds a pydantic model whose optional fields default to None; an
    optional property the model omits must stay ABSENT in the emitted dict, never surfacing
    as an explicit ``null`` that the authored (optional, non-nullable) schema would reject."""
    schema = {"title": "Opt", "type": "object", "properties": {"s": {"type": "string"}}}
    model_cls = json_schema_to_pydantic_model(inject_int64_bounds(schema), model_name="Opt")
    omitted = model_cls.model_validate({})
    assert validate_structured_output(omitted, schema) == {}
    present = model_cls.model_validate({"s": "hi"})
    assert validate_structured_output(present, schema) == {"s": "hi"}


def test_pydantic_model_validates_and_coerces():
    out = extract_structured_output({"structured_response": _VALID}, response_format=_Person)
    assert isinstance(out, _Person)
    assert out.age == 36


def test_pydantic_model_mismatch_raises():
    with pytest.raises(ValidationError):
        extract_structured_output({"structured_response": {"name": "ada"}}, response_format=_Person)


def test_non_schema_response_format_passes_through():
    """A langchain response strategy (neither a model class nor a dict) is
    returned as produced, without validation."""
    out = extract_structured_output({"structured_response": _VALID}, response_format=object())
    assert out == _VALID


def test_validate_value_dict_schema_mismatch_raises():
    with pytest.raises(JsonSchemaValidationError):
        validate_structured_output(_INVALID, _SCHEMA)


def test_validate_value_dict_schema_conforming_returns_as_produced():
    assert validate_structured_output(_VALID, _SCHEMA) == _VALID


def test_validate_value_pydantic_instance_revalidates():
    person = _Person(name="ada", age=36)
    out = validate_structured_output(person, _Person)
    assert isinstance(out, _Person)
    assert out.age == 36


def test_dict_schema_dumps_basemodel_to_json_native_types():
    """A pydantic instance with a non-JSON-native field (datetime) is dumped in
    JSON mode, so the value validates as its ISO string rather than failing the
    schema's ``type: string``."""

    class _Event(BaseModel):
        name: str
        at: datetime

    schema = {
        "title": "Event",
        "type": "object",
        "properties": {"name": {"type": "string"}, "at": {"type": "string", "format": "date-time"}},
        "required": ["name", "at"],
    }
    event = _Event(name="launch", at=datetime(2026, 1, 1, 12, 0, 0))
    out = validate_structured_output(event, schema)
    assert out == {"name": "launch", "at": "2026-01-01T12:00:00"}
    assert not isinstance(out, _Event)


def test_validate_value_invalid_schema_raises():
    """A malformed schema raises loudly instead of mis-validating the value."""
    with pytest.raises(InvalidJsonSchemaError):
        validate_structured_output(_VALID, {"type": "object", "required": "name"})


# --- unconditional int64 walk closes the native BaseModel door -----------------
# A plain-int BaseModel field accepts any Python int, so a value in
# [2**63, 2**64-1] validates by pydantic yet has no signed-int64 encoding — it
# would silently store (neither retryable nor loud) without the unconditional walk.


class _Big(BaseModel):
    n: int


class _BigInner(BaseModel):
    k: int


class _BigOuter(BaseModel):
    inner: _BigInner


def test_validate_basemodel_rejects_oversized_top_level_int_naming_path():
    over = INT64_MAX + 1  # 2**63: encodes in msgpack as uint64 but overflows int64
    with pytest.raises(JsonSchemaValidationError) as exc:
        validate_structured_output(_Big(n=over), _Big)
    assert exc.value.offending_value == over
    assert exc.value.json_path == "['n']"
    assert "['n']" in str(exc.value)


def test_validate_basemodel_rejects_oversized_nested_int():
    over = INT64_MAX + 1
    with pytest.raises(JsonSchemaValidationError) as exc:
        validate_structured_output(_BigOuter(inner=_BigInner(k=over)), _BigOuter)
    assert exc.value.json_path == "['inner']['k']"


def test_validate_basemodel_conforming_value_reinflates_to_class():
    # A conforming value passes the walk and re-inflates into the caller's class,
    # preserving the .structured/result contract.
    out = validate_structured_output({"n": 5}, _Big)
    assert isinstance(out, _Big)
    assert out.n == 5


def test_dict_schema_rejects_oversized_int_in_a_pydantic_instance():
    # The tool tier parses its bound model to a pydantic instance; validated against a
    # dict-authored schema it is still walked for an integer outside the platform int64
    # range, so an oversized integer raises rather than being emitted as a dict.
    over = INT64_MAX + 1
    schema = {"title": "Big", "type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
    with pytest.raises(JsonSchemaValidationError) as exc:
        validate_structured_output(_Big(n=over), schema)
    assert exc.value.offending_value == over


def test_dict_schema_emits_conforming_pydantic_instance_as_a_plain_dict():
    # A conforming pydantic instance against a dict schema yields the plain JSON-native
    # dict the consumer expects, not the instance.
    schema = {"title": "Big", "type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
    out = validate_structured_output(_Big(n=5), schema)
    assert out == {"n": 5}
    assert not isinstance(out, _Big)


def test_pydantic_class_validates_a_generated_model_instance_via_produced_fields():
    # The tool tier parses its bound (generated) model to an instance that is a DIFFERENT
    # type from the authored class; validating it against the authored class re-inflates the
    # class from the fields the model set, the omitted optional falling back to the class
    # default — never a cross-type model_validate failure.
    class _Payload(BaseModel):
        value: int
        note: str = "default"

    generated = json_schema_to_pydantic_model(inject_int64_bounds(_Payload.model_json_schema()), model_name="_Payload")
    out = validate_structured_output(generated.model_validate({"value": 7}), _Payload)
    assert isinstance(out, _Payload)
    assert out.value == 7
    assert out.note == "default"
