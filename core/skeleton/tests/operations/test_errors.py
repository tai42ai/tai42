"""Each declared operation error class maps to its HTTP status."""

from __future__ import annotations

import pytest

from tai42_skeleton.operations.errors import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    OperationError,
    OperationFailedError,
    PermissionDeniedError,
    UnavailableError,
    ValidationRejectedError,
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
