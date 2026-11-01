"""The exec doors over a host process: a one-shot run to its end, and the interactive exec handle.

:func:`run_host_process` runs one :class:`~tai42_kit.sandbox.host_process.HostProcess` to its
end as a one-shot exec; :class:`HostExecHandle` is the
:class:`~tai42_contract.sandbox.SandboxExecHandle` over one started for an interactive exec.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import AsyncIterator

from tai42_contract.sandbox import (
    ExecResult,
    SandboxExecHandle,
    SandboxExecTimeoutError,
    SandboxStreamChunk,
    SandboxStreamExit,
)

from tai42_kit.sandbox.exec_deadline import DeadlineStop, DeadlineVerdict, ExecDeadline, receive_within
from tai42_kit.sandbox.exec_kill import OUTPUT_HELD_NOTE
from tai42_kit.sandbox.host_process import HostProcess
from tai42_kit.sandbox.stdin_feed import read_while_feeding

logger = logging.getLogger(__name__)


async def run_host_process(process: HostProcess, *, stdin: bytes | None, timeout_seconds: float) -> ExecResult:
    """Run ``process`` to its end as a one-shot exec: feed it ``stdin`` while collecting its output and exit code.

    ``stdin`` is written, then closed, while the output is read, so a command that echoes its
    input never blocks the write; a process spawned without stdin takes ``None``. A command that
    closes its stdin or exits before taking all of it still returns its result, unless a process
    it started still held its stdin at its exit: the input left unwritten then raises
    :class:`SandboxError` (:meth:`HostProcess.feed_stdin`). A run past ``timeout_seconds`` kills
    the process and raises :class:`SandboxExecTimeoutError` with the output byte counts the
    process gave, noted :data:`~tai42_kit.sandbox.exec_kill.OUTPUT_HELD_NOTE` when a process it
    started still held its output as the kill closed it; any other failure (a stdin write that
    fails otherwise, a cancelled call) kills it and propagates. A completed run releases the
    process's group: what the command left running in it is not stopped by the kit.
    """
    stdout_buf = bytearray()
    stderr_buf = bytearray()

    async def _read() -> None:
        while (chunk := await process.next_output()) is not None:
            (stdout_buf if chunk.stream == "stdout" else stderr_buf).extend(chunk.data)

    feed = functools.partial(process.feed_stdin, stdin) if stdin is not None else None
    try:
        await asyncio.wait_for(read_while_feeding(_read, feed), timeout=timeout_seconds)
    except TimeoutError:
        await process.kill()
        timeout = SandboxExecTimeoutError(
            timeout_seconds=timeout_seconds,
            stdout_len=process.stdout_len,
            stderr_len=process.stderr_len,
        )
        if process.output_held_at_close:
            timeout.add_note(OUTPUT_HELD_NOTE)
        raise timeout from None
    except BaseException:
        # A failed stdin write (or a cancelled call) ends the exec: its process goes with it.
        await process.kill()
        raise
    process.release()
    return ExecResult(
        exit_code=process.exit_code(),
        stdout=stdout_buf.decode("utf-8", "replace"),
        stderr=stderr_buf.decode("utf-8", "replace"),
    )


class HostExecHandle(SandboxExecHandle):
    """A live interactive exec over one started :class:`HostProcess`.

    A host subprocess exposes SEPARATE stdin/stdout/stderr pipes, so a write never interleaves
    with a read and the concurrency contract is met without demuxing one shared attach stream.
    ``output`` yields each stdout/stderr chunk and one terminal :class:`SandboxStreamExit`. The
    exec's one :class:`~tai42_kit.sandbox.exec_deadline.ExecDeadline`, fixed at its start, bounds
    the whole interactive exec through a
    :class:`~tai42_kit.sandbox.exec_deadline.DeadlineStop`, whether or not anything reads its
    output. Whether the exec is still running at the deadline (:attr:`HostProcess.running`)
    decides what happens then. An exec over by then, its process exited and no process left
    holding its output pipes, is over within its deadline: the output yields all it produced,
    whatever was still unread in its pipes included, and its exit item, however late it is
    read. An exec still running at the deadline, its process running or a descendant holding
    its pipes, has run past it: the deadline kills the process group (:meth:`HostProcess.kill`),
    a stdin write made while that kill is in flight waits for it and then raises, and the next
    read raises :class:`SandboxExecTimeoutError` at once, all the output the exec gave counted
    on it, read or not, but not yielded; a descendant that still held the pipes as the kill
    closed them may have written more, which the timeout notes
    (:data:`~tai42_kit.sandbox.exec_kill.OUTPUT_HELD_NOTE`). A deadline kill that fails is
    logged and noted on that timeout. An exec that exits with stdin input still unwritten while
    a process it started holds its stdin ends its output with a :class:`SandboxError` instead of
    its exit item (:meth:`HostProcess.next_output`). Once the caller's :meth:`kill` has
    returned, the output yields the chunks already received from the exec's pipes, read or not,
    then ends at once, whether or not the deadline has passed since and even while a descendant
    that left the process group still holds its pipes: with its exit item, or
    with that stdin cut when the exec's own exit came before the kill and cut its input; the
    kill never causes a cut and never clears one. The handle holds the exec's process group
    until :meth:`kill` or the session's teardown, the exec's own exit included.
    """

    def __init__(self, process: HostProcess, *, deadline: ExecDeadline) -> None:
        """Bind the handle to a started ``process``, bounded by the exec's ``deadline``."""
        self._process = process
        self._deadline = deadline
        # Serializes concurrent write_stdin calls so a single call's bytes are never
        # interleaved with another's; the provider does no framing beyond this.
        self._write_lock = asyncio.Lock()
        self._output_iter: AsyncIterator[SandboxStreamChunk | SandboxStreamExit] | None = None
        # Set once a stop found the exec over, by a caller's kill that returned or the deadline's
        # stop: the deadline no longer turns the end of its output into a timeout.
        self._stopped = False
        self._deadline_stop = DeadlineStop(deadline, self._stop_at_deadline)

    async def write_stdin(self, data: bytes) -> None:
        """Write ``data`` to the exec's stdin; raises once stdin is closed or the exec has exited."""
        if (stop := self._deadline_stop.task) is not None:
            await asyncio.shield(stop)
        async with self._write_lock:
            await self._process.write(data)

    async def close_stdin(self) -> None:
        """Close the exec's stdin so the in-session process reads EOF."""
        self._process.close_stdin()

    @property
    def output(self) -> AsyncIterator[SandboxStreamChunk | SandboxStreamExit]:
        """The exec's merged output stream (stdout/stderr chunks then an exit), iterated once."""
        if self._output_iter is None:
            self._output_iter = self._stream_output()
        return self._output_iter

    async def kill(self) -> None:
        """Kill the exec's whole process group and reap it, even once the exec has exited; idempotent.

        The kill awaits the exec's own exit, never its pipes' EOF, then closes the pipes, so
        no child outlives the handle as an unreaped process with an open transport.
        """
        await self._process.kill()
        self._stopped = True
        self._deadline_stop.disarm()

    async def _stream_output(self) -> AsyncIterator[SandboxStreamChunk | SandboxStreamExit]:
        while True:
            item = await self._next_item()
            if item is None:
                break
            yield item
        self._deadline_stop.disarm()
        yield SandboxStreamExit(exit_code=self._process.exit_code())

    async def _next_item(self) -> SandboxStreamChunk | None:
        """The next output chunk, or ``None`` at the end; an exec still running at the deadline times out.

        Past the deadline, an exec the deadline's stop found over yields the rest of its output
        to its end. A caller's kill that returns while the deadline's own kill is in flight, or
        after it, wins: the exec is over by that kill, so its output ends as :meth:`kill`
        states instead of a timeout.
        """
        loop = asyncio.get_running_loop()
        while not self._stopped:
            if self._deadline_stop.due():
                verdict = await asyncio.shield(self._deadline_stop.start())
                if verdict.ran_past and not self._stopped:
                    timeout = SandboxExecTimeoutError(
                        timeout_seconds=self._deadline.timeout_seconds,
                        stdout_len=self._process.stdout_len,
                        stderr_len=self._process.stderr_len,
                    )
                    for note in verdict.notes:
                        timeout.add_note(note)
                    raise timeout
                continue
            read, item = await receive_within(
                self._process.next_output, self._deadline.at - loop.time(), (self._deadline_stop.started,)
            )
            if read:
                return item
        return await self._process.next_output()

    async def _stop_at_deadline(self) -> DeadlineVerdict:
        """The deadline's stop: kill the exec should it still be running, and say whether it was.

        A kill that fails is logged as it ends, whether or not a read ever raises the timeout,
        and noted on that timeout; a process still holding the exec's output as the kill closed
        its pipes is noted on it too.
        """
        if not self._process.running:
            self._stopped = True
            return DeadlineVerdict(ran_past=False)
        try:
            await self._process.kill()
        except Exception as error:
            logger.exception("the deadline kill of a sandbox exec failed")
            return DeadlineVerdict(ran_past=True, notes=(f"the kill of the sandbox exec also failed: {error}",))
        if self._process.output_held_at_close:
            return DeadlineVerdict(ran_past=True, notes=(OUTPUT_HELD_NOTE,))
        return DeadlineVerdict(ran_past=True)
