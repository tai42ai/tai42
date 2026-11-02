"""The one-shot exec whose command a sibling exec kills: its run within its timeout, and what its timeout counts.

For a provider whose execs run inside a container and are killed through a sibling exec
(:mod:`~tai42_kit.sandbox.exec_group_scripts`). The provider reads a one-shot exec's output to
its end into a :class:`OneShotOutput`, and :func:`run_one_shot` runs that read within the exec's
``timeout_seconds``. At the timeout the read goes on while the exec's kill runs, the output
counted and no longer kept, so the timeout counts the output the exec produced, the output still
on its way to the provider when the timeout passed included, as the contract's
:class:`~tai42_contract.sandbox.SandboxExecTimeoutError` states.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from tai42_contract.sandbox import SandboxStreamChunk

from tai42_kit.sandbox.exec_kill import KillResult, kill_for_failure, report_cleanup_kill
from tai42_kit.sandbox.uncancellable_wait import wait_through_cancellation


class OneShotOutput:
    """A one-shot exec's stdout and stderr, kept while the exec may still return them, and counted throughout."""

    def __init__(self) -> None:
        """Start with no output, keeping what is added."""
        self.stdout = bytearray()
        self.stderr = bytearray()
        # Every byte added, kept or not.
        self.stdout_len = 0
        self.stderr_len = 0
        self._keeping = True

    def add(self, chunk: SandboxStreamChunk) -> None:
        """Count ``chunk``, and keep its bytes unless :meth:`stop_keeping` was called."""
        if chunk.stream == "stderr":
            self.stderr_len += len(chunk.data)
            if self._keeping:
                self.stderr.extend(chunk.data)
        else:
            self.stdout_len += len(chunk.data)
            if self._keeping:
                self.stdout.extend(chunk.data)

    def stop_keeping(self) -> None:
        """Drop the output kept so far and keep none added after; the counts go on."""
        self._keeping = False
        self.stdout.clear()
        self.stderr.clear()


async def run_one_shot[T](
    run: Awaitable[T],
    output: OneShotOutput,
    *,
    timeout_seconds: float,
    kill: Callable[[], Awaitable[KillResult]],
    exec_id: str,
    grace_seconds: float,
    timeout_error: Callable[[], BaseException],
    exec_started: Callable[[BaseException], bool] = lambda exc: True,
) -> T:
    """Run ``run``, the read of exec ``exec_id``'s output into ``output`` to its end, within ``timeout_seconds``.

    Returns what ``run`` returns when it ends within the time. A run that fails, or a call that
    is cancelled, first, ends the exec through the cleanup kill ``kill()`` starts
    (:func:`~tai42_kit.sandbox.exec_kill.kill_for_failure`), unless ``exec_started`` says the
    exec never started, then raises that error.

    At the timeout ``output`` stops keeping the output, and the run goes on, counting it, while
    the kill runs. A kill after which nothing holds the exec's output lets the run go on to the
    output's end, for at most ``grace_seconds``, so the counts cover every byte the exec
    produced; a grace that passes first, or a run that fails, is noted as output the counts may
    miss, the run's error the timeout's cause. A kill that fails, or after which a process the
    command started still holds the output, cuts the run at once: the counts cover the output
    received up to the kill, and the kill is logged and noted
    (:func:`~tai42_kit.sandbox.exec_kill.report_cleanup_kill`). It then raises
    ``timeout_error()``, built once the counts are final, with those notes. A cancellation of
    the call during the kill waits for the kill; one during the kill or the rest of the run cuts
    the run and raises from the timeout. A run cut by a cancellation of the call that had
    already failed has its error noted on the cancellation.
    """
    reading = asyncio.ensure_future(run)
    try:
        await asyncio.wait([reading], timeout=timeout_seconds)
    except asyncio.CancelledError as cancelled:
        read_error, _ = await _cut(reading)
        if read_error is not None:
            cancelled.add_note(f"the sandbox exec output read also failed: {read_error}")
        if exec_started(cancelled):
            await kill_for_failure(kill(), cancelled, exec_id)
        raise
    if reading.done():
        try:
            return reading.result()
        except BaseException as exc:
            if exec_started(exc):
                await kill_for_failure(kill(), exc, exec_id)
            raise
    raise await _stop_at_timeout(
        reading, output, kill=kill, exec_id=exec_id, grace_seconds=grace_seconds, timeout_error=timeout_error
    )


async def _stop_at_timeout(
    reading: asyncio.Future[Any],
    output: OneShotOutput,
    *,
    kill: Callable[[], Awaitable[KillResult]],
    exec_id: str,
    grace_seconds: float,
    timeout_error: Callable[[], BaseException],
) -> BaseException:
    """Stop the exec whose run ``reading`` the timeout passed, and return the error the call raises.

    That error is the timeout, built once its counts are final, or a cancellation of the call
    caused by it; see :func:`run_one_shot` for what the stop counts and notes.
    """
    output.stop_keeping()
    killing = asyncio.ensure_future(kill())
    cancelled = await wait_through_cancellation(killing)
    notes = report_cleanup_kill(killing, exec_id)
    if cancelled is None and killing.exception() is None and not killing.result().output_held:
        try:
            await asyncio.wait([reading], timeout=grace_seconds)
        except asyncio.CancelledError as exc:
            cancelled = exc
        else:
            if not reading.done():
                notes.append(
                    "the timeout's count may miss output: the read of the sandbox exec did not end within "
                    f"{grace_seconds:g}s of the kill"
                )
    read_error, cut_cancelled = await _cut(reading)
    cancelled = cancelled or cut_cancelled
    if read_error is not None:
        notes.append(
            f"the timeout's count may miss output: the read of the sandbox exec failed after the kill: {read_error}"
        )
    timeout = timeout_error()
    for note in notes:
        timeout.add_note(note)
    if cancelled is not None:
        cancelled.__cause__ = timeout
        return cancelled
    timeout.__cause__ = read_error
    return timeout


async def _cut(reading: asyncio.Future[Any]) -> tuple[BaseException | None, asyncio.CancelledError | None]:
    """End the run ``reading`` here, unless it has ended, and wait for its end through a cancellation of the call.

    Returns the error the run ended with, if it ended with one, and the first cancellation of
    the call during the wait (:func:`~tai42_kit.sandbox.uncancellable_wait.wait_through_cancellation`).
    """
    reading.cancel()
    cancelled = await wait_through_cancellation(reading)
    if reading.cancelled():
        return None, cancelled
    return reading.exception(), cancelled
