"""What the sibling kill of an exec found, its bound, and the cleanup kill of a call that fails.

A provider whose execs a sibling exec kills (:mod:`~tai42_kit.sandbox.exec_group_scripts`)
reports each kill as a :class:`KillResult`, bounds it with :func:`within_kill_timeout`, which
hands the kill a :class:`KillProgress` to keep what it met for the error of a kill past its
bound, and
runs the cleanup kill of a call that ends without the exec's own end through
:func:`kill_for_failure`, or around the call with :func:`kill_on_failure`; a kill that runs on
its own, with no call waiting on it, is reported with :func:`report_cleanup_kill`.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from tai42_contract.sandbox import SandboxError

from tai42_kit.sandbox.uncancellable_wait import wait_through_cancellation

logger = logging.getLogger(__name__)

# The bound on one sibling kill, from the start of its exec to its report, and the follow-up
# read of the exec's state after a kill that could not run: a kill the runtime accepts and
# never answers fails once this has passed, so neither a caller's ``kill()`` nor a cancelled
# call's cleanup waits on it for ever.
KILL_TIMEOUT_SECONDS = 5.0

# Noted on an exec's timeout, and logged and noted when a cleanup kill reports it, when a
# process still held the exec's output once the kill was done, and so may still be writing: one
# the command started that left its group, which the kill does not reach, or one the kill
# expected to end, the wrapper or a member of the group its pidfile names, that had not yet ended
# when the kill stopped waiting for it.
OUTPUT_HELD_NOTE = (
    "a process the command started still held the sandbox exec's output after the stop, "
    "so it may still be running and may have produced output past what was received"
)


class KillOutcome(enum.Enum):
    """What the sibling kill of an exec found."""

    # It SIGKILLed the exec's process group.
    SIGNALLED = "signalled"
    # It found no pidfile or no group: the exec has not yet started its own group and so never
    # starts its argv, or it has ended (or its command removed the pidfile). With
    # :attr:`KillResult.output_held` false nothing holds its output either, so the exec has
    # ended or its argv never starts; with it true a process the command started still does.
    NOTHING_RUNNING = "nothing running"
    # The exec has ended, as its runtime shows after a kill that could not run: nothing was left
    # to stop.
    EXEC_ENDED = "exec ended"
    # The runtime shows it never started the exec, after a kill that could not run: the kill's
    # mark keeps its argv from ever starting, so nothing was left to stop.
    NOT_STARTED = "not started"
    # The session's container is gone, and every process in it with it: the exec has ended.
    SESSION_GONE = "session gone"


@dataclass(frozen=True)
class KillResult:
    """What the sibling kill of an exec found, with the status a signalled exec ended with when the kill read it."""

    outcome: KillOutcome
    # The status the argv of a :attr:`KillOutcome.SIGNALLED` exec ended with, for a provider
    # whose kill reads it; ``None`` otherwise.
    exit_code: int | None = None
    # Whether a process still held the exec's output once a :attr:`KillOutcome.SIGNALLED` or
    # :attr:`KillOutcome.NOTHING_RUNNING` kill was done (the processes the comment on
    # :data:`OUTPUT_HELD_NOTE` names). ``False`` for every other outcome.
    output_held: bool = False


def kill_failed(detail: bytes) -> SandboxError:
    """The error of a sibling kill that failed, carrying its own output ``detail``."""
    text = detail.decode("utf-8", "replace").strip()
    return SandboxError(f"sandbox exec kill failed: {text}")


class KillProgress:
    """What one sibling kill has met so far: what it wrote on its stderr, and the failure it ended with.

    The provider's kill adds what the kill wrote on its stderr to :attr:`stderr` by the time the
    kill returns or is cut off at its bound, and records its failure with :meth:`failed` before
    any follow-up step (a read of the exec's state), so a kill that then runs past
    :data:`KILL_TIMEOUT_SECONDS` keeps them on its timeout (:meth:`notes`).
    """

    def __init__(self) -> None:
        """A kill that has met nothing yet: no stderr written, no failure."""
        self.stderr = bytearray()
        self._failure: SandboxError | None = None

    def failed(self, error: SandboxError) -> None:
        """Record ``error``, the failure the kill met before its follow-up step."""
        self._failure = error

    def notes(self) -> list[str]:
        """The notes a kill past its bound leaves on its timeout: its failure, else what it wrote on its stderr.

        The stderr is given on one line, its lines joined by ``; ``, a partial line as it came.
        Empty when the kill met no failure and wrote nothing on its stderr.
        """
        if self._failure is not None:
            return [f"the sandbox exec kill failed before its time limit passed: {self._failure}"]
        text = "; ".join(self.stderr.decode("utf-8", "replace").strip().splitlines())
        if text:
            return [f"the sandbox exec kill wrote on its stderr before its time limit passed: {text}"]
        return []


async def within_kill_timeout(kill: Callable[[KillProgress], Awaitable[KillResult]]) -> KillResult:
    """Await ``kill(progress)`` for at most :data:`KILL_TIMEOUT_SECONDS`; one still running then raises.

    The kill still running raises :class:`SandboxError`, noted with what it met before
    (:meth:`KillProgress.notes`), the failure line it wrote, or a partial one, included.
    """
    progress = KillProgress()
    try:
        async with asyncio.timeout(KILL_TIMEOUT_SECONDS):
            return await kill(progress)
    except TimeoutError as exc:
        timeout = SandboxError(f"sandbox exec kill did not finish within {KILL_TIMEOUT_SECONDS}s")
        for note in progress.notes():
            timeout.add_note(note)
        raise timeout from exc


async def kill_for_failure(kill: Awaitable[KillResult], failure: BaseException, exec_id: str) -> None:
    """Run the cleanup ``kill`` of exec ``exec_id`` for a call that ends with ``failure``.

    A kill that fails never replaces ``failure``, the error the caller receives: it is logged at
    ERROR with its traceback and noted on ``failure``. A kill after which a process still held
    the exec's output is logged at WARNING and noted on ``failure`` too, since that process may
    still be running. Any other kill reports nothing. The
    ``kill`` is bounded by :func:`within_kill_timeout`, failing once that has passed, so the
    wait on it ends.

    A cancellation of the caller does not cut the kill short, so a cancelled call still stops
    its command: the kill runs as its own task, awaited through
    :func:`~tai42_kit.sandbox.uncancellable_wait.wait_through_cancellation`. When ``failure`` is
    itself a cancellation it returns once the kill is done, so the caller re-raises its own
    cancellation unchanged; otherwise it then raises the cancellation that came during the kill,
    caused by ``failure``.
    """
    task = asyncio.ensure_future(kill)
    cancelled = await wait_through_cancellation(task)
    _report_kill(task, failure, exec_id)
    if cancelled is None or isinstance(failure, asyncio.CancelledError):
        return
    raise cancelled from failure


@contextlib.asynccontextmanager
async def kill_on_failure(
    kill: Callable[[], Awaitable[KillResult]],
    exec_id: str,
    *,
    failure_of: Callable[[BaseException], BaseException] = lambda exc: exc,
    exec_started: Callable[[BaseException], bool] = lambda exc: True,
) -> AsyncIterator[None]:
    """Kill exec ``exec_id`` when the call in the block ends with an error, then raise that error.

    The block's error ``exc`` (a cancellation included) becomes the caller's error
    ``failure_of(exc)``, raised from ``exc`` when the mapping gives another error and as it came
    otherwise. Unless ``exec_started(exc)`` says the exec never started, the kill that ``kill()``
    starts runs through :func:`kill_for_failure` first, so a kill that fails or leaves a process
    holding the exec's output is noted on that error, and a cancellation during the kill waits for it.
    """
    try:
        yield
    except BaseException as exc:
        failure = failure_of(exc)
        if exec_started(exc):
            await kill_for_failure(kill(), failure, exec_id)
        if failure is exc:
            raise
        raise failure from exc


def _report_kill(task: asyncio.Future[KillResult], failure: BaseException, exec_id: str) -> None:
    """Log the ended cleanup kill ``task`` of exec ``exec_id`` and note it on ``failure``."""
    for note in report_cleanup_kill(task, exec_id):
        failure.add_note(note)


def report_cleanup_kill(task: asyncio.Future[KillResult], exec_id: str) -> list[str]:
    """Log the ended cleanup kill ``task`` of exec ``exec_id`` and return the notes it leaves on its failure.

    A kill that failed is logged at ERROR with its traceback and gives one note, followed by
    the notes its own error carries; one after which a process still held the exec's output is
    logged at WARNING and gives :data:`OUTPUT_HELD_NOTE`. Any other kill gives none.
    """
    kill_exc = task.exception()
    if kill_exc is not None:
        logger.error("the kill of sandbox exec %s failed", exec_id, exc_info=kill_exc)
        return [f"the kill of the sandbox exec also failed: {kill_exc}", *getattr(kill_exc, "__notes__", ())]
    if task.result().output_held:
        logger.warning("after the kill of sandbox exec %s, %s", exec_id, OUTPUT_HELD_NOTE)
        return [OUTPUT_HELD_NOTE]
    return []
