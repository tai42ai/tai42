"""The operation-error base the contract owns: ``OperationError`` and its ``503`` member ``UnavailableError``."""

from __future__ import annotations

from tai42_contract.errors import ErrorKind, OperationError, UnavailableError, error_kind


def test_operation_error_carries_its_message_extra_status_and_kind():
    err = OperationError("m", extra={"code": "x"})
    assert err.message == "m"
    assert str(err) == "m"
    assert err.extra == {"code": "x"}
    assert err.status == 500
    assert error_kind(err) is ErrorKind.UPSTREAM_ERROR


def test_operation_error_extra_defaults_empty():
    assert OperationError("m").extra == {}


def test_unavailable_error_is_a_503_operation_error():
    err = UnavailableError("busy")
    assert isinstance(err, OperationError)
    assert err.status == 503
    assert error_kind(err) is ErrorKind.UNAVAILABLE
