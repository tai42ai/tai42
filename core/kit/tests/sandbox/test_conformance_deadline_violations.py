"""The deadline checks have teeth: each FAILS loudly against a provider that breaks exactly
the deadline property it certifies.

These drive the checks of :mod:`tai42_kit.sandbox.conformance_deadline` against deliberately
non-conformant fakes: an exec that ignores its timeout, a killed exec whose output never ends,
one that loses its output or its exit when read late, a timeout that miscounts what was
received or does not note a process still holding the output.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import ClassVar

import pytest
from pydantic import SecretStr
from tai42_contract.sandbox import (
    ExecResult,
    SandboxError,
    SandboxExecHandle,
    SandboxExecTimeoutError,
    SandboxStreamChunk,
    SandboxStreamExit,
)

from tai42_kit.sandbox import ManagedSandbox, SandboxConformanceConfig, conformance_deadline
from tai42_kit.sandbox.conformance_deadline import (
    check_exec_timeout,
    check_interactive_exec_timeout,
    check_interactive_timeout_counts_large_output,
    check_interactive_timeout_counts_output,
    check_kill_after_deadline,
    check_late_read_after_exit,
    check_late_read_of_large_output,
    check_output_closed_exec_killed_at_deadline,
    check_output_holder_at_deadline_times_out,
    check_signalled_exec_with_output_holder_times_out,
    check_slow_read_of_large_output,
    check_unread_exec_killed_at_deadline,
)
from tai42_kit.sandbox.exec_kill import OUTPUT_HELD_NOTE

from .fakes import FAKE_CONFORMANCE_CONFIG, FakeExecHandle, FakeSandbox, FakeSandboxSession, with_permissive_policy


class _KilledTimesOutHandle(FakeExecHandle):
    """Ends a killed exec in its deadline timeout instead of its exit item."""

    async def _stream(self) -> AsyncIterator[SandboxStreamChunk | SandboxStreamExit]:
        yield SandboxStreamChunk(stream="stdout", data=bytes(self._buffer))
        raise SandboxExecTimeoutError(timeout_seconds=1.0, stdout_len=len(self._buffer), stderr_len=0)


class _KilledTimesOutSession(FakeSandboxSession):
    handle_cls: ClassVar[type[FakeExecHandle]] = _KilledTimesOutHandle


class _KilledTimesOutSandbox(FakeSandbox):
    session_cls: ClassVar[type[FakeSandboxSession]] = _KilledTimesOutSession


class _LateReadTimesOutHandle(FakeExecHandle):
    """Times out a read past the deadline though the exec ended within it, its output lost."""

    async def _stream(self) -> AsyncIterator[SandboxStreamChunk | SandboxStreamExit]:
        raise SandboxExecTimeoutError(timeout_seconds=1.0, stdout_len=len(self._buffer), stderr_len=0)
        yield SandboxStreamExit(exit_code=0)  # pragma: no cover - the raise above always ends the stream


class _LateReadTimesOutSession(FakeSandboxSession):
    handle_cls: ClassVar[type[FakeExecHandle]] = _LateReadTimesOutHandle


class _LateReadTimesOutSandbox(FakeSandbox):
    session_cls: ClassVar[type[FakeSandboxSession]] = _LateReadTimesOutSession


class _NoDeadlineHandle(FakeExecHandle):
    """Lets an interactive exec run on past its deadline: the deadline never kills it."""

    def _on_deadline(self) -> None:
        return None


class _NoDeadlineSession(FakeSandboxSession):
    handle_cls: ClassVar[type[FakeExecHandle]] = _NoDeadlineHandle


class _NoDeadlineSandbox(FakeSandbox):
    session_cls: ClassVar[type[FakeSandboxSession]] = _NoDeadlineSession


class _LateReadLosesOutputHandle(FakeExecHandle):
    """Ends an exec read past its deadline with its exit but without the output it produced."""

    async def _stream(self) -> AsyncIterator[SandboxStreamChunk | SandboxStreamExit]:
        await self._ended.wait()
        yield SandboxStreamExit(exit_code=0)


class _LateReadLosesOutputSession(FakeSandboxSession):
    handle_cls: ClassVar[type[FakeExecHandle]] = _LateReadLosesOutputHandle


class _LateReadLosesOutputSandbox(FakeSandbox):
    session_cls: ClassVar[type[FakeSandboxSession]] = _LateReadLosesOutputSession


class _LateReadLosesExitHandle(FakeExecHandle):
    """Ends an exec read past its deadline after its output, with no exit item."""

    async def _stream(self) -> AsyncIterator[SandboxStreamChunk | SandboxStreamExit]:
        await self._ended.wait()
        yield SandboxStreamChunk(stream="stdout", data=bytes(self._buffer))


class _LateReadLosesExitSession(FakeSandboxSession):
    handle_cls: ClassVar[type[FakeExecHandle]] = _LateReadLosesExitHandle


class _LateReadLosesExitSandbox(FakeSandbox):
    session_cls: ClassVar[type[FakeSandboxSession]] = _LateReadLosesExitSession


class _DeadlineEndsWithExitHandle(FakeExecHandle):
    """Ends an exec at its deadline as if it had exited: its output ends with an exit item, no timeout."""

    def _on_deadline(self) -> None:
        self._end()


class _DeadlineEndsWithExitSession(FakeSandboxSession):
    handle_cls: ClassVar[type[FakeExecHandle]] = _DeadlineEndsWithExitHandle


class _DeadlineEndsWithExitSandbox(FakeSandbox):
    session_cls: ClassVar[type[FakeSandboxSession]] = _DeadlineEndsWithExitSession


class _KillOnReadHandle(FakeExecHandle):
    """Kills an exec past its deadline only once its output is read: an exec nothing reads runs on."""

    _deadline_passed = False

    def _on_deadline(self) -> None:
        self._deadline_passed = True

    async def _stream(self) -> AsyncIterator[SandboxStreamChunk | SandboxStreamExit]:
        if self._deadline_passed and not self._ended.is_set():
            self._timed_out = True
            self._end()
        async for item in super()._stream():
            yield item


class _KillOnReadSession(FakeSandboxSession):
    handle_cls: ClassVar[type[FakeExecHandle]] = _KillOnReadHandle


class _KillOnReadSandbox(FakeSandbox):
    session_cls: ClassVar[type[FakeSandboxSession]] = _KillOnReadSession


class _CountsReadOutputHandle(FakeExecHandle):
    """Counts on its timeout only the output already handed to the caller: none, read past the deadline."""

    async def _stream(self) -> AsyncIterator[SandboxStreamChunk | SandboxStreamExit]:
        await self._ended.wait()
        if self._timed_out:
            raise SandboxExecTimeoutError(timeout_seconds=self._timeout_seconds, stdout_len=0, stderr_len=0)
        yield SandboxStreamExit(exit_code=0)


class _CountsReadOutputSession(FakeSandboxSession):
    handle_cls: ClassVar[type[FakeExecHandle]] = _CountsReadOutputHandle


class _CountsReadOutputSandbox(FakeSandbox):
    session_cls: ClassVar[type[FakeSandboxSession]] = _CountsReadOutputSession


# The output a transport-bound provider takes ahead of its reader, and all it ever gives.
_TRANSPORT_BUFFER = 128 * 1024


class _TransportBoundHandle(FakeExecHandle):
    """Gives and counts only the output its transport took ahead of its reader: the rest is lost."""

    async def _stream(self) -> AsyncIterator[SandboxStreamChunk | SandboxStreamExit]:
        del self._buffer[_TRANSPORT_BUFFER:]
        async for item in super()._stream():
            yield item


class _TransportBoundSession(FakeSandboxSession):
    handle_cls: ClassVar[type[FakeExecHandle]] = _TransportBoundHandle


class _TransportBoundSandbox(FakeSandbox):
    session_cls: ClassVar[type[FakeSandboxSession]] = _TransportBoundSession


# The grace a reader-bound provider gives an ended exec's stream, counted from its first read.
_READER_BOUND_GRACE = 0.1


class _ReaderBoundGraceHandle(FakeExecHandle):
    """Fails the output once its reader has taken longer than a grace since its first read: its own pauses count."""

    async def _stream(self) -> AsyncIterator[SandboxStreamChunk | SandboxStreamExit]:
        loop = asyncio.get_running_loop()
        grace_end = loop.time() + _READER_BOUND_GRACE
        async for item in super()._stream():
            if loop.time() > grace_end:
                raise SandboxError("sandbox exec stream did not end within the grace")
            yield item


class _ReaderBoundGraceSession(FakeSandboxSession):
    handle_cls: ClassVar[type[FakeExecHandle]] = _ReaderBoundGraceHandle


class _ReaderBoundGraceSandbox(FakeSandbox):
    session_cls: ClassVar[type[FakeSandboxSession]] = _ReaderBoundGraceSession


class _OutputEndIsExecEndSession(FakeSandboxSession):
    """Never kills an exec at its deadline once its output has closed, as though its command had ended."""

    async def exec_start(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, SecretStr] | None = None,
        timeout_seconds: float,
    ) -> SandboxExecHandle:
        handle = await super().exec_start(argv, cwd=cwd, env=env, timeout_seconds=timeout_seconds)
        if list(argv) == conformance_deadline._output_closing_exec_argv():
            # The exec's deadline timer is the first one it arms.
            assert isinstance(handle, FakeExecHandle)
            handle._timers[0].cancel()
        return handle


class _OutputEndIsExecEndSandbox(FakeSandbox):
    session_cls: ClassVar[type[FakeSandboxSession]] = _OutputEndIsExecEndSession


class _NeverStartsSession(FakeSandboxSession):
    """Starts the unread-exec case's exec without the start marker its command leaves."""

    async def exec_start(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, SecretStr] | None = None,
        timeout_seconds: float,
    ) -> SandboxExecHandle:
        return self.handle_cls(argv, timeout_seconds=timeout_seconds)


class _NeverStartsSandbox(FakeSandbox):
    session_cls: ClassVar[type[FakeSandboxSession]] = _NeverStartsSession


class _ProbeFailsSession(FakeSandboxSession):
    """Fails the unread-exec case's marker probe with an exit code of its own."""

    async def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, SecretStr] | None = None,
        stdin: bytes | None = None,
        timeout_seconds: float,
    ) -> ExecResult:
        if tuple(argv) == conformance_deadline._UNREAD_PROBE_ARGV:
            return ExecResult(exit_code=2, stdout="", stderr="sh: no shell here\n")
        return await super().exec(argv, cwd=cwd, env=env, stdin=stdin, timeout_seconds=timeout_seconds)


class _ProbeFailsSandbox(FakeSandbox):
    session_cls: ClassVar[type[FakeSandboxSession]] = _ProbeFailsSession


class _KilledHangsHandle(FakeExecHandle):
    """Keeps a killed exec's output open, as a provider waiting on a pipe a descendant holds."""

    async def _stream(self) -> AsyncIterator[SandboxStreamChunk | SandboxStreamExit]:
        yield SandboxStreamChunk(stream="stdout", data=b"conf-ready\n")
        await asyncio.Event().wait()
        yield SandboxStreamExit(exit_code=0)  # pragma: no cover - the wait above never ends


class _KilledHangsSession(FakeSandboxSession):
    handle_cls: ClassVar[type[FakeExecHandle]] = _KilledHangsHandle


class _KilledHangsSandbox(FakeSandbox):
    session_cls: ClassVar[type[FakeSandboxSession]] = _KilledHangsSession


class _NoTimeoutSession(FakeSandboxSession):
    async def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, SecretStr] | None = None,
        stdin: bytes | None = None,
        timeout_seconds: float,
    ) -> ExecResult:
        if argv[0] == "sleep":
            return ExecResult(exit_code=0, stdout="", stderr="")  # ignores the timeout
        return await super().exec(argv, cwd=cwd, env=env, stdin=stdin, timeout_seconds=timeout_seconds)


class _NoTimeoutSandbox(FakeSandbox):
    session_cls: ClassVar[type[FakeSandboxSession]] = _NoTimeoutSession


class _HeldOutputUnnotedHandle(FakeExecHandle):
    """Times out an exec whose output a process it started still holds without noting that process."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        late: tuple[float, Callable[[], None]] | None = None,
        output: bytes = b"",
        held: bool = False,
    ) -> None:
        super().__init__(argv, timeout_seconds=timeout_seconds, late=late, output=output)


class _HeldOutputUnnotedSession(FakeSandboxSession):
    handle_cls: ClassVar[type[FakeExecHandle]] = _HeldOutputUnnotedHandle


class _HeldOutputUnnotedSandbox(FakeSandbox):
    session_cls: ClassVar[type[FakeSandboxSession]] = _HeldOutputUnnotedSession


class _HeldOutputUndercountedHandle(FakeExecHandle):
    """Counts on the timeout of an exec whose output a process it started still holds none of its output."""

    async def _stream(self) -> AsyncIterator[SandboxStreamChunk | SandboxStreamExit]:
        try:
            async for item in super()._stream():
                yield item
        except SandboxExecTimeoutError as exc:
            undercounted = SandboxExecTimeoutError(timeout_seconds=exc.timeout_seconds, stdout_len=0, stderr_len=0)
            undercounted.add_note(OUTPUT_HELD_NOTE)
            raise undercounted from None


class _HeldOutputUndercountedSession(FakeSandboxSession):
    handle_cls: ClassVar[type[FakeExecHandle]] = _HeldOutputUndercountedHandle


class _HeldOutputUndercountedSandbox(FakeSandbox):
    session_cls: ClassVar[type[FakeSandboxSession]] = _HeldOutputUndercountedSession


class _KillAfterTimeoutHangsHandle(FakeExecHandle):
    """Never returns from ``kill()`` once the exec has timed out."""

    async def kill(self) -> None:
        if self._timed_out:
            await asyncio.Event().wait()
        await super().kill()


class _KillAfterTimeoutHangsSession(FakeSandboxSession):
    handle_cls: ClassVar[type[FakeExecHandle]] = _KillAfterTimeoutHangsHandle


class _KillAfterTimeoutHangsSandbox(FakeSandbox):
    session_cls: ClassVar[type[FakeSandboxSession]] = _KillAfterTimeoutHangsSession


class _StopsNoWriterSession(FakeSandboxSession):
    """Fails the one-shot exec that stops the output-holder cases' writer."""

    async def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, SecretStr] | None = None,
        stdin: bytes | None = None,
        timeout_seconds: float,
    ) -> ExecResult:
        if tuple(argv) == conformance_deadline._STOP_OUTPUT_WRITER_ARGV:
            return ExecResult(exit_code=1, stdout="", stderr="kill: no such process")
        return await super().exec(argv, cwd=cwd, env=env, stdin=stdin, timeout_seconds=timeout_seconds)


class _StopsNoWriterSandbox(FakeSandbox):
    session_cls: ClassVar[type[FakeSandboxSession]] = _StopsNoWriterSession


async def test_a_killed_exec_that_ends_in_its_deadline_timeout_is_caught() -> None:
    with pytest.raises(AssertionError, match="ended in a timeout"):
        await check_kill_after_deadline(with_permissive_policy(_KilledTimesOutSandbox()), FAKE_CONFORMANCE_CONFIG)


async def test_an_exec_ended_within_its_deadline_that_times_out_when_read_late_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(conformance_deadline, "_LATE_READ_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(AssertionError, match="timed out when read after it"):
        await check_late_read_after_exit(with_permissive_policy(_LateReadTimesOutSandbox()), FAKE_CONFORMANCE_CONFIG)


async def test_an_exec_ended_within_its_deadline_that_loses_its_output_when_read_late_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(conformance_deadline, "_LATE_READ_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(AssertionError, match="lost its output when read after it"):
        await check_late_read_after_exit(with_permissive_policy(_LateReadLosesOutputSandbox()), FAKE_CONFORMANCE_CONFIG)


async def test_an_exec_ended_within_its_deadline_that_loses_its_exit_when_read_late_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(conformance_deadline, "_LATE_READ_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(AssertionError, match="did not end with its exit when read after it"):
        await check_late_read_after_exit(with_permissive_policy(_LateReadLosesExitSandbox()), FAKE_CONFORMANCE_CONFIG)


async def test_a_large_output_exec_ended_within_its_deadline_that_times_out_when_read_late_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(conformance_deadline, "_LATE_READ_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(AssertionError, match="past its transport's buffer, timed out when read after it"):
        await check_late_read_of_large_output(
            with_permissive_policy(_LateReadTimesOutSandbox()), FAKE_CONFORMANCE_CONFIG
        )


async def test_a_large_output_exec_that_loses_the_output_its_transport_did_not_take_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(conformance_deadline, "_LATE_READ_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(AssertionError, match="gave 131072 of the 163840 stdout bytes it wrote when read after it"):
        await check_late_read_of_large_output(with_permissive_policy(_TransportBoundSandbox()), FAKE_CONFORMANCE_CONFIG)


async def test_a_large_output_exec_that_loses_its_exit_when_read_late_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(conformance_deadline, "_LATE_READ_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(AssertionError, match="past its transport's buffer, did not end with its exit"):
        await check_late_read_of_large_output(
            with_permissive_policy(_LateReadLosesExitSandbox()), FAKE_CONFORMANCE_CONFIG
        )


async def test_a_large_output_exec_whose_grace_counts_its_slow_reader_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(conformance_deadline, "_LATE_READ_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(conformance_deadline, "_SLOW_READ_PAUSE_SECONDS", _READER_BOUND_GRACE * 2)
    monkeypatch.setattr(conformance_deadline, "_SLOW_READ_SECONDS", _READER_BOUND_GRACE * 2)
    with pytest.raises(
        AssertionError,
        match=r"past its transport's buffer, raised SandboxError\('sandbox exec stream did not end within the grace'\)"
        r" when read slowly after it$",
    ):
        await check_slow_read_of_large_output(
            with_permissive_policy(_ReaderBoundGraceSandbox()), FAKE_CONFORMANCE_CONFIG
        )


async def test_the_fake_passes_the_large_output_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conformance_deadline, "_LATE_READ_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(conformance_deadline, "_TIMED_OUT_EXEC_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(conformance_deadline, "_SLOW_READ_PAUSE_SECONDS", _READER_BOUND_GRACE * 2)
    monkeypatch.setattr(conformance_deadline, "_SLOW_READ_SECONDS", _READER_BOUND_GRACE * 2)
    await check_late_read_of_large_output(with_permissive_policy(FakeSandbox()), FAKE_CONFORMANCE_CONFIG)
    await check_slow_read_of_large_output(with_permissive_policy(FakeSandbox()), FAKE_CONFORMANCE_CONFIG)
    await check_interactive_timeout_counts_large_output(with_permissive_policy(FakeSandbox()), FAKE_CONFORMANCE_CONFIG)


async def test_a_killed_exec_whose_output_does_not_end_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conformance_deadline, "_KILLED_PAST_DEADLINE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(conformance_deadline, "_KILLED_EXEC_END_SECONDS", 0.05)
    with pytest.raises(AssertionError, match="output did not end within"):
        await check_kill_after_deadline(with_permissive_policy(_KilledHangsSandbox()), FAKE_CONFORMANCE_CONFIG)


async def test_an_exec_that_ignores_its_timeout_is_caught() -> None:
    with pytest.raises(AssertionError, match="exec past its timeout"):
        await check_exec_timeout(with_permissive_policy(_NoTimeoutSandbox()), FAKE_CONFORMANCE_CONFIG)


async def test_an_interactive_exec_that_ignores_its_timeout_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conformance_deadline, "_TIMED_OUT_EXEC_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(conformance_deadline, "_TIMED_OUT_EXEC_END_SECONDS", 0.05)
    with pytest.raises(AssertionError, match="running past its deadline was not killed"):
        await check_interactive_exec_timeout(with_permissive_policy(_NoDeadlineSandbox()), FAKE_CONFORMANCE_CONFIG)


async def test_an_interactive_exec_whose_deadline_ends_it_with_an_exit_item_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(conformance_deadline, "_TIMED_OUT_EXEC_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(AssertionError, match=r"its output ended with \[SandboxStreamExit"):
        await check_interactive_exec_timeout(
            with_permissive_policy(_DeadlineEndsWithExitSandbox()), FAKE_CONFORMANCE_CONFIG
        )


def _fast_unread_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conformance_deadline, "_UNREAD_EXEC_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(conformance_deadline, "_UNREAD_EXEC_SLEEP_SECONDS", 0.05)
    monkeypatch.setattr(conformance_deadline, "_UNREAD_EXEC_MARGIN_SECONDS", 0.05)


async def test_the_fake_passes_the_unread_exec_check(monkeypatch: pytest.MonkeyPatch) -> None:
    _fast_unread_exec(monkeypatch)
    await check_unread_exec_killed_at_deadline(with_permissive_policy(FakeSandbox()), FAKE_CONFORMANCE_CONFIG)


async def test_an_exec_killed_past_its_deadline_only_when_read_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    _fast_unread_exec(monkeypatch)
    with pytest.raises(AssertionError, match="nothing read was not killed at its deadline"):
        await check_unread_exec_killed_at_deadline(
            with_permissive_policy(_KillOnReadSandbox()), FAKE_CONFORMANCE_CONFIG
        )


async def test_the_fake_passes_the_closed_output_exec_check(monkeypatch: pytest.MonkeyPatch) -> None:
    _fast_unread_exec(monkeypatch)
    await check_output_closed_exec_killed_at_deadline(with_permissive_policy(FakeSandbox()), FAKE_CONFORMANCE_CONFIG)


async def test_an_exec_whose_closed_output_spares_it_its_deadline_kill_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fast_unread_exec(monkeypatch)
    with pytest.raises(AssertionError, match="that closed its output was not killed at its deadline"):
        await check_output_closed_exec_killed_at_deadline(
            with_permissive_policy(_OutputEndIsExecEndSandbox()), FAKE_CONFORMANCE_CONFIG
        )


async def test_an_unread_exec_that_never_started_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    _fast_unread_exec(monkeypatch)
    with pytest.raises(AssertionError, match="never left its start marker"):
        await check_unread_exec_killed_at_deadline(
            with_permissive_policy(_NeverStartsSandbox()), FAKE_CONFORMANCE_CONFIG
        )


async def test_an_unread_exec_marker_probe_that_fails_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    _fast_unread_exec(monkeypatch)
    with pytest.raises(AssertionError, match="marker probe exited 2: sh: no shell here"):
        await check_unread_exec_killed_at_deadline(
            with_permissive_policy(_ProbeFailsSandbox()), FAKE_CONFORMANCE_CONFIG
        )


async def test_a_timeout_that_counts_only_the_output_read_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conformance_deadline, "_TIMED_OUT_EXEC_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(AssertionError, match="timeout counted stdout 0 and stderr 0 bytes"):
        await check_interactive_timeout_counts_output(
            with_permissive_policy(_CountsReadOutputSandbox()), FAKE_CONFORMANCE_CONFIG
        )


async def test_a_timeout_that_misses_the_output_its_transport_did_not_take_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(conformance_deadline, "_TIMED_OUT_EXEC_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(AssertionError, match="timeout counted stdout 131072 and stderr 0 bytes of the 163840"):
        await check_interactive_timeout_counts_large_output(
            with_permissive_policy(_TransportBoundSandbox()), FAKE_CONFORMANCE_CONFIG
        )


async def test_a_counted_exec_that_ignores_its_timeout_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conformance_deadline, "_TIMED_OUT_EXEC_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(conformance_deadline, "_TIMED_OUT_EXEC_END_SECONDS", 0.05)
    with pytest.raises(AssertionError, match=r"did not raise its timeout within 0\.05s"):
        await check_interactive_timeout_counts_output(
            with_permissive_policy(_NoDeadlineSandbox()), FAKE_CONFORMANCE_CONFIG
        )


async def test_a_counted_exec_whose_deadline_ends_it_with_an_exit_item_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(conformance_deadline, "_TIMED_OUT_EXEC_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(AssertionError, match=r"ended with \[SandboxStreamExit.*instead of raising its timeout"):
        await check_interactive_timeout_counts_output(
            with_permissive_policy(_DeadlineEndsWithExitSandbox()), FAKE_CONFORMANCE_CONFIG
        )


# An output-holder conformance check.
_Check = Callable[[ManagedSandbox, SandboxConformanceConfig], Awaitable[None]]


_OUTPUT_HOLDER_CHECKS = pytest.mark.parametrize(
    "check", [check_output_holder_at_deadline_times_out, check_signalled_exec_with_output_holder_times_out]
)


def _fast_output_holder_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conformance_deadline, "_TIMED_OUT_EXEC_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(conformance_deadline, "_TIMED_OUT_EXEC_END_SECONDS", 0.05)
    monkeypatch.setattr(conformance_deadline, "_KILLED_EXEC_END_SECONDS", 0.05)
    monkeypatch.setattr(
        conformance_deadline,
        "_OUTPUT_HOLDER_READS",
        (
            (0.0, False, "read as it came"),
            (0.001, False, "read with pauses"),
            (0.0, True, "first read past its deadline"),
        ),
    )


@_OUTPUT_HOLDER_CHECKS
async def test_the_fake_passes_the_output_holder_checks(monkeypatch: pytest.MonkeyPatch, check: _Check) -> None:
    _fast_output_holder_exec(monkeypatch)
    await check(with_permissive_policy(FakeSandbox()), FAKE_CONFORMANCE_CONFIG)


@_OUTPUT_HOLDER_CHECKS
async def test_an_exec_read_on_while_a_process_holds_its_output_is_caught(
    monkeypatch: pytest.MonkeyPatch, check: _Check
) -> None:
    _fast_output_holder_exec(monkeypatch)
    with pytest.raises(AssertionError, match=r"read as it came, did not raise its timeout within 0\.05s"):
        await check(with_permissive_policy(_NoDeadlineSandbox()), FAKE_CONFORMANCE_CONFIG)


@_OUTPUT_HOLDER_CHECKS
async def test_an_exec_ended_while_a_process_holds_its_output_is_caught(
    monkeypatch: pytest.MonkeyPatch, check: _Check
) -> None:
    _fast_output_holder_exec(monkeypatch)
    with pytest.raises(AssertionError, match=r"ended with SandboxStreamExit\(.*\) instead of raising its timeout"):
        await check(with_permissive_policy(_DeadlineEndsWithExitSandbox()), FAKE_CONFORMANCE_CONFIG)


@_OUTPUT_HOLDER_CHECKS
async def test_a_held_output_timeout_that_does_not_note_the_holder_is_caught(
    monkeypatch: pytest.MonkeyPatch, check: _Check
) -> None:
    _fast_output_holder_exec(monkeypatch)
    with pytest.raises(AssertionError, match="timed out without noting that a process still held its output"):
        await check(with_permissive_policy(_HeldOutputUnnotedSandbox()), FAKE_CONFORMANCE_CONFIG)


@_OUTPUT_HOLDER_CHECKS
async def test_a_held_output_timeout_that_counts_less_than_was_received_is_caught(
    monkeypatch: pytest.MonkeyPatch, check: _Check
) -> None:
    _fast_output_holder_exec(monkeypatch)
    with pytest.raises(AssertionError, match="timed out counting 0 stdout bytes of the 4096 it gave"):
        await check(with_permissive_policy(_HeldOutputUndercountedSandbox()), FAKE_CONFORMANCE_CONFIG)


@_OUTPUT_HOLDER_CHECKS
async def test_a_kill_that_never_returns_after_a_held_output_timeout_is_caught(
    monkeypatch: pytest.MonkeyPatch, check: _Check
) -> None:
    _fast_output_holder_exec(monkeypatch)
    with pytest.raises(AssertionError, match=r"did not return from kill\(\) within 0\.05s of its timeout"):
        await check(with_permissive_policy(_KillAfterTimeoutHangsSandbox()), FAKE_CONFORMANCE_CONFIG)


@_OUTPUT_HOLDER_CHECKS
async def test_an_output_holder_case_that_cannot_stop_its_writer_is_caught(
    monkeypatch: pytest.MonkeyPatch, check: _Check
) -> None:
    _fast_output_holder_exec(monkeypatch)
    with pytest.raises(AssertionError, match=r"could not stop the descendant it started \(exit 1\): kill: no such"):
        await check(with_permissive_policy(_StopsNoWriterSandbox()), FAKE_CONFORMANCE_CONFIG)
