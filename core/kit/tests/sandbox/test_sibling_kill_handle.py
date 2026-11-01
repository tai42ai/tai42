"""The shared interactive exec handle, driven through a provider whose transport is an in-memory queue."""

from __future__ import annotations

import asyncio
import contextlib
import gc
import weakref
from collections.abc import Iterator
from typing import Any, ClassVar

import pytest
from tai42_contract.sandbox import SandboxError, SandboxExecTimeoutError, SandboxStreamChunk, SandboxStreamExit

from tai42_kit.sandbox.exec_deadline import ExecDeadline
from tai42_kit.sandbox.exec_kill import OUTPUT_HELD_NOTE, KillOutcome, KillResult
from tai42_kit.sandbox.sibling_kill_handle import SiblingKillExecHandle

_KILLED = 137


class _QueueExecHandle(SiblingKillExecHandle):
    """An exec whose stream is a queue of ``(stream, data)`` messages, ``None`` ending it.

    A ``("error", text)`` message carries a stream error its demultiplexing raises, and an
    exception queued as a message is one its receive raises.
    """

    _status_grace_seconds: ClassVar[float] = 0.05

    def __init__(self, *, deadline: ExecDeadline | None = None, kill_result: KillResult | None = None) -> None:
        super().__init__(exec_id="e1", deadline=deadline or ExecDeadline.from_now(10.0))
        self.messages: asyncio.Queue[Any] = asyncio.Queue()
        self.kill_result = kill_result or KillResult(KillOutcome.SIGNALLED)
        self.kill_gate = asyncio.Event()
        self.kill_gate.set()
        self.kills = 0
        self.written: list[bytes] = []
        self.input_ended = False
        self.stream_closed = False
        self.exit_code = 0
        # Set when the stream ends without the exec's exit status, as a dropped transport does.
        self.status_lost = False
        # Set when the exec's exit status is read while its stream is still open.
        self.status_read = False
        # Set when the stream shows the exec ended though its exit status is not read yet.
        self.ended_in_stream = False
        # The messages a signalled exec's stream carries after its kill returns, before its end.
        self.after_kill: list[Any] = []
        # Set when a signalled exec's stream stays open after its kill, as one a descendant holds does.
        self.stream_outlives_kill = False
        self._end_queued = False

    def end_stream(self) -> None:
        """End the stream now, after the messages already queued; once."""
        if not self._end_queued:
            self._end_queued = True
            self.messages.put_nowait(None)

    def _deliver_after_kill(self) -> None:
        for message in self.after_kill:
            self.messages.put_nowait(message)
        self.end_stream()

    async def _write(self, data: bytes) -> None:
        if self.stream_closed:
            raise self._stdin_lost()
        self.written.append(data)

    async def _end_input(self) -> None:
        self.input_ended = True

    async def _receive(self) -> Any:
        message = await self.messages.get()
        if isinstance(message, Exception):
            raise message
        return message

    def _demux(self, message: Any) -> SandboxStreamChunk | None:
        stream, data = message
        if stream == "error":
            raise SandboxError(data.decode())
        return SandboxStreamChunk(stream=stream, data=data) if data else None

    async def _close_stream(self) -> None:
        self.stream_closed = True

    async def _exit_status(self) -> int:
        if self.status_lost:
            raise SandboxError("sandbox exec stream ended without an exit status")
        return self.exit_code

    def _grace_error(self) -> SandboxError:
        return SandboxError("the stream stayed open after the kill")

    async def _kill_exec(self) -> KillResult:
        self.kills += 1
        await self.kill_gate.wait()
        if self.kill_result.outcome is KillOutcome.SIGNALLED and not self.stream_outlives_kill:
            # The signalled group's last output, then its stream's end, arrive after the kill returns.
            asyncio.get_running_loop().call_later(0.01, self._deliver_after_kill)
        return self.kill_result

    async def _end_killed(self, result: KillResult) -> None:
        if result.outcome is not KillOutcome.NOTHING_RUNNING:
            self.exit_code = _KILLED
        self.end_stream()

    def _ended_in_stream(self) -> bool:
        return self.status_read or self.ended_in_stream

    def _take_buffered(self) -> list[Any]:
        taken: list[Any] = []
        while not self.messages.empty():
            message = self.messages.get_nowait()
            if message is None:
                # The stream's end stays queued after the messages taken.
                self.messages.put_nowait(None)
                break
            taken.append(message)
        return taken


class _LongGraceHandle(_QueueExecHandle):
    """A queue exec whose grace no drain of a stream that ends by itself comes near, however busy the host."""

    _status_grace_seconds: ClassVar[float] = 30.0


class _StatusAtKillHandle(_QueueExecHandle):
    """Holds the exec's exit status once a kill closed its stream, as a provider whose kill reads it does."""

    async def _end_killed(self, result: KillResult) -> None:
        await super()._end_killed(result)
        self.status_read = True


class _DroppingCloseHandle(_QueueExecHandle):
    """Drops the messages its stream still holds when a kill closes it, as a read-ahead that stops does."""

    async def _end_killed(self, result: KillResult) -> None:
        while not self.messages.empty():
            self.messages.get_nowait()
        self._end_queued = False
        await super()._end_killed(result)


class _TakesInAtCloseHandle(_QueueExecHandle):
    """Takes in what its transport still holds when a kill closes its stream, as a read-ahead that drains does."""

    async def _end_killed(self, result: KillResult) -> None:
        self.messages.put_nowait(("stdout", b"held"))
        await super()._end_killed(result)


async def _drain(handle: SiblingKillExecHandle) -> list[SandboxStreamChunk | SandboxStreamExit]:
    return [item async for item in handle.output]


async def test_the_output_yields_each_chunk_then_the_exit_status() -> None:
    handle = _QueueExecHandle()
    for message in (("stdout", b"out"), ("stdout", b""), ("stderr", b"err"), None):
        handle.messages.put_nowait(message)
    handle.exit_code = 4
    assert await _drain(handle) == [
        SandboxStreamChunk(stream="stdout", data=b"out"),
        SandboxStreamChunk(stream="stderr", data=b"err"),
        SandboxStreamExit(exit_code=4),
    ]
    assert handle.stream_closed


async def test_a_write_after_the_exec_ended_raises() -> None:
    handle = _QueueExecHandle()
    handle.messages.put_nowait(None)
    await _drain(handle)
    with pytest.raises(SandboxError, match=r"^cannot write stdin: the sandbox exec has exited$"):
        await handle.write_stdin(b"x")


async def test_a_write_to_a_stream_found_closed_raises_and_so_does_every_later_one() -> None:
    handle = _QueueExecHandle()
    handle.stream_closed = True
    for _ in range(2):
        with pytest.raises(SandboxError, match=r"^cannot write stdin: the sandbox exec stream is closed$"):
            await handle.write_stdin(b"x")
    assert handle.written == []


async def test_stdin_takes_writes_until_its_end_of_input_which_is_signalled_once() -> None:
    handle = _QueueExecHandle()
    await handle.write_stdin(b"a")
    await handle.close_stdin()
    handle.input_ended = False
    await handle.close_stdin()
    assert (handle.written, handle.input_ended) == ([b"a"], False)
    with pytest.raises(SandboxError, match=r"^cannot write stdin: the sandbox exec stream is closed$"):
        await handle.write_stdin(b"b")


async def test_a_stop_after_the_output_ended_reports_the_exec_ended_without_a_kill() -> None:
    handle = _QueueExecHandle()
    handle.messages.put_nowait(None)
    await _drain(handle)
    assert await handle._stop() == KillResult(KillOutcome.EXEC_ENDED)
    assert handle.kills == 0


async def test_a_stop_once_the_exit_status_is_read_reports_the_exec_ended_without_a_kill() -> None:
    handle = _QueueExecHandle()
    handle.status_read = True
    assert await handle._stop() == KillResult(KillOutcome.EXEC_ENDED)
    assert handle.kills == 0


async def test_a_kill_that_signalled_the_group_ends_the_output_with_the_killed_status() -> None:
    handle = _QueueExecHandle()
    handle.messages.put_nowait(("stdout", b"tail"))
    await handle.kill()
    assert await _drain(handle) == [
        SandboxStreamChunk(stream="stdout", data=b"tail"),
        SandboxStreamExit(exit_code=_KILLED),
    ]
    await handle.kill()
    assert handle.kills == 1


async def test_overlapping_stops_share_one_kill_and_its_result() -> None:
    handle = _QueueExecHandle(kill_result=KillResult(KillOutcome.NOTHING_RUNNING))
    handle.kill_gate.clear()
    stops = [asyncio.ensure_future(handle._stop()) for _ in range(3)]
    await asyncio.sleep(0)
    handle.kill_gate.set()
    assert await asyncio.gather(*stops) == [KillResult(KillOutcome.NOTHING_RUNNING)] * 3
    assert handle.kills == 1
    assert await handle._stop() == KillResult(KillOutcome.NOTHING_RUNNING)
    assert handle.kills == 2


async def test_a_failed_kill_raises_to_the_caller_and_leaves_the_exec_as_it_was() -> None:
    handle = _QueueExecHandle()

    async def failing_kill() -> KillResult:
        raise SandboxError("kill refused")

    handle._kill_exec = failing_kill  # type: ignore[method-assign]
    with pytest.raises(SandboxError, match=r"^kill refused$"):
        await handle.kill()
    await handle.write_stdin(b"still open")
    assert handle.written == [b"still open"]


async def test_after_a_kill_that_found_nothing_running_the_output_reads_on_for_the_exec_own_end() -> None:
    handle = _QueueExecHandle(kill_result=KillResult(KillOutcome.NOTHING_RUNNING))
    await handle.kill()
    handle.messages.put_nowait(("stdout", b"last"))
    handle.messages.put_nowait(None)
    handle.exit_code = 0
    assert await _drain(handle) == [SandboxStreamChunk(stream="stdout", data=b"last"), SandboxStreamExit(exit_code=0)]


async def test_a_stream_still_open_past_the_grace_raises_and_takes_stdin_with_it() -> None:
    handle = _QueueExecHandle(kill_result=KillResult(KillOutcome.NOTHING_RUNNING))
    await handle.kill()
    with pytest.raises(SandboxError, match=r"^the stream stayed open after the kill$"):
        await _drain(handle)
    assert handle.stream_closed
    with pytest.raises(SandboxError, match=r"^cannot write stdin: the sandbox exec stream is closed$"):
        await handle.write_stdin(b"x")


async def test_the_deadline_stops_the_exec_and_times_out_with_what_was_read() -> None:
    handle = _QueueExecHandle(deadline=ExecDeadline.from_now(0.05))
    handle.messages.put_nowait(("stdout", b"abc"))
    handle.messages.put_nowait(("stderr", b"de"))
    with pytest.raises(SandboxExecTimeoutError) as raised:
        await _drain(handle)
    assert (raised.value.stdout_len, raised.value.stderr_len) == (3, 2)
    assert handle.kills == 1


async def test_the_deadline_runs_from_the_exec_start_not_from_the_first_read() -> None:
    # The exec started a whole timeout ago: its first read is already past the deadline.
    started = asyncio.get_running_loop().time() - 10.0
    handle = _QueueExecHandle(deadline=ExecDeadline(timeout_seconds=10.0, at=started + 10.0))
    handle.messages.put_nowait(("stdout", b"abc"))
    with pytest.raises(SandboxExecTimeoutError) as raised:
        await asyncio.wait_for(_drain(handle), 5)
    assert (raised.value.timeout_seconds, raised.value.stdout_len) == (10.0, 3)
    assert handle.kills == 1


async def test_a_stream_ended_within_the_deadline_gives_its_output_and_exit_however_late_it_is_read() -> None:
    handle = _QueueExecHandle(deadline=ExecDeadline.from_now(0.05), kill_result=KillResult(KillOutcome.NOTHING_RUNNING))
    handle.messages.put_nowait(("stdout", b"done"))
    handle.exit_code = 3
    handle.end_stream()
    # The output is first read well after the deadline.
    await asyncio.sleep(0.15)
    assert await asyncio.wait_for(_drain(handle), 5) == [
        SandboxStreamChunk(stream="stdout", data=b"done"),
        SandboxStreamExit(exit_code=3),
    ]
    # The deadline asked the exec's runtime, which found nothing running.
    assert handle.kills == 1


async def test_an_exec_running_at_the_deadline_times_out_though_its_stream_ended_before_the_read() -> None:
    handle = _QueueExecHandle(deadline=ExecDeadline.from_now(0.05))
    handle.messages.put_nowait(("stdout", b"late"))
    await asyncio.sleep(0.1)
    with pytest.raises(SandboxExecTimeoutError) as raised:
        await asyncio.wait_for(_drain(handle), 5)
    assert raised.value.stdout_len == 4
    assert handle.kills == 1


async def test_the_deadline_stops_an_exec_nobody_reads_and_counts_the_output_its_stop_dropped() -> None:
    handle = _DroppingCloseHandle(deadline=ExecDeadline.from_now(0.05))
    handle.messages.put_nowait(("stdout", b"abc"))
    handle.messages.put_nowait(("stdout", b""))  # a message that carries no output counts nothing
    handle.messages.put_nowait(("stderr", b"de"))
    # Nothing reads the output: the deadline alone stops the exec.
    await asyncio.sleep(0.15)
    assert handle.kills == 1
    with pytest.raises(SandboxError, match=r"^cannot write stdin: the sandbox exec has exited$"):
        await handle.write_stdin(b"x")
    await handle.close_stdin()
    assert not handle.input_ended
    with pytest.raises(SandboxExecTimeoutError) as raised:
        await asyncio.wait_for(_drain(handle), 5)
    assert (raised.value.stdout_len, raised.value.stderr_len) == (3, 2)


async def test_a_write_racing_the_deadline_stop_waits_for_it_and_raises() -> None:
    handle = _QueueExecHandle(deadline=ExecDeadline.from_now(0.05))
    handle.kill_gate.clear()
    while handle.kills == 0:
        await asyncio.sleep(0.01)
    write = asyncio.ensure_future(handle.write_stdin(b"x"))
    await asyncio.sleep(0.01)
    assert not write.done()
    handle.kill_gate.set()
    with pytest.raises(SandboxError, match=r"^cannot write stdin: the sandbox exec has exited$"):
        await write
    assert handle.written == []


async def test_a_deadline_stop_that_fails_is_noted_on_the_timeout_the_next_read_raises() -> None:
    handle = _QueueExecHandle(deadline=ExecDeadline.from_now(0.05))

    async def failing_kill() -> KillResult:
        handle.kills += 1
        raise SandboxError("kill refused")

    handle._kill_exec = failing_kill  # type: ignore[method-assign]
    await asyncio.sleep(0.15)
    assert handle.kills == 1
    with pytest.raises(SandboxExecTimeoutError) as raised:
        await asyncio.wait_for(_drain(handle), 5)
    assert raised.value.__notes__ == ["the kill of the sandbox exec also failed: kill refused"]
    assert handle.kills == 1


async def test_a_callers_kill_after_the_deadline_stop_ends_the_output_with_the_exit() -> None:
    handle = _QueueExecHandle(deadline=ExecDeadline.from_now(0.05))
    handle.messages.put_nowait(("stdout", b"tail"))
    await asyncio.sleep(0.15)
    await handle.kill()
    # The deadline's stop counted the stream to its end; the caller's kill wins over the
    # timeout, so the output the stream carried is still read, then the exit.
    assert await asyncio.wait_for(_drain(handle), 5) == [
        SandboxStreamChunk(stream="stdout", data=b"tail"),
        SandboxStreamExit(exit_code=_KILLED),
    ]
    assert handle.kills == 1


async def test_a_callers_kill_before_the_deadline_ends_the_deadline_timer() -> None:
    handle = _QueueExecHandle(deadline=ExecDeadline.from_now(0.05), kill_result=KillResult(KillOutcome.NOTHING_RUNNING))
    await handle.kill()
    assert not handle._deadline_stop.armed
    await asyncio.sleep(0.1)
    assert handle.kills == 1


async def test_a_kill_made_while_the_deadline_kill_is_in_flight_ends_the_output_with_the_exit() -> None:
    handle = _QueueExecHandle(deadline=ExecDeadline.from_now(0.05))
    handle.kill_gate.clear()
    reader = asyncio.ensure_future(_drain(handle))
    while handle.kills == 0:
        await asyncio.sleep(0.01)
    caller_kill = asyncio.ensure_future(handle.kill())
    await asyncio.sleep(0)
    handle.kill_gate.set()
    await caller_kill
    assert await reader == [SandboxStreamExit(exit_code=_KILLED)]
    assert handle.kills == 1


async def test_a_stream_cut_off_before_the_exit_status_leaves_the_exec_to_a_kill() -> None:
    handle = _QueueExecHandle()
    handle.status_lost = True
    handle.messages.put_nowait(None)
    with pytest.raises(SandboxError, match=r"^sandbox exec stream ended without an exit status$"):
        await _drain(handle)
    with pytest.raises(SandboxError, match=r"^cannot write stdin: the sandbox exec stream is closed$"):
        await handle.write_stdin(b"x")
    await handle.kill()
    assert handle.kills == 1


async def test_stdin_is_gone_once_the_stream_ends_while_the_exit_status_is_read() -> None:
    handle = _QueueExecHandle()
    status_read = asyncio.Event()
    release = asyncio.Event()

    async def slow_status() -> int:
        status_read.set()
        await release.wait()
        return 0

    handle._exit_status = slow_status  # type: ignore[method-assign]
    handle.messages.put_nowait(None)
    reader = asyncio.ensure_future(_drain(handle))
    await status_read.wait()
    with pytest.raises(SandboxError, match=r"^cannot write stdin: the sandbox exec stream is closed$"):
        await handle.write_stdin(b"x")
    assert handle.written == []
    release.set()
    assert await reader == [SandboxStreamExit(exit_code=0)]
    with pytest.raises(SandboxError, match=r"^cannot write stdin: the sandbox exec has exited$"):
        await handle.write_stdin(b"x")


async def test_a_stream_lost_before_the_deadline_still_has_its_exec_killed_at_the_deadline() -> None:
    handle = _QueueExecHandle(deadline=ExecDeadline.from_now(0.05))
    handle.messages.put_nowait(("stdout", b"cut"))
    # The connection drops: the stream ends without the exit status while the command runs on.
    handle.status_lost = True
    handle.end_stream()
    await asyncio.sleep(0.15)
    assert handle.kills == 1
    with pytest.raises(SandboxExecTimeoutError) as raised:
        await asyncio.wait_for(_drain(handle), 5)
    assert raised.value.stdout_len == 3


async def test_a_stream_lost_and_read_before_the_deadline_has_its_exec_killed_at_the_deadline() -> None:
    handle = _QueueExecHandle(deadline=ExecDeadline.from_now(0.05))
    handle.status_lost = True
    handle.end_stream()
    with pytest.raises(SandboxError, match=r"^sandbox exec stream ended without an exit status$"):
        await _drain(handle)
    assert handle.kills == 0
    await asyncio.sleep(0.15)
    assert handle.kills == 1


@pytest.mark.parametrize("outcome", [KillOutcome.NOTHING_RUNNING, KillOutcome.EXEC_ENDED, KillOutcome.NOT_STARTED])
async def test_a_deadline_stop_that_finds_the_exec_ended_gives_all_its_output_and_its_exit(
    outcome: KillOutcome, caplog: pytest.LogCaptureFixture
) -> None:
    handle = _QueueExecHandle(deadline=ExecDeadline.from_now(0.05), kill_result=KillResult(outcome))
    handle.exit_code = 0
    # The exec ended in time; the rest of its output waits in a transport that stopped reading.
    handle.messages.put_nowait(("stdout", b"first"))
    await asyncio.sleep(0.15)
    assert handle.kills == 1
    handle.messages.put_nowait(("stdout", b"rest"))
    handle.end_stream()
    assert await asyncio.wait_for(_drain(handle), 5) == [
        SandboxStreamChunk(stream="stdout", data=b"first"),
        SandboxStreamChunk(stream="stdout", data=b"rest"),
        SandboxStreamExit(exit_code=0),
    ]
    assert handle.kills == 1
    assert [record for record in caplog.records if record.levelname in ("WARNING", "ERROR")] == []


async def test_a_deadline_stop_that_finds_the_session_gone_ends_the_output_without_a_timeout() -> None:
    handle = _QueueExecHandle(deadline=ExecDeadline.from_now(0.05), kill_result=KillResult(KillOutcome.SESSION_GONE))
    handle.messages.put_nowait(("stdout", b"tail"))
    await asyncio.sleep(0.15)
    assert await asyncio.wait_for(_drain(handle), 5) == [
        SandboxStreamChunk(stream="stdout", data=b"tail"),
        SandboxStreamExit(exit_code=_KILLED),
    ]
    assert handle.kills == 1


async def test_an_exec_its_stream_shows_ended_is_not_killed_at_the_deadline() -> None:
    handle = _QueueExecHandle(deadline=ExecDeadline.from_now(0.05))
    handle.ended_in_stream = True
    handle.messages.put_nowait(("stdout", b"x"))
    await asyncio.sleep(0.15)
    handle.end_stream()
    assert await asyncio.wait_for(_drain(handle), 5) == [
        SandboxStreamChunk(stream="stdout", data=b"x"),
        SandboxStreamExit(exit_code=0),
    ]
    assert handle.kills == 0


async def test_the_timeout_counts_the_output_the_stream_carried_after_the_deadline_kill() -> None:
    handle = _DroppingCloseHandle(deadline=ExecDeadline.from_now(0.05))
    handle.messages.put_nowait(("stdout", b"abc"))
    # Output the exec wrote before its kill that its transport delivers only after it.
    handle.after_kill = [("stdout", b"defg"), ("stderr", b"hi")]
    await asyncio.sleep(0.15)
    with pytest.raises(SandboxExecTimeoutError) as raised:
        await asyncio.wait_for(_drain(handle), 5)
    assert (raised.value.stdout_len, raised.value.stderr_len) == (7, 2)
    assert getattr(raised.value, "__notes__", []) == []


async def test_a_stream_still_open_past_the_grace_after_the_deadline_kill_is_noted_on_the_timeout() -> None:
    handle = _DroppingCloseHandle(deadline=ExecDeadline.from_now(0.05))
    handle.stream_outlives_kill = True
    handle.messages.put_nowait(("stdout", b"abc"))
    with pytest.raises(SandboxExecTimeoutError) as raised:
        await asyncio.wait_for(_drain(handle), 5)
    assert raised.value.stdout_len == 3
    assert raised.value.__notes__ == [
        "the timeout's count may miss output: the sandbox exec stream did not end within 0.05s of the kill"
    ]


async def test_a_stream_error_met_counting_the_timeout_is_noted_on_it_and_is_its_cause() -> None:
    handle = _QueueExecHandle(deadline=ExecDeadline.from_now(0.05))
    handle.messages.put_nowait(("stdout", b"abc"))
    handle.messages.put_nowait(("error", b"stream broke"))
    handle.messages.put_nowait(("stderr", b"de"))
    # Nothing reads before the deadline: the stream error is met only by the timeout's count.
    await asyncio.sleep(0.15)
    with pytest.raises(SandboxExecTimeoutError) as raised:
        await asyncio.wait_for(_drain(handle), 5)
    assert (raised.value.stdout_len, raised.value.stderr_len) == (3, 2)
    assert raised.value.__notes__ == [
        "the timeout's count skipped a sandbox exec stream message that failed: stream broke"
    ]
    assert isinstance(raised.value.__cause__, SandboxError)
    assert str(raised.value.__cause__) == "stream broke"


async def test_a_stream_read_failing_after_the_deadline_kill_is_noted_on_the_timeout_and_is_its_cause() -> None:
    handle = _QueueExecHandle(deadline=ExecDeadline.from_now(0.05))
    handle.messages.put_nowait(("stdout", b"abc"))
    cut = SandboxError("connection lost")
    handle.after_kill = [cut]
    with pytest.raises(SandboxExecTimeoutError) as raised:
        await asyncio.wait_for(_drain(handle), 5)
    assert raised.value.stdout_len == 3
    assert raised.value.__notes__ == [
        "the timeout's count may miss output: reading the sandbox exec stream after the kill failed: connection lost"
    ]
    assert raised.value.__cause__ is cut


async def test_a_stream_read_failing_after_the_deadline_kill_raises_once_a_callers_kill_wins() -> None:
    handle = _QueueExecHandle(deadline=ExecDeadline.from_now(0.05))
    handle.messages.put_nowait(("stdout", b"abc"))
    handle.after_kill = [SandboxError("connection lost")]
    await asyncio.sleep(0.15)
    await handle.kill()
    output = handle.output
    assert await anext(output) == SandboxStreamChunk(stream="stdout", data=b"abc")
    with pytest.raises(SandboxError, match=r"^connection lost$"):
        await anext(output)


async def _drain_slowly(handle: SiblingKillExecHandle, pause: float) -> list[SandboxStreamChunk | SandboxStreamExit]:
    """Read the whole output, pausing ``pause`` seconds after each item, as a slow consumer does."""
    items: list[SandboxStreamChunk | SandboxStreamExit] = []
    async for item in handle.output:
        items.append(item)
        await asyncio.sleep(pause)
    return items


_SIX_CHUNKS = [SandboxStreamChunk(stream="stdout", data=b"x" * 10) for _ in range(6)]


@pytest.mark.parametrize("ended_in_stream", [False, True])
async def test_an_exec_ended_in_time_gives_all_its_output_to_a_reader_slower_than_the_grace(
    ended_in_stream: bool,
) -> None:
    handle = _QueueExecHandle(deadline=ExecDeadline.from_now(0.05), kill_result=KillResult(KillOutcome.NOTHING_RUNNING))
    for chunk in _SIX_CHUNKS:
        handle.messages.put_nowait((chunk.stream, chunk.data))
    handle.end_stream()
    # The deadline passes unread, and its kill finds nothing running: the exec ended in time.
    await asyncio.sleep(0.1)
    handle.ended_in_stream = ended_in_stream
    # The reader takes three times the grace over the output it already has.
    items = await asyncio.wait_for(_drain_slowly(handle, handle._status_grace_seconds * 0.6), 5)
    assert items == [*_SIX_CHUNKS, SandboxStreamExit(exit_code=0)]
    assert handle.kills == 1


async def test_after_a_kill_that_found_nothing_running_a_reader_slower_than_the_grace_gets_all_the_output() -> None:
    handle = _QueueExecHandle(kill_result=KillResult(KillOutcome.NOTHING_RUNNING))
    await handle.kill()
    for chunk in _SIX_CHUNKS:
        handle.messages.put_nowait((chunk.stream, chunk.data))
    handle.end_stream()
    items = await asyncio.wait_for(_drain_slowly(handle, handle._status_grace_seconds * 0.6), 5)
    assert items == [*_SIX_CHUNKS, SandboxStreamExit(exit_code=0)]


async def test_output_that_arrives_while_a_slow_reader_is_away_is_read_without_spending_the_grace() -> None:
    handle = _QueueExecHandle(kill_result=KillResult(KillOutcome.NOTHING_RUNNING))
    await handle.kill()
    loop = asyncio.get_running_loop()
    pause = handle._status_grace_seconds * 0.8
    # Each message lands while the reader is away, so it never waits on the stream.
    for index, chunk in enumerate(_SIX_CHUNKS):
        loop.call_later(pause * index + pause / 4, handle.messages.put_nowait, (chunk.stream, chunk.data))
    loop.call_later(pause * len(_SIX_CHUNKS) + pause / 4, handle.end_stream)
    output = handle.output
    await asyncio.sleep(pause / 2)
    items: list[SandboxStreamChunk | SandboxStreamExit] = []
    async for item in output:
        items.append(item)
        await asyncio.sleep(pause)
    assert items == [*_SIX_CHUNKS, SandboxStreamExit(exit_code=0)]


async def test_the_grace_counts_every_wait_on_the_stream_together() -> None:
    # The command runs on after the kill, writing a little at a time: no one wait reaches the
    # grace, but together they do.
    handle = _QueueExecHandle(kill_result=KillResult(KillOutcome.NOTHING_RUNNING))
    await handle.kill()
    loop = asyncio.get_running_loop()
    step = handle._status_grace_seconds * 0.4
    for index in range(1, 20):
        loop.call_later(step * index, handle.messages.put_nowait, ("stdout", b"tick"))
    received: list[SandboxStreamChunk | SandboxStreamExit] = []

    async def _drain() -> None:
        async with asyncio.timeout(5):
            async for item in handle.output:
                received.append(item)

    with pytest.raises(SandboxError, match=r"^the stream stayed open after the kill$"):
        await _drain()
    assert 0 < len(received) < 19


@pytest.mark.parametrize("deadline_kill", [True, False])
async def test_an_exec_its_stream_shows_ended_reads_on_past_the_grace_to_its_end(deadline_kill: bool) -> None:
    handle = _QueueExecHandle(
        deadline=ExecDeadline.from_now(0.05 if deadline_kill else 10.0),
        kill_result=KillResult(KillOutcome.NOTHING_RUNNING),
    )
    if deadline_kill:
        await asyncio.sleep(0.1)
    else:
        await handle.kill()
    assert handle.kills == 1
    # The stream shows the exec ended; its end comes once the reader has waited past the grace.
    handle.ended_in_stream = True
    handle.messages.put_nowait(("stdout", b"tail"))
    asyncio.get_running_loop().call_later(handle._status_grace_seconds * 1.5, handle.end_stream)
    assert await asyncio.wait_for(_drain(handle), 5) == [
        SandboxStreamChunk(stream="stdout", data=b"tail"),
        SandboxStreamExit(exit_code=0),
    ]


class _Message:
    """A stream message the test can watch for collection: ``(stream, data)``, unpacked as a tuple."""

    __slots__ = ("__weakref__", "data", "stream")

    def __init__(self, stream: str, data: bytes) -> None:
        self.stream = stream
        self.data = data

    def __iter__(self) -> Iterator[Any]:
        return iter((self.stream, self.data))


class _RunawayWriter:
    """A process the command started outside its group, writing to the exec's stream until stopped."""

    def __init__(self, handle: _QueueExecHandle, *, interval: float = 0.001) -> None:
        self._handle = handle
        self._interval = interval
        self.written = 0
        self._task = asyncio.ensure_future(self._write())

    async def _write(self) -> None:
        while True:
            self._handle.messages.put_nowait(("stdout", b"held"))
            self.written += 4
            await asyncio.sleep(self._interval)

    async def stop(self) -> None:
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task


async def _received_until_timeout(handle: _QueueExecHandle, pause: float) -> tuple[int, SandboxExecTimeoutError]:
    """Read the output, pausing ``pause`` after each chunk, until it raises its timeout: the bytes received and it."""
    received = 0

    async def _read() -> None:
        nonlocal received
        async for item in handle.output:
            assert isinstance(item, SandboxStreamChunk)
            received += len(item.data)
            await asyncio.sleep(pause)

    with pytest.raises(SandboxExecTimeoutError) as raised:
        await _read()
    return received, raised.value


@pytest.mark.parametrize("outcome", [KillOutcome.NOTHING_RUNNING, KillOutcome.SIGNALLED])
@pytest.mark.parametrize(("pause", "unread"), [(0.0, False), (0.02, False), (0.0, True)])
async def test_an_exec_whose_output_a_process_still_holds_at_the_deadline_times_out_at_once(
    outcome: KillOutcome, pause: float, unread: bool
) -> None:
    # The command's process has exited, or its group is killed, but a process it started outside
    # its group writes on faster than the reader reads.
    handle = _QueueExecHandle(deadline=ExecDeadline.from_now(0.1), kill_result=KillResult(outcome, output_held=True))
    handle.stream_outlives_kill = True
    writer = _RunawayWriter(handle)
    try:
        if unread:
            await asyncio.sleep(0.2)
        loop = asyncio.get_running_loop()
        started = loop.time()
        received, timeout = await asyncio.wait_for(_received_until_timeout(handle, pause), 5)
        assert loop.time() - started < 1.0
    finally:
        await writer.stop()
    assert timeout.timeout_seconds == 0.1
    assert timeout.stdout_len >= received
    assert timeout.__notes__ == [OUTPUT_HELD_NOTE]
    assert handle.stream_closed or outcome is KillOutcome.SIGNALLED
    assert handle.kills == 1


async def test_a_signalled_exec_with_a_holder_counts_what_its_stream_had_received_at_the_stop() -> None:
    handle = _QueueExecHandle(
        deadline=ExecDeadline.from_now(0.05), kill_result=KillResult(KillOutcome.SIGNALLED, output_held=True)
    )
    handle.stream_outlives_kill = True
    handle.messages.put_nowait(("stdout", b"abc"))
    handle.messages.put_nowait(("stderr", b"de"))
    await asyncio.sleep(0.15)
    with pytest.raises(SandboxExecTimeoutError) as raised:
        await asyncio.wait_for(_drain(handle), 5)
    assert (raised.value.stdout_len, raised.value.stderr_len) == (3, 2)
    assert raised.value.__notes__ == [OUTPUT_HELD_NOTE]


async def test_a_signalled_exec_with_no_holder_counts_its_whole_stream_and_keeps_none_of_it() -> None:
    # The stream ends by itself once the kill has returned; the drain is bounded by the grace
    # in wall-clock time, so the grace here is one the counting of the tail never reaches.
    handle = _LongGraceHandle(deadline=ExecDeadline.from_now(0.05))
    handle.stream_outlives_kill = True
    watched: list[weakref.ref[_Message]] = []
    kill_exec = handle._kill_exec

    async def kill_then_flush() -> KillResult:
        result = await kill_exec()
        # The group's last output, then the stream's end, arrive once the kill has returned.
        for _ in range(1000):
            message = _Message("stdout", b"x" * 100)
            watched.append(weakref.ref(message))
            handle.messages.put_nowait(message)
        handle.end_stream()
        return result

    handle._kill_exec = kill_then_flush  # type: ignore[method-assign]
    await asyncio.sleep(0.15)
    with pytest.raises(SandboxExecTimeoutError) as raised:
        await asyncio.wait_for(_drain(handle), 5)
    assert (raised.value.stdout_len, raised.value.stderr_len) == (100_000, 0)
    assert getattr(raised.value, "__notes__", []) == []
    gc.collect()
    assert [ref for ref in watched if ref() is not None] == []


async def test_a_deadline_stop_that_found_the_exec_over_raises_the_grace_error_on_a_stream_that_never_ends() -> None:
    handle = _QueueExecHandle(deadline=ExecDeadline.from_now(0.05), kill_result=KillResult(KillOutcome.NOTHING_RUNNING))
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(SandboxError, match=r"^the stream stayed open after the kill$"):
        await asyncio.wait_for(_drain(handle), 5)
    # The deadline's stop, then one grace of the reader's waiting.
    assert loop.time() - started >= 0.05 + handle._status_grace_seconds
    assert handle.stream_closed
    assert handle.kills == 1


async def test_a_late_slow_read_of_a_large_output_ended_in_time_yields_all_of_it() -> None:
    handle = _QueueExecHandle(deadline=ExecDeadline.from_now(0.05), kill_result=KillResult(KillOutcome.NOTHING_RUNNING))
    chunks = [SandboxStreamChunk(stream="stdout", data=bytes([index % 256]) * 64) for index in range(2000)]
    for chunk in chunks:
        handle.messages.put_nowait((chunk.stream, chunk.data))
    handle.end_stream()
    await asyncio.sleep(0.1)
    items: list[SandboxStreamChunk | SandboxStreamExit] = []
    async for item in handle.output:
        items.append(item)
        if len(items) % 100 == 0:
            await asyncio.sleep(handle._status_grace_seconds * 0.6)
    assert items == [*chunks, SandboxStreamExit(exit_code=0)]


async def test_a_callers_kill_after_which_a_process_holds_the_output_cuts_the_stream() -> None:
    handle = _QueueExecHandle(kill_result=KillResult(KillOutcome.NOTHING_RUNNING, output_held=True))
    writer = _RunawayWriter(handle)
    try:
        await asyncio.sleep(0.02)
        await handle.kill()
        items = await asyncio.wait_for(_drain(handle), 1)
    finally:
        await writer.stop()
    assert items[-1] == SandboxStreamExit(exit_code=0)
    assert all(isinstance(item, SandboxStreamChunk) for item in items[:-1])
    assert handle.stream_closed


async def test_a_reader_waiting_when_a_callers_kill_returns_wakes_and_waits_only_the_grace() -> None:
    handle = _QueueExecHandle(kill_result=KillResult(KillOutcome.NOTHING_RUNNING))
    drain = asyncio.ensure_future(_drain(handle))
    await asyncio.sleep(0.02)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await handle.kill()
    with pytest.raises(SandboxError, match=r"^the stream stayed open after the kill$"):
        await asyncio.wait_for(drain, 5)
    # The deadline is ten seconds off: the kill's wake ended the wait on it.
    assert loop.time() - started < 1.0


async def test_a_kill_whose_provider_holds_the_status_at_once_still_yields_the_output_received_before_it() -> None:
    handle = _StatusAtKillHandle()
    handle.messages.put_nowait(("stdout", b"before"))
    await handle.kill()
    assert await asyncio.wait_for(_drain(handle), 5) == [
        SandboxStreamChunk(stream="stdout", data=b"before"),
        SandboxStreamExit(exit_code=_KILLED),
    ]


async def test_a_callers_kill_after_which_a_process_holds_the_output_yields_the_output_received_before_it() -> None:
    handle = _QueueExecHandle(kill_result=KillResult(KillOutcome.SIGNALLED, output_held=True))
    handle.stream_outlives_kill = True
    handle.messages.put_nowait(("stdout", b"abc"))
    handle.messages.put_nowait(("stderr", b"de"))
    await handle.kill()
    assert await asyncio.wait_for(_drain(handle), 5) == [
        SandboxStreamChunk(stream="stdout", data=b"abc"),
        SandboxStreamChunk(stream="stderr", data=b"de"),
        SandboxStreamExit(exit_code=_KILLED),
    ]
    assert handle.stream_closed


async def test_a_callers_kill_after_the_deadline_cut_a_held_stream_yields_what_the_stream_had_received() -> None:
    handle = _QueueExecHandle(
        deadline=ExecDeadline.from_now(0.05), kill_result=KillResult(KillOutcome.SIGNALLED, output_held=True)
    )
    handle.stream_outlives_kill = True
    handle.messages.put_nowait(("stdout", b"abc"))
    handle.messages.put_nowait(("stderr", b"de"))
    await asyncio.sleep(0.15)
    await handle.kill()
    assert await asyncio.wait_for(_drain(handle), 5) == [
        SandboxStreamChunk(stream="stdout", data=b"abc"),
        SandboxStreamChunk(stream="stderr", data=b"de"),
        SandboxStreamExit(exit_code=_KILLED),
    ]


async def test_a_deadline_timeout_keeps_nothing_of_the_output_it_counted() -> None:
    handle = _QueueExecHandle(
        deadline=ExecDeadline.from_now(0.05), kill_result=KillResult(KillOutcome.SIGNALLED, output_held=True)
    )
    handle.stream_outlives_kill = True
    message = _Message("stdout", b"abc")
    watched = weakref.ref(message)
    handle.messages.put_nowait(message)
    del message
    await asyncio.sleep(0.15)
    with pytest.raises(SandboxExecTimeoutError):
        await asyncio.wait_for(_drain(handle), 5)
    gc.collect()
    assert watched() is None


async def test_a_deadline_cut_of_a_held_stream_counts_what_its_close_took_in() -> None:
    handle = _TakesInAtCloseHandle(
        deadline=ExecDeadline.from_now(0.05), kill_result=KillResult(KillOutcome.SIGNALLED, output_held=True)
    )
    handle.stream_outlives_kill = True
    handle.messages.put_nowait(("stdout", b"abc"))
    await asyncio.sleep(0.15)
    with pytest.raises(SandboxExecTimeoutError) as raised:
        await asyncio.wait_for(_drain(handle), 5)
    assert raised.value.stdout_len == 7


async def test_a_callers_kill_after_the_deadline_cut_a_held_stream_yields_what_its_close_took_in() -> None:
    handle = _TakesInAtCloseHandle(
        deadline=ExecDeadline.from_now(0.05), kill_result=KillResult(KillOutcome.SIGNALLED, output_held=True)
    )
    handle.stream_outlives_kill = True
    handle.messages.put_nowait(("stdout", b"abc"))
    await asyncio.sleep(0.15)
    await handle.kill()
    assert await asyncio.wait_for(_drain(handle), 5) == [
        SandboxStreamChunk(stream="stdout", data=b"abc"),
        SandboxStreamChunk(stream="stdout", data=b"held"),
        SandboxStreamExit(exit_code=_KILLED),
    ]
