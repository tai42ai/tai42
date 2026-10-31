"""Whether this context runs a backend job, and the refusal of a wait for another job from inside one.

A job does not wait synchronously for another job's result. A worker runs a bounded number
of jobs at once, so jobs that each wait for a job queued behind them can hold every slot
while the jobs they wait for never start; a job that waits also holds up a module re-import
it would otherwise let through, and the job it waits for cannot start while that re-import is
pending. A job chains the next one with a callback instead.

Two sides, one per role:

* a backend plugin marks each job's own body with :func:`running_job`, in the code that runs
  the job: around the job function in the process that runs it, so on a forking pool in the
  child, after the fork. A tool the job runs then sees the mark, on the job's own thread, in
  a coroutine it awaits, and in a sync step it hands to ``asyncio.to_thread``;
* a tool that waits for a job's result calls :func:`assert_not_in_job` before it waits, and a
  tool with a non-waiting form takes it inside a job (:func:`in_job`).

The mark is a :class:`contextvars.ContextVar`, so it holds only in the job's own context: a
thread or a task started without that context is not in the job.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager

_in_job: contextvars.ContextVar[bool] = contextvars.ContextVar("tai42_backend_running_job", default=False)


@contextmanager
def running_job() -> Iterator[None]:
    """Mark the enclosed body as one backend job, for as long as it runs."""
    mark = _in_job.set(True)
    try:
        yield
    finally:
        _in_job.reset(mark)


def in_job() -> bool:
    """Whether this context runs a backend job: a tool with a non-waiting form takes it inside a job."""
    return _in_job.get()


def assert_not_in_job(what: str) -> None:
    """Raise ``RuntimeError`` when called inside a backend job; ``what`` names the waiting tool in the error."""
    if in_job():
        raise RuntimeError(
            f"{what} cannot wait for another job's result from inside a job: chain it with a callback instead"
        )
