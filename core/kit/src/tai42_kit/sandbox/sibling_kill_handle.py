"""The interactive exec handle whose command a sibling exec kills: its deadline, its stops and the grace after a kill.

For a provider whose execs run inside a container and are killed through a sibling exec
(:mod:`~tai42_kit.sandbox.exec_group_scripts`). :class:`SiblingKillExecHandle` holds what such a
handle does the same way for every provider; the provider supplies only its transport (the
stdin writes and end-of-input, the reads of the exec's stream and their demultiplexing, the
stream's close), its sibling kill, and how it reads the exec's exit status.
"""

from __future__ import annotations

import asyncio
from abc import abstractmethod
from collections import deque
from collections.abc import AsyncIterator
from typing import Any, ClassVar

from tai42_contract.sandbox import (
    SandboxError,
    SandboxExecHandle,
    SandboxExecTimeoutError,
    SandboxStreamChunk,
    SandboxStreamExit,
)

from tai42_kit.sandbox.exec_deadline import DeadlineStop, DeadlineVerdict, ExecDeadline, receive_within
from tai42_kit.sandbox.exec_kill import KillOutcome, KillResult, report_cleanup_kill

# The error text of a stdin write into an exec stream that is closed, or whose connection was
# lost under the write.
STDIN_STREAM_CLOSED = "cannot write stdin: the sandbox exec stream is closed"


class SiblingKillExecHandle(SandboxExecHandle):
    """A live interactive exec over one stream, whose command a sibling exec kills.

    A write lock keeps a ``write_stdin`` from interleaving with another or with the end of
    input, and never splits a call's bytes, while the output drains concurrently. The exec's
    one :class:`~tai42_kit.sandbox.exec_deadline.ExecDeadline`, fixed at its start, is
    enforced host-side by a :class:`~tai42_kit.sandbox.exec_deadline.DeadlineStop`, whether or
    not anything reads the output. At the deadline, unless the exec has ended already (its exit
    status read, a caller's kill returned, or its stream showing it ended), the deadline runs
    the exec's sibling kill, as a caller's kill does, and what the kill finds decides what
    happens then; whether or when the stream ended never does. The exec ran past its deadline
    when the kill signalled its group, or when a process the command started still held its
    output once the kill was done (:attr:`~tai42_kit.sandbox.exec_kill.KillResult.output_held`):
    the next read raises :class:`SandboxExecTimeoutError` at once, even when the stream has
    ended since, and the output not yet read is not yielded. With no process holding the
    output, the timeout counts every byte the stream carried, read or not, up to its end,
    counted as it is drained for at most :attr:`_status_grace_seconds` after the kill; with one
    holding it, the stream is cut at the stop and the timeout counts the output received up to
    it, noted :data:`~tai42_kit.sandbox.exec_kill.OUTPUT_HELD_NOTE`. A kill that fails gives
    the timeout too, its failure logged and noted on it; a count the grace or a failed read cut
    short is noted on it, the first error met its cause. Any other kill found the exec over
    within its deadline, its process exited and nothing holding its output: the handle is left
    as a caller's kill with that outcome leaves it, and the output ends as below, however late
    or slowly it is read. A write or end-of-input made while the deadline's stop is in flight
    waits for it. Once the exec has ended, or its stream is gone, a write raises
    :class:`SandboxError`.

    A caller's :meth:`kill` runs the sibling kill. One that signalled the group ends the exec
    and closes its stream: the output ends with the exec's exit status, whether or not the
    deadline has passed since. One that found the session gone ends the exec and closes its
    stream too: the output ends with the exec's exit status when the provider can still read
    it, and with the provider's :class:`SandboxError` when it cannot. One after which a process
    the command started still holds the exec's output cuts the stream the same way, since no
    kill stops that process. One that found nothing running, or the exec ended or not started,
    leaves the stream open for the exec's own end: the output yields the unread tail and ends
    with the exec's exit status. Whatever the kill found, the output still yields every message
    the stream received before the stream ended, in order, the ones the deadline's stop took off
    it to count them included, before its end. The output of an exec over by a stop is read to its end at the
    consumer's pace, with no bound: its process has exited and nothing holds its output, so the
    stream carries a finite tail. The grace, :attr:`_status_grace_seconds`, bounds only the
    reader's waits on a stream that should end, summed: the time the consumer spends between
    reads never counts, and output the stream has already received is read before the grace is
    checked. An exec whose stream shows it ended (:meth:`_ended_in_stream`) reads on to its end,
    past the grace. A stream still open past the grace otherwise is the runtime failing to end
    it, the provider's :meth:`_grace_error`, and the handle closes it. Until the stream ends,
    or the grace does, the exec's stdin stays open to writes and to its end-of-input.
    """

    # How long the reader waits, summed, on the stream of an exec over by a stop before that
    # stream not ending is the runtime's failure. Each provider sets it, from how long its
    # runtime takes to end the stream of an exec that has exited with nothing holding its output.
    _status_grace_seconds: ClassVar[float]

    def __init__(self, *, exec_id: str, deadline: ExecDeadline) -> None:
        """Bind the handle to exec ``exec_id``, bounded by the exec's ``deadline``."""
        self._exec_id = exec_id
        self._deadline = deadline
        self._write_lock = asyncio.Lock()
        # Keeps the reads of the exec's stream one at a time: the reader's, and the deadline
        # kill's drain of what a signalled exec's stream carries.
        self._receive_lock = asyncio.Lock()
        # Set once the exec has ended: its exit status read, or a kill that signalled its group
        # or found its session gone. A stream that ends without the status leaves it unset, so
        # a stop still kills a command that runs on.
        self._finished = False
        # Set once the exec's stdin is gone, by its end-of-input or a stream found closed: the
        # command may still be running, and a kill or the deadline still stops it.
        self._stdin_closed = False
        # Set once a stop found the exec over, by a caller's kill that returned or the
        # deadline's stop: the deadline no longer turns the end of the exec's output into a
        # timeout, and the output reads on to the stream's end, its waits on the stream
        # bounded by the grace.
        self._stopped = False
        # Set with ``_stopped``, so a reader already waiting wakes and arms the grace instead of
        # waiting out the deadline.
        self._stop_returned = asyncio.Event()
        # The grace's time left for the reader's waits on the stream, armed by the stop and
        # spent only while a read waits.
        self._grace_left: float | None = None
        # Set once the grace ended before the exec's stream did.
        self._grace_expired = False
        # The one sibling kill in flight, which every stop of the exec awaits, so overlapping
        # stops share one kill and one result.
        self._kill_in_flight: asyncio.Task[KillResult] | None = None
        self._output_iter: AsyncIterator[SandboxStreamChunk | SandboxStreamExit] | None = None
        self._stdout_len = 0
        self._stderr_len = 0
        # Set once a read of the stream returned its end: every later read returns it again.
        self._stream_ended = False
        # The messages the deadline's kill took off the stream to count them, in order: the
        # output yields them first should a caller's kill win over the timeout, and drops them
        # when the timeout is raised.
        self._counted_messages: deque[Any] = deque()
        # The error a read of the stream raised while the deadline's kill drained it: what the
        # output raises, after the messages counted before it, should a caller's kill win over
        # the timeout.
        self._drain_error: Exception | None = None
        # The first error met while counting the output for the timeout, its cause.
        self._count_error: Exception | None = None
        # What the timeout's count covers or may miss, noted on the timeout.
        self._count_notes: list[str] = []
        self._deadline_stop = DeadlineStop(deadline, self._run_deadline_stop)

    # -- the provider's part ---------------------------------------------------------------

    @abstractmethod
    async def _write(self, data: bytes) -> None:
        """Write ``data`` to the exec's stream; a stream found gone raises :meth:`_stdin_lost`."""

    @abstractmethod
    async def _end_input(self) -> None:
        """Signal end-of-input on the exec's stream; one that cannot be signalled raises :class:`SandboxError`."""

    @abstractmethod
    async def _receive(self) -> Any:
        """The next message of the exec's stream, or ``None`` at its end; the handle makes one call at a time."""

    @abstractmethod
    def _demux(self, message: Any) -> SandboxStreamChunk | None:
        """The output chunk ``message`` carries, or ``None`` for one that carries no output."""

    @abstractmethod
    def _take_buffered(self) -> list[Any]:
        """Take off the exec's stream every message it holds that :meth:`_receive` would return without waiting."""

    @abstractmethod
    def _ended_in_stream(self) -> bool:
        """Whether the exec's stream shows the exec ended, read or not, so a stop needs no kill.

        A provider whose stream carries the exec's exit status answers from it; one whose
        stream carries none answers ``False``, and the kill finds out. A provider answering
        ``True`` holds the rest of the exec's stream already, so a stopped exec's output reads
        on to its end with no bound.
        """

    @abstractmethod
    async def _close_stream(self) -> None:
        """Close the exec's stream: it ends after the messages it has received, which :meth:`_receive` still returns."""

    @abstractmethod
    async def _exit_status(self) -> int:
        """The exit status the exec ended with, read once its stream has ended."""

    @abstractmethod
    def _grace_error(self) -> SandboxError:
        """The error of the stream of an exec over by a stop still open once the grace has passed."""

    @abstractmethod
    async def _kill_exec(self) -> KillResult:
        """Run the exec's sibling kill and report what it found."""

    @abstractmethod
    async def _end_killed(self, result: KillResult) -> None:
        """Settle an exec whose kill signalled it, found its session gone or left its output held; close its stream.

        The closed stream ends after the messages it has received: :meth:`_receive` still
        returns them, then its end.
        """

    # -- stdin -----------------------------------------------------------------------------

    def _stdin_lost(self) -> SandboxError:
        """Mark the exec's stdin gone, its stream found closed, and return the error a write raises."""
        self._stdin_closed = True
        return SandboxError(STDIN_STREAM_CLOSED)

    async def write_stdin(self, data: bytes) -> None:
        """Write ``data`` to the exec's stdin; raises once the exec has exited or its stream is closed."""
        await self._deadline_stop_done()
        if self._finished:
            raise SandboxError("cannot write stdin: the sandbox exec has exited")
        if self._stdin_closed:
            raise SandboxError(STDIN_STREAM_CLOSED)
        async with self._write_lock:
            await self._write(data)

    async def close_stdin(self) -> None:
        """Signal end-of-input to the exec, leaving its output draining; a no-op once stdin is gone.

        An end-of-input that cannot be signalled raises :class:`SandboxError`: the exec never
        saw it, and its stdin is gone for later writes and closes.
        """
        await self._deadline_stop_done()
        if self._finished or self._stdin_closed:
            return
        self._stdin_closed = True
        async with self._write_lock:
            await self._end_input()

    # -- output ----------------------------------------------------------------------------

    @property
    def output(self) -> AsyncIterator[SandboxStreamChunk | SandboxStreamExit]:
        """The exec's output: its stdout and stderr chunks, then one exit item, iterated once."""
        if self._output_iter is None:
            self._output_iter = self._iter_output()
        return self._output_iter

    async def _iter_output(self) -> AsyncIterator[SandboxStreamChunk | SandboxStreamExit]:
        try:
            while (message := await self._next_message()) is not None:
                chunk = self._demux(message)
                if chunk is None:
                    continue
                self._count(chunk)
                yield chunk
        finally:
            await self._close_stream()
            # The stream is closed, and the exec's stdin with it, while the command may run on
            # until its exit status is read.
            self._stdin_closed = True
        if self._grace_expired:
            raise self._grace_error()
        exit_code = await self._exit_status()
        self._mark_finished()
        yield SandboxStreamExit(exit_code=exit_code)

    async def _next_message(self) -> Any:
        """The next stream message, or ``None`` at the end of the stream, of the grace, or of a stopped exec.

        An exec no stop has found over is bounded by the deadline. Past it, the deadline's stop,
        started by its timer or by this read, decides: an exec it found running times out with
        :class:`SandboxExecTimeoutError`, and one it found over reads on as after a caller's
        kill. After a stop the bound is :attr:`_status_grace_seconds`, spent only while a read
        waits on the stream, never between reads; the stop's wake ends a wait in progress so
        the grace takes over. A message the stream holds already is read under any bound, one
        passed included, so no bound is checked before what has arrived is read. A grace that
        passes with the stream open ends the read, marking the grace expired, unless the stream
        shows the exec ended, which reads on to its end. A caller's kill made while the
        deadline's own stop is in flight, or after it, shares that stop and wins over the
        timeout: the output yields what the stop took off the stream to count it, then ends
        with its exit item, or with the error a read of the stream raised while the stop
        drained it.
        """
        while True:
            if not self._stopped and self._deadline_stop.due():
                await self._deadline_verdict()
                continue
            if self._counted_messages:
                return self._counted_messages.popleft()
            if self._drain_error is not None:
                raise self._drain_error
            if self._stream_ended:
                return None
            read, message = await self._read_within(max(self._bound_left(), 0.0))
            if read:
                return message
            if not self._stopped or self._bound_left() > 0:
                continue
            if self._ended_in_stream():
                return await self._read_stream()
            self._grace_expired = True
            return None

    def _bound_left(self) -> float:
        """The time left of the read's bound: the grace's, once armed, or else the deadline's."""
        if self._grace_left is None:
            return self._deadline.at - asyncio.get_running_loop().time()
        return self._grace_left

    async def _read_within(self, bound: float) -> tuple[bool, Any]:
        """Read the stream's next message, waiting at most ``bound``, a wake ending the wait first.

        A message the stream holds already is read with a zero ``bound`` too. The time the read
        waits is spent off the grace, when the grace was armed as the read began.
        """
        loop = asyncio.get_running_loop()
        started = loop.time()
        spends_grace = self._grace_left is not None
        try:
            return await receive_within(self._read_stream, bound, (self._stop_returned, self._deadline_stop.started))
        finally:
            if spends_grace and self._grace_left is not None:
                self._grace_left -= loop.time() - started

    async def _deadline_verdict(self) -> None:
        """Await the deadline's stop, started now should its timer not have run yet; an exec it found running times out.

        A caller's kill that returned meanwhile wins over the timeout. The timeout drops the
        messages the stop took off the stream to count them.
        """
        verdict = await asyncio.shield(self._deadline_stop.start())
        if verdict.ran_past and not self._stopped:
            self._counted_messages.clear()
            raise self._timeout_error(verdict.notes)

    async def _read_stream(self) -> Any:
        """The next message of the exec's stream, or ``None`` at its end, read one at a time.

        The end, once read, stays.
        """
        async with self._receive_lock:
            if self._stream_ended:
                return None
            message = await self._receive()
            if message is None:
                self._stream_ended = True
            return message

    def _count(self, chunk: SandboxStreamChunk) -> None:
        """Add ``chunk`` to the counts of the exec's output."""
        if chunk.stream == "stderr":
            self._stderr_len += len(chunk.data)
        else:
            self._stdout_len += len(chunk.data)

    def _count_message(self, message: Any) -> None:
        """Count the output stream message ``message`` carries, for the timeout.

        A message whose demultiplexing fails is skipped, and noted; the first error met while
        counting is the timeout's cause.
        """
        try:
            chunk = self._demux(message)
        except Exception as error:
            self._count_notes.append(f"the timeout's count skipped a sandbox exec stream message that failed: {error}")
            self._count_error = self._count_error or error
            return
        if chunk is not None:
            self._count(chunk)

    def _timeout_error(self, notes: tuple[str, ...]) -> SandboxExecTimeoutError:
        """The deadline's timeout, with the counts of the exec's output and carrying ``notes``, then the count's own."""
        timeout = SandboxExecTimeoutError(
            timeout_seconds=self._deadline.timeout_seconds, stdout_len=self._stdout_len, stderr_len=self._stderr_len
        )
        for note in (*notes, *self._count_notes):
            timeout.add_note(note)
        timeout.__cause__ = self._count_error
        return timeout

    # -- the deadline ----------------------------------------------------------------------

    async def _run_deadline_stop(self) -> DeadlineVerdict:
        """The deadline's stop: the exec's sibling kill, unless the exec has ended, and what it found.

        A kill that signalled the group, or after which a process the command started still
        held the exec's output, found the exec running past its deadline; one that fails gives
        the timeout too. A kill that fails or leaves the output held is logged as it ends and
        noted on the timeout. Any other outcome found the exec over, and stops it as a caller's
        kill with that outcome stops it.
        """
        stop = asyncio.ensure_future(self._stop())
        await asyncio.wait([stop])
        notes = tuple(report_cleanup_kill(stop, self._exec_id))
        if stop.exception() is not None:
            return DeadlineVerdict(ran_past=True, notes=notes)
        result = stop.result()
        if result.outcome is KillOutcome.SIGNALLED or result.output_held:
            return DeadlineVerdict(ran_past=True, notes=notes)
        self._mark_stopped()
        return DeadlineVerdict(ran_past=False)

    async def _deadline_stop_done(self) -> None:
        """Wait for the deadline's stop of the exec, when one is in flight."""
        if (stop := self._deadline_stop.task) is not None:
            await asyncio.shield(stop)

    async def _drain_to_end(self) -> None:
        """Count what the signalled exec's stream carries up to its end, for at most the grace.

        The messages counted are kept, in order, for a caller's kill that wins over the timeout
        (``_counted_messages``). A grace that passes first, or a read that fails, is noted as
        output the timeout's count may miss; the failed read's error is kept (``_drain_error``)
        as the count's cause.
        """
        loop = asyncio.get_running_loop()
        end = loop.time() + self._status_grace_seconds
        while (remaining := end - loop.time()) > 0:
            try:
                read, message = await receive_within(self._read_stream, remaining, ())
            except Exception as error:
                self._drain_error = error
                self._count_error = self._count_error or error
                self._count_notes.append(
                    "the timeout's count may miss output: reading the sandbox exec stream after the kill "
                    f"failed: {error}"
                )
                return
            if not read:
                break
            if message is None:
                return
            self._count_message(message)
            self._counted_messages.append(message)
        self._count_notes.append(
            "the timeout's count may miss output: the sandbox exec stream did not end within "
            f"{self._status_grace_seconds:g}s of the kill"
        )

    async def _cut_stream(self, result: KillResult) -> None:
        """Cut the stream of an exec whose kill left a process holding its output: the stream ends here.

        The provider's close ends the stream after the messages it has received, those it takes
        in as it closes included. After the deadline's kill, the timeout counts what the closed
        stream holds, and those messages are kept, in order, for a caller's kill that wins over
        the timeout.
        """
        self._mark_finished()
        await self._end_killed(result)
        if self._deadline_stop.task is not None:
            for message in self._take_buffered():
                self._count_message(message)
                self._counted_messages.append(message)

    def _mark_finished(self) -> None:
        """Mark the exec ended: nothing is left for its deadline to stop."""
        self._finished = True
        self._deadline_stop.disarm()

    def _mark_stopped(self) -> None:
        """Mark the exec over by a stop: the deadline no longer applies, and a waiting reader wakes for the grace."""
        self._stopped = True
        if self._grace_left is None:
            self._grace_left = self._status_grace_seconds
        self._deadline_stop.disarm()
        self._stop_returned.set()

    # -- kill ------------------------------------------------------------------------------

    async def kill(self) -> None:
        """Kill the command's process group; a no-op once the exec has ended (idempotent).

        A kill that fails raises :class:`SandboxError` and leaves the exec as it was.
        """
        await self._stop()
        self._mark_stopped()

    async def _stop(self) -> KillResult:
        """Kill the command's process group unless the exec has ended, and report what the kill found.

        An exec that has already ended, its exit status read or its group signalled, or one its
        stream shows ended (:meth:`_ended_in_stream`), read or not, needs no kill and reports
        :attr:`KillOutcome.EXEC_ENDED`; one whose stream ended without its exit status is
        killed, since its command may run on. A stop that signalled the group, or found the
        session gone, ends the exec and closes its stream, and so does one after which a
        process the command started still holds the exec's output; one that found nothing
        running, or the exec ended or not started, leaves the exec and its stream, stdin
        included, as they were. A stop that fails raises :class:`SandboxError` and leaves the exec as it was: a
        caller's :meth:`kill` raises it, and the deadline notes it on its timeout.

        Stops that overlap share one sibling kill: a stop made while a kill is in flight awaits
        that kill's result, or its error, and a cancelled stop leaves the kill running for the
        others. A stop made after it has ended runs a kill of its own.
        """
        if self._finished or self._ended_in_stream():
            return KillResult(KillOutcome.EXEC_ENDED)
        if self._kill_in_flight is None:
            self._kill_in_flight = asyncio.ensure_future(self._kill_once())
            self._kill_in_flight.add_done_callback(self._kill_ended)
        return await asyncio.shield(self._kill_in_flight)

    def _kill_ended(self, kill: asyncio.Task[KillResult]) -> None:
        """Let the next stop run a kill of its own once ``kill`` has ended."""
        if self._kill_in_flight is kill:
            self._kill_in_flight = None

    async def _kill_once(self) -> KillResult:
        """Run the exec's sibling kill and settle the exec on what it found."""
        result = await self._kill_exec()
        if result.output_held:
            await self._cut_stream(result)
        elif result.outcome in (KillOutcome.SIGNALLED, KillOutcome.SESSION_GONE):
            if result.outcome is KillOutcome.SIGNALLED and self._deadline_stop.task is not None:
                # The deadline's timeout counts all the exec's output: what its stream carries
                # up to its end is counted before the close.
                await self._drain_to_end()
            self._mark_finished()
            await self._end_killed(result)
        return result
