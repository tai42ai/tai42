"""The door-side classification of a definition write's errors, by exact type.

Every refusal class of the binding validation maps to its status; a subclass of a refusal
class, the bare ``StatesError`` and the unbound-store error are not refusals and propagate
unchanged (the operation answers 500).
"""

from __future__ import annotations

import pytest
from tai42_contract.states.errors import (
    AttachConflictError,
    InvalidPathError,
    RegimeViolationError,
    SchemaValidationError,
    StateNotFoundError,
    StatesError,
    StatesNotConfiguredError,
    SubjectRefusedError,
    TemplateExistsError,
    TemplateValidationError,
    ValueValidationError,
)

from tai42_skeleton.operations.definition_door import definition_door
from tai42_skeleton.operations.errors import BadRequestError, ConflictError, NotFoundError
from tai42_skeleton.tools.state_binding import BINDING_REFUSALS, is_binding_refusal


class _ProbeValueRefusalError(ValueValidationError):
    """A synthetic subclass of a refusal class: not itself in the exact-type set."""


@pytest.mark.parametrize(
    ("error_class", "status"),
    [
        (SubjectRefusedError, BadRequestError),
        (SchemaValidationError, BadRequestError),
        (InvalidPathError, BadRequestError),
        (ValueValidationError, BadRequestError),
        (RegimeViolationError, BadRequestError),
        (TemplateValidationError, BadRequestError),
        (StateNotFoundError, NotFoundError),
        (AttachConflictError, ConflictError),
    ],
)
def test_each_binding_refusal_maps_to_its_status(error_class: type[StatesError], status: type) -> None:
    with pytest.raises(status) as caught, definition_door("probe definition"):
        raise error_class("the refusal text", extra={"path": ["a"]})
    assert type(caught.value) is status
    assert str(caught.value) == "invalid probe definition: the refusal text"
    assert caught.value.extra == {"path": ["a"]}


def test_the_mapped_classes_are_exactly_the_refusal_set() -> None:
    assert {
        SubjectRefusedError,
        SchemaValidationError,
        InvalidPathError,
        ValueValidationError,
        RegimeViolationError,
        TemplateValidationError,
        StateNotFoundError,
        AttachConflictError,
    } == BINDING_REFUSALS


@pytest.mark.parametrize(
    "error",
    [
        StatesError("store down"),
        StatesNotConfiguredError("states database is not bound"),
        _ProbeValueRefusalError("a subclass is not a refusal"),
        TemplateExistsError("a conflict class the binding validation never raises"),
    ],
)
def test_a_non_refusal_states_error_propagates_unchanged(error: StatesError) -> None:
    assert not is_binding_refusal(error)
    with pytest.raises(type(error)) as caught, definition_door("probe definition"):
        raise error
    assert caught.value is error


def test_a_value_error_is_a_bad_request() -> None:
    with (
        pytest.raises(BadRequestError, match=r"^invalid probe definition: jq does not compile$"),
        definition_door("probe definition"),
    ):
        raise ValueError("jq does not compile")


def test_any_other_exception_propagates_unchanged() -> None:
    error = ConnectionError("redis down")
    with pytest.raises(ConnectionError) as caught, definition_door("probe definition"):
        raise error
    assert caught.value is error


def test_an_operation_error_raised_inside_passes_through() -> None:
    # An OperationError is not a ValueError, so a 4xx the block raises itself keeps its own text.
    with pytest.raises(BadRequestError, match=r"^own text$"), definition_door("probe definition"):
        raise BadRequestError("own text")
