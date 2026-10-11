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


def test_server_reloading_error_is_unavailable_and_not_swallowed_as_bad_input():
    from tai42_contract import ServerReloadingError as TopLevel
    from tai42_contract.errors import ServerReloadingError

    err = ServerReloadingError("reloading")
    assert TopLevel is ServerReloadingError
    assert error_kind(err) is ErrorKind.UNAVAILABLE
    # A channel acknowledges and drops ValueError / LookupError; the refusal must reach the sender.
    assert not isinstance(err, ValueError | LookupError)
    assert not isinstance(err, OperationError)
