"""A compute job's passage to a worker process and back.

The caller packs the job and its arguments into one payload (:func:`pack`). The worker runs it
through :func:`run_job`, which returns a :class:`JobOutcome`: the job's module is imported, and its
result and exception are pickled, inside :func:`run_job`, so a job that fails at any of those steps
comes back as the job's own outcome. A :class:`JobOutcome` holds only bytes and text, so it always
crosses back to the caller, where :func:`outcome_value` turns it back into the job's result, or
raises what the job raised.
"""

from __future__ import annotations

import pickle
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from tai42_kit.compute.errors import ComputeJobFailedError, WorkerTraceback


@dataclass(frozen=True)
class JobOutcome:
    """What a job did in the worker: exactly one of ``result``, ``exception`` and ``failure`` is set.

    ``result`` is the pickled return value. ``exception`` is the pickled exception the job raised,
    set when it rebuilds from its pickle; ``failure`` (``"<module>.<qualname>: <message>"``)
    describes one that does not. ``traceback`` is the worker's traceback text of a raised exception.
    """

    result: bytes | None = None
    exception: bytes | None = None
    failure: str | None = None
    traceback: str | None = None


def job_name(fn: Callable[..., object]) -> str:
    """The ``<module>.<qualname>`` a job's errors name it by."""
    return f"{getattr(fn, '__module__', '?')}.{getattr(fn, '__qualname__', repr(fn))}"


def pack(fn: Callable[..., object], args: tuple[object, ...]) -> bytes:
    """Pickle the job and its arguments; a job or an argument that cannot be pickled raises here."""
    return pickle.dumps((fn, args))


def run_job(payload: bytes) -> JobOutcome:
    """Unpickle the job from ``payload``, run it and return what it did; a fault of the job never raises."""
    try:
        fn, args = pickle.loads(payload)  # noqa: S301 - the payload comes from pack() in the pool's parent process
        result = pickle.dumps(fn(*args))
    except BaseException as exc:  # every exception of the job is its outcome, the worker carries on
        return _raised(exc)
    return JobOutcome(result=result)


def _raised(exc: BaseException) -> JobOutcome:
    """The outcome of a raised exception; when describing it raises in turn, the outcome names its type alone."""
    try:
        return _described(exc)
    except (
        BaseException
    ) as describe_error:  # formatting or pickling may call into the exception, which may raise anything
        return JobOutcome(
            failure=f"{type(exc).__name__}: <describing the exception raised {type(describe_error).__name__}>",
            traceback=f"{type(exc).__name__} raised in the worker; its traceback could not be formatted\n",
        )


def _described(exc: BaseException) -> JobOutcome:
    text = "".join(traceback.format_exception(exc))
    try:
        pickled = pickle.dumps(exc)
        pickle.loads(pickled)  # noqa: S301 - the bytes were pickled on the line above
    except BaseException as rebuild_error:  # whatever the exception's pickling raises, the job's outcome is still built
        rebuild = "".join(traceback.format_exception_only(rebuild_error)).strip()
        return JobOutcome(
            failure=f"{type(exc).__module__}.{type(exc).__qualname__}: {_message(exc)}",
            traceback=f"{text}The exception does not rebuild from its pickle: {rebuild}\n",
        )
    return JobOutcome(exception=pickled, traceback=text)


def _message(exc: BaseException) -> str:
    """The exception's ``str``, or a text naming what its ``str`` raised."""
    try:
        return str(exc)
    except BaseException as str_error:  # an exception's own __str__ may raise anything
        return f"<str() raised {type(str_error).__module__}.{type(str_error).__qualname__}>"


def outcome_value(outcome: JobOutcome, name: str) -> Any:
    """Return the job's result, or raise what the job raised, in the caller.

    A raised exception is raised as itself, the worker's traceback chained as its cause; one that
    does not rebuild raises :class:`~tai42_kit.compute.errors.ComputeJobFailedError` naming
    ``name`` and the exception's type and message, with the same cause. A result that cannot
    be unpickled here raises the unpickling error.
    """
    if outcome.result is not None:
        return pickle.loads(outcome.result)  # noqa: S301 - pickled by run_job in the pool's own worker
    cause = WorkerTraceback(outcome.traceback)
    if outcome.exception is not None:
        raise pickle.loads(outcome.exception) from cause  # noqa: S301 - pickled by run_job in the pool's own worker
    raise ComputeJobFailedError(f"compute job {name} raised {outcome.failure}") from cause
