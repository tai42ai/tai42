"""The typed form-reaction events: each kind parses, a foreign kind or key is refused, and the published schema."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from tai42_contract.interactions import (
    FieldChangedEvent,
    FormReactionEvent,
    FormReactionEventBody,
    FormReactionEventKind,
    PageAdvancedEvent,
    SubmittedEvent,
)

_ADAPTER: TypeAdapter[FormReactionEvent] = TypeAdapter(FormReactionEvent)


@pytest.mark.parametrize(
    ("wire", "model"),
    [
        ({"kind": "field_changed", "field": "mode"}, FieldChangedEvent(field="mode")),
        ({"kind": "page_advanced", "page": "details"}, PageAdvancedEvent(page="details")),
        ({"kind": "submitted"}, SubmittedEvent()),
    ],
)
def test_each_event_kind_parses_and_dumps_its_wire_shape(wire: dict[str, str], model: object) -> None:
    parsed = _ADAPTER.validate_python(wire)
    assert parsed == model
    assert parsed.model_dump(mode="json") == wire


@pytest.mark.parametrize(
    "wire",
    [
        {"kind": "opened"},
        {"field": "mode"},
        {"kind": "field_changed"},
        {"kind": "field_changed", "field": ""},
        {"kind": "page_advanced", "page": "p", "field": "x"},
        {"kind": "submitted", "extra": True},
    ],
)
def test_an_unknown_kind_a_missing_name_or_an_extra_key_is_refused(wire: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _ADAPTER.validate_python(wire)


def test_the_kinds_are_the_form_reaction_triggers() -> None:
    assert {kind.value for kind in FormReactionEventKind} == {"field_changed", "page_advanced", "submitted"}


def test_an_event_is_frozen() -> None:
    event = FieldChangedEvent(field="mode")
    with pytest.raises(ValidationError):
        event.field = "other"  # type: ignore[misc]


def test_the_published_body_is_a_discriminated_one_of() -> None:
    schema = FormReactionEventBody.model_json_schema()
    assert len(schema["oneOf"]) == 3
    assert schema["discriminator"]["propertyName"] == "kind"
    assert set(schema["discriminator"]["mapping"]) == {"field_changed", "page_advanced", "submitted"}
    assert FormReactionEventBody.model_validate({"kind": "submitted"}).root == SubmittedEvent()
