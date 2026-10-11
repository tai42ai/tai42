"""The conversations errors a component outside the platform core catches by type."""

from __future__ import annotations

from tai42_contract.conversations import ThreadQueueOverflowError
from tai42_contract.errors import ErrorKind, UnavailableError, error_kind


def test_the_thread_queue_overflow_is_a_retriable_unavailable_error() -> None:
    exc = ThreadQueueOverflowError("thread queue full")
    assert isinstance(exc, UnavailableError)
    assert exc.status == 503
    assert error_kind(exc) is ErrorKind.UNAVAILABLE
    assert str(exc) == "thread queue full"
