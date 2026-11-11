"""The errors a compute pool raises when it cannot run a job or cannot hand back what the job raised.

The pool and its workers are a facility the platform owns: their failures classify as an unavailable
platform transport. A job's own failure carries whatever the job raised and no platform kind.
"""

from tai42_contract.errors import ErrorKind


class ComputePoolUnavailableError(RuntimeError):
    """The compute pool facility cannot serve: a pool, a pool host or a share cannot run or start, or ended.

    It is raised when a job cannot reach a pool that runs it, or gets no result from it, and when
    a pool's share cannot start serving the children of its process.
    """

    __tai_error_kind__ = ErrorKind.UNAVAILABLE
    __tai_transport_error__ = True


class ComputeWorkerLostError(RuntimeError):
    """The worker process running the job ended before it returned a result."""

    __tai_error_kind__ = ErrorKind.UNAVAILABLE
    __tai_transport_error__ = True


class ComputeJobFailedError(RuntimeError):
    """The job raised an exception that cannot be rebuilt in the caller: the job's failure, not the platform's.

    The message carries the exception's type and message; the worker's traceback is the cause. It
    declares no error kind, because the kind is the job's and the platform does not know it.
    """


class WorkerTraceback(Exception):  # noqa: N818 - a traceback carried as a cause, not an error raised on its own
    """The worker's traceback of the exception this is chained to as the cause."""
