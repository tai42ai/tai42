"""Each declared operation error class maps to its HTTP status."""

from __future__ import annotations

import pytest
from tai42_contract.errors import OperationError, UnavailableError

from tai42_skeleton.operations.errors import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    OperationFailedError,
    PermissionDeniedError,
    ValidationRejectedError,
    operator_message,
)


@pytest.mark.parametrize(
    ("cls", "status"),
    [
        (BadRequestError, 400),
        (PermissionDeniedError, 403),
        (NotFoundError, 404),
        (ConflictError, 409),
        (ValidationRejectedError, 422),
        (UnavailableError, 503),
        (OperationFailedError, 500),
        (OperationError, 500),
    ],
)
def test_error_class_status(cls, status):
    exc = cls("boom")
    assert exc.status == status
    assert exc.message == "boom"
    assert str(exc) == "boom"
    assert isinstance(exc, OperationError)


def test_operator_message_of_an_exception_without_notes_is_its_text() -> None:
    assert operator_message(RuntimeError("the step failed")) == "the step failed"


def test_operator_message_renders_each_note_after_the_text_in_the_order_added() -> None:
    exc = RuntimeError("the step failed")
    exc.add_note("first context")
    exc.add_note("second context")

    assert operator_message(exc) == "the step failed\nfirst context\nsecond context"
