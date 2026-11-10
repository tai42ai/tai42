"""``run_job`` turns everything a job does, its unpickling included, into a ``JobOutcome``; ``outcome_value``
turns that outcome back into the job's result or its exception in the caller."""

from __future__ import annotations

import importlib
import operator
import pickle
import threading
from pathlib import Path

import pytest

from tai42_kit.compute import ComputeJobFailedError, WorkerTraceback
from tai42_kit.compute.job import JobOutcome, outcome_value, pack, run_job

_WORKER_MODULES = Path(__file__).parent / "worker_modules"


def _unpickled(data: bytes) -> object:
    return pickle.loads(data)  # noqa: S301 - the bytes are pickled by this test's own run_job calls


class _FailsToUnpickle:
    """Pickles to a call that raises when the payload is unpickled, as a job module failing to import does."""

    def __reduce__(self) -> tuple[object, tuple[int, int]]:
        return operator.truediv, (1, 0)


class _UnprintableError(Exception):
    """An exception whose ``str``, ``repr`` and pickling all raise."""

    def __str__(self) -> str:
        raise ValueError("str broken")

    def __repr__(self) -> str:
        raise ValueError("repr broken")

    def __reduce__(self) -> tuple[object, ...]:
        raise ValueError("reduce broken")


def _raise_unprintable() -> None:
    raise _UnprintableError


class _Halt(BaseException):
    pass


class _NotesRaiseHaltError(Exception):
    """An exception whose ``__notes__`` raises outside ``Exception``, which the traceback formatter does not catch."""

    def __getattribute__(self, name: str) -> object:
        if name == "__notes__":
            raise _Halt("notes")
        return super().__getattribute__(name)


def _raise_notes_halt() -> None:
    raise _NotesRaiseHaltError("x")


def test_a_returned_value_is_the_pickled_result() -> None:
    outcome = run_job(pack(operator.add, (2, 3)))

    assert outcome.result is not None
    assert _unpickled(outcome.result) == 5
    assert (outcome.exception, outcome.failure, outcome.traceback) == (None, None, None)


def test_a_raised_exception_is_pickled_with_the_worker_traceback() -> None:
    outcome = run_job(pack(operator.truediv, (1, 0)))

    assert outcome.exception is not None
    assert isinstance(_unpickled(outcome.exception), ZeroDivisionError)
    assert outcome.traceback is not None
    assert "ZeroDivisionError: division by zero" in outcome.traceback
    assert (outcome.result, outcome.failure) == (None, None)


def test_a_payload_that_fails_to_unpickle_is_the_jobs_exception() -> None:
    outcome = run_job(pickle.dumps(_FailsToUnpickle()))

    assert outcome.exception is not None
    assert isinstance(_unpickled(outcome.exception), ZeroDivisionError)


def test_a_result_that_cannot_be_pickled_is_the_jobs_exception() -> None:
    outcome = run_job(pack(threading.Lock, ()))

    assert outcome.exception is not None
    error = _unpickled(outcome.exception)
    assert isinstance(error, TypeError)
    assert "cannot pickle" in str(error)


def test_an_exception_that_does_not_rebuild_is_described_by_type_and_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(_WORKER_MODULES)
    raise_two_arg = importlib.import_module("exception_that_does_not_rebuild").raise_two_arg

    outcome = run_job(pack(raise_two_arg, ()))

    assert outcome.failure == "exception_that_does_not_rebuild.TwoArgError: 7: bad"
    assert outcome.traceback is not None
    assert 'raise TwoArgError(7, "bad")' in outcome.traceback
    assert "The exception does not rebuild from its pickle: TypeError" in outcome.traceback
    assert (outcome.result, outcome.exception) == (None, None)


def test_an_outcome_crosses_a_pickle_unchanged() -> None:
    outcome = run_job(pack(operator.truediv, (1, 0)))

    assert _unpickled(pickle.dumps(outcome)) == outcome
    assert isinstance(outcome, JobOutcome)


def test_an_exception_whose_str_repr_and_pickling_raise_is_still_the_jobs_outcome() -> None:
    outcome = run_job(pack(_raise_unprintable, ()))

    assert outcome.failure == f"{__name__}._UnprintableError: <str() raised builtins.ValueError>"
    assert outcome.traceback is not None
    assert "raise _UnprintableError" in outcome.traceback
    assert (outcome.result, outcome.exception) == (None, None)


def test_an_exception_that_cannot_be_described_is_still_the_jobs_outcome() -> None:
    outcome = run_job(pack(_raise_notes_halt, ()))

    assert outcome.result is None
    assert outcome.exception is None
    assert outcome.failure == "_NotesRaiseHaltError: <describing the exception raised _Halt>"
    assert outcome.traceback == "_NotesRaiseHaltError raised in the worker; its traceback could not be formatted\n"


def test_the_outcome_value_of_a_result_is_the_unpickled_value() -> None:
    assert outcome_value(run_job(pack(operator.add, (2, 3))), "operator.add") == 5


def test_the_outcome_value_of_a_raised_exception_raises_it_with_the_worker_traceback() -> None:
    with pytest.raises(ZeroDivisionError) as raised:
        outcome_value(run_job(pack(operator.truediv, (1, 0))), "operator.truediv")

    assert isinstance(raised.value.__cause__, WorkerTraceback)
    assert "ZeroDivisionError: division by zero" in str(raised.value.__cause__)


def test_the_outcome_value_of_an_exception_that_does_not_rebuild_raises_compute_job_failed_error() -> None:
    outcome = run_job(pack(_raise_unprintable, ()))

    with pytest.raises(
        ComputeJobFailedError, match=f"^compute job a.job raised {__name__}._UnprintableError: "
    ) as raised:
        outcome_value(outcome, "a.job")

    assert isinstance(raised.value.__cause__, WorkerTraceback)
    assert "raise _UnprintableError" in str(raised.value.__cause__)
