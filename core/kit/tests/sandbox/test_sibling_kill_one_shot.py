"""The one-shot exec a sibling exec kills: its run within its timeout, and what the timeout counts."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

import anyio
import pytest
from tai42_contract.sandbox import SandboxError, SandboxExecTimeoutError, SandboxStreamChunk

from tai42_kit.sandbox.exec_kill import OUTPUT_HELD_NOTE, KillOutcome, KillResult
from tai42_kit.sandbox.sibling_kill_one_shot import OneShotOutput, run_one_shot

_EXEC_ID = "exec-1"
_EXIT_CODE = 3


class _Exec:
    """An exec's output stream, read to its end by :meth:`run`, and the sibling kill that stops it.

    The kill delivers ``tail``, the output still on its way when it signalled the exec, and then
    ends the stream, unless a process holds the output (``held``) or the stream stays open.
    """

    def __init__(
        self,
        *,
        tail: list[SandboxStreamChunk] | None = None,
        held: bool = False,
        stays_open: bool = False,
        kill_error: BaseException | None = None,
        kill_seconds: float = 0.0,
        read_error_after_kill: BaseException | None = None,
    ) -> None:
        self.output = OneShotOutput()
        self.stream: asyncio.Queue[SandboxStreamChunk | None] = asyncio.Queue()
        self._tail = tail or []
        self._held = held
        self._stays_open = stays_open
        self._kill_error = kill_error
        self._kill_seconds = kill_seconds
        self._read_error_after_kill = read_error_after_kill
        self.kills = 0
        self.read_cut = False

    async def run(self) -> int:
        try:
            while (chunk := await self.stream.get()) is not None:
                self.output.add(chunk)
        except asyncio.CancelledError:
            self.read_cut = True
            raise
        if self._read_error_after_kill is not None and self.kills:
            raise self._read_error_after_kill
        return _EXIT_CODE

    async def kill(self) -> KillResult:
        self.kills += 1
        await asyncio.sleep(self._kill_seconds)
        for chunk in self._tail:
            self.stream.put_nowait(chunk)
        if self._kill_error is not None:
            raise self._kill_error
        if not (self._held or self._stays_open):
            self.stream.put_nowait(None)
        return KillResult(KillOutcome.SIGNALLED, output_held=self._held)

    def call(self, *, timeout_seconds: float = 0.05, grace_seconds: float = 5.0) -> Coroutine[Any, Any, int]:
        return run_one_shot(
            self.run(),
            self.output,
            timeout_seconds=timeout_seconds,
            kill=self.kill,
            exec_id=_EXEC_ID,
            grace_seconds=grace_seconds,
            timeout_error=lambda: SandboxExecTimeoutError(
                timeout_seconds=timeout_seconds,
                stdout_len=self.output.stdout_len,
                stderr_len=self.output.stderr_len,
            ),
        )


def _chunk(stream: str, size: int) -> SandboxStreamChunk:
    return SandboxStreamChunk(stream="stderr" if stream == "stderr" else "stdout", data=b"x" * size)


def test_the_output_keeps_what_is_added_and_counts_it_after_it_stops_keeping() -> None:
    output = OneShotOutput()
    output.add(SandboxStreamChunk(stream="stdout", data=b"out"))
    output.add(SandboxStreamChunk(stream="stderr", data=b"err"))
    assert (bytes(output.stdout), bytes(output.stderr)) == (b"out", b"err")

    output.stop_keeping()
    output.add(SandboxStreamChunk(stream="stdout", data=b"more"))
    assert (bytes(output.stdout), bytes(output.stderr)) == (b"", b"")
    assert (output.stdout_len, output.stderr_len) == (7, 3)


async def test_a_run_that_ends_in_time_returns_its_result_without_a_kill() -> None:
    exec_ = _Exec()
    exec_.stream.put_nowait(_chunk("stdout", 4))
    exec_.stream.put_nowait(None)

    assert await asyncio.wait_for(exec_.call(timeout_seconds=5), 5) == _EXIT_CODE
    assert exec_.kills == 0
    assert bytes(exec_.output.stdout) == b"xxxx"


async def test_a_run_that_fails_in_time_is_killed_and_raises_its_error() -> None:
    exec_ = _Exec()

    async def failing() -> int:
        raise SandboxError("engine error")

    call = run_one_shot(
        failing(),
        exec_.output,
        timeout_seconds=5,
        kill=exec_.kill,
        exec_id=_EXEC_ID,
        grace_seconds=5,
        timeout_error=lambda: AssertionError("no timeout"),
    )
    with pytest.raises(SandboxError, match="engine error"):
        await asyncio.wait_for(call, 5)
    assert exec_.kills == 1


async def test_a_run_that_fails_before_its_exec_started_raises_its_error_without_a_kill() -> None:
    exec_ = _Exec()

    async def failing() -> int:
        raise SandboxError("never started")

    call = run_one_shot(
        failing(),
        exec_.output,
        timeout_seconds=5,
        kill=exec_.kill,
        exec_id=_EXEC_ID,
        grace_seconds=5,
        timeout_error=lambda: AssertionError("no timeout"),
        exec_started=lambda exc: False,
    )
    with pytest.raises(SandboxError, match="never started"):
        await asyncio.wait_for(call, 5)
    assert exec_.kills == 0


async def test_a_timeout_counts_the_output_that_reached_the_read_until_the_stream_ended_after_the_kill() -> None:
    exec_ = _Exec(tail=[_chunk("stdout", 300), _chunk("stderr", 7)])
    exec_.stream.put_nowait(_chunk("stdout", 5))

    with pytest.raises(SandboxExecTimeoutError) as raised:
        await asyncio.wait_for(exec_.call(), 5)
    assert (raised.value.stdout_len, raised.value.stderr_len) == (305, 7)
    assert getattr(raised.value, "__notes__", []) == []
    assert not exec_.read_cut
    # The output past the timeout is counted, never kept.
    assert (bytes(exec_.output.stdout), bytes(exec_.output.stderr)) == (b"", b"")


async def test_a_timeout_whose_output_a_process_still_holds_cuts_the_read_and_notes_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    exec_ = _Exec(tail=[_chunk("stdout", 300)], held=True)

    with caplog.at_level(logging.WARNING, logger="tai42_kit"), pytest.raises(SandboxExecTimeoutError) as raised:
        await asyncio.wait_for(exec_.call(grace_seconds=30), 5)
    assert raised.value.__notes__ == [OUTPUT_HELD_NOTE]
    assert exec_.read_cut
    assert raised.value.stdout_len <= 300


async def test_a_timeout_whose_kill_fails_cuts_the_read_and_notes_the_kill() -> None:
    exec_ = _Exec(kill_error=SandboxError("kill: Operation not permitted"))

    with pytest.raises(SandboxExecTimeoutError) as raised:
        await asyncio.wait_for(exec_.call(grace_seconds=30), 5)
    assert raised.value.__notes__ == ["the kill of the sandbox exec also failed: kill: Operation not permitted"]
    assert exec_.read_cut


async def test_a_timeout_whose_stream_stays_open_past_the_grace_notes_the_count_may_miss_output() -> None:
    exec_ = _Exec(tail=[_chunk("stdout", 300)], stays_open=True)

    with pytest.raises(SandboxExecTimeoutError) as raised:
        await asyncio.wait_for(exec_.call(grace_seconds=0.1), 5)
    assert (raised.value.stdout_len, raised.value.stderr_len) == (300, 0)
    assert raised.value.__notes__ == [
        "the timeout's count may miss output: the read of the sandbox exec did not end within 0.1s of the kill"
    ]
    assert exec_.read_cut


async def test_a_timeout_whose_read_fails_after_the_kill_notes_it_and_raises_from_it() -> None:
    failure = SandboxError("no exit code")
    exec_ = _Exec(read_error_after_kill=failure)

    with pytest.raises(SandboxExecTimeoutError) as raised:
        await asyncio.wait_for(exec_.call(), 5)
    assert raised.value.__notes__ == [
        "the timeout's count may miss output: the read of the sandbox exec failed after the kill: no exit code"
    ]
    assert raised.value.__cause__ is failure


async def test_a_cancelled_call_cuts_the_read_kills_the_exec_and_raises_the_cancellation() -> None:
    exec_ = _Exec(kill_seconds=0.05)
    call = asyncio.ensure_future(exec_.call(timeout_seconds=30))
    await asyncio.sleep(0.02)
    call.cancel()

    with pytest.raises(asyncio.CancelledError):
        await call
    assert exec_.read_cut
    assert exec_.kills == 1


async def test_a_call_cancelled_by_an_anyio_scope_still_kills_the_exec() -> None:
    exec_ = _Exec(kill_seconds=0.05)

    with anyio.move_on_after(0.02) as scope:
        await exec_.call(timeout_seconds=30)
    assert scope.cancelled_caught
    assert exec_.read_cut
    assert exec_.kills == 1


async def test_a_call_cancelled_during_its_timeout_kill_waits_for_the_kill_and_raises_from_the_timeout() -> None:
    exec_ = _Exec(kill_seconds=0.1, stays_open=True)
    call = asyncio.ensure_future(exec_.call(timeout_seconds=0.02))
    await asyncio.sleep(0.06)
    call.cancel()

    with pytest.raises(asyncio.CancelledError) as raised:
        await call
    assert exec_.kills == 1
    assert exec_.read_cut
    assert isinstance(raised.value.__cause__, SandboxExecTimeoutError)
