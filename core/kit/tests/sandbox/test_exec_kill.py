"""The sibling kill's result, its bound, and the cleanup kill of a call that fails."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import anyio
import pytest
from tai42_contract.sandbox import SandboxError

from tai42_kit.sandbox import exec_kill
from tai42_kit.sandbox.exec_kill import (
    OUTPUT_HELD_NOTE,
    KillOutcome,
    KillProgress,
    KillResult,
    kill_failed,
    kill_for_failure,
    kill_on_failure,
    within_kill_timeout,
)


def test_a_failed_kill_carries_its_own_output() -> None:
    error = kill_failed(b"  kill: Operation not permitted\n")
    assert str(error) == "sandbox exec kill failed: kill: Operation not permitted"


async def test_a_kill_within_the_bound_returns_what_it_found() -> None:
    async def kill(progress: KillProgress) -> KillResult:
        return KillResult(KillOutcome.SIGNALLED, exit_code=137)

    assert await within_kill_timeout(kill) == KillResult(KillOutcome.SIGNALLED, exit_code=137)


async def test_a_kill_past_the_bound_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(exec_kill, "KILL_TIMEOUT_SECONDS", 0.01)

    async def kill(progress: KillProgress) -> KillResult:
        await asyncio.sleep(10)
        return KillResult(KillOutcome.SIGNALLED)

    with pytest.raises(SandboxError, match=r"^sandbox exec kill did not finish within 0\.01s$") as raised:
        await within_kill_timeout(kill)
    assert not hasattr(raised.value, "__notes__")


async def test_a_kill_past_the_bound_keeps_what_it_wrote_on_its_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    # The kill wrote part of its failure line, then its runtime stopped answering.
    monkeypatch.setattr(exec_kill, "KILL_TIMEOUT_SECONDS", 0.01)

    async def kill(progress: KillProgress) -> KillResult:
        progress.stderr.extend(b"cannot read the open files of process 19:\nls: /proc/19/fd: Permi")
        await asyncio.sleep(10)
        return KillResult(KillOutcome.SIGNALLED)

    with pytest.raises(SandboxError, match=r"^sandbox exec kill did not finish within 0\.01s") as raised:
        await within_kill_timeout(kill)
    assert raised.value.__notes__ == [
        "the sandbox exec kill wrote on its stderr before its time limit passed: "
        "cannot read the open files of process 19:; ls: /proc/19/fd: Permi"
    ]


async def test_a_kill_past_the_bound_keeps_the_failure_it_met_before(monkeypatch: pytest.MonkeyPatch) -> None:
    # The kill failed, and the follow-up read of the exec's state then hung.
    monkeypatch.setattr(exec_kill, "KILL_TIMEOUT_SECONDS", 0.01)

    async def kill(progress: KillProgress) -> KillResult:
        progress.stderr.extend(b"kill: Operation not permitted\n")
        progress.failed(kill_failed(bytes(progress.stderr)))
        await asyncio.sleep(10)
        return KillResult(KillOutcome.EXEC_ENDED)

    with pytest.raises(SandboxError, match=r"^sandbox exec kill did not finish within 0\.01s") as raised:
        await within_kill_timeout(kill)
    assert raised.value.__notes__ == [
        "the sandbox exec kill failed before its time limit passed: "
        "sandbox exec kill failed: kill: Operation not permitted"
    ]


async def test_a_cleanup_kill_that_fails_keeps_the_notes_of_its_failure_on_the_failure() -> None:
    failure = RuntimeError("the call failed")

    async def kill() -> KillResult:
        error = SandboxError("sandbox exec kill did not finish within 5.0s")
        error.add_note("the sandbox exec kill failed before its time limit passed: no route to the runtime")
        raise error

    await kill_for_failure(kill(), failure, "e1")
    assert failure.__notes__ == [
        "the kill of the sandbox exec also failed: sandbox exec kill did not finish within 5.0s",
        "the sandbox exec kill failed before its time limit passed: no route to the runtime",
    ]


async def test_a_cleanup_kill_that_fails_is_logged_and_noted_on_the_failure(caplog: pytest.LogCaptureFixture) -> None:
    failure = RuntimeError("the call failed")

    async def kill() -> KillResult:
        raise SandboxError("no route to the runtime")

    with caplog.at_level(logging.ERROR, logger=exec_kill.__name__):
        await kill_for_failure(kill(), failure, "e1")
    assert failure.__notes__ == ["the kill of the sandbox exec also failed: no route to the runtime"]
    assert [record.getMessage() for record in caplog.records] == ["the kill of sandbox exec e1 failed"]
    assert caplog.records[0].exc_info is not None


@pytest.mark.parametrize("outcome", [KillOutcome.SIGNALLED, KillOutcome.NOTHING_RUNNING])
async def test_a_cleanup_kill_after_which_a_process_holds_the_output_is_logged_and_noted(
    outcome: KillOutcome, caplog: pytest.LogCaptureFixture
) -> None:
    failure = RuntimeError("the call failed")

    async def kill() -> KillResult:
        return KillResult(outcome, output_held=True)

    with caplog.at_level(logging.WARNING, logger=exec_kill.__name__):
        await kill_for_failure(kill(), failure, "e1")
    assert failure.__notes__ == [OUTPUT_HELD_NOTE]
    assert [(record.levelno, record.getMessage()) for record in caplog.records] == [
        (logging.WARNING, f"after the kill of sandbox exec e1, {OUTPUT_HELD_NOTE}")
    ]


@pytest.mark.parametrize(
    "outcome",
    [
        KillOutcome.SIGNALLED,
        KillOutcome.NOTHING_RUNNING,
        KillOutcome.EXEC_ENDED,
        KillOutcome.NOT_STARTED,
        KillOutcome.SESSION_GONE,
    ],
)
async def test_a_cleanup_kill_that_stopped_or_found_the_exec_over_reports_nothing(
    outcome: KillOutcome, caplog: pytest.LogCaptureFixture
) -> None:
    failure = RuntimeError("the call failed")

    async def kill() -> KillResult:
        return KillResult(outcome)

    with caplog.at_level(logging.DEBUG, logger=exec_kill.__name__):
        await kill_for_failure(kill(), failure, "e1")
    assert not hasattr(failure, "__notes__")
    assert caplog.records == []


async def test_a_cancelled_caller_waits_for_the_cleanup_kill_then_raises_its_cancellation() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    finished: list[bool] = []
    failure = RuntimeError("the call failed")

    async def kill() -> KillResult:
        started.set()
        await release.wait()
        finished.append(True)
        return KillResult(KillOutcome.SIGNALLED)

    caller = asyncio.ensure_future(kill_for_failure(kill(), failure, "e1"))
    await started.wait()
    caller.cancel()
    await asyncio.sleep(0)
    caller.cancel()
    await asyncio.sleep(0)
    assert not caller.done()
    release.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await caller
    assert finished == [True]
    assert raised.value.__cause__ is failure


async def test_a_cleanup_kill_for_a_cancellation_returns_so_the_caller_re_raises_its_own() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    failure = asyncio.CancelledError()

    async def kill() -> KillResult:
        started.set()
        await release.wait()
        return KillResult(KillOutcome.SIGNALLED)

    caller = asyncio.ensure_future(kill_for_failure(kill(), failure, "e1"))
    await started.wait()
    caller.cancel()
    await asyncio.sleep(0)
    release.set()
    assert await caller is None


async def test_a_cleanup_kill_under_an_anyio_scope_waits_without_spinning(monkeypatch: pytest.MonkeyPatch) -> None:
    # The scope re-delivers its cancellation at every wake-up: unshielded, the wait on the kill
    # would wake and loop again at once, thousands of times over one slow kill.
    waits = 0
    wait = asyncio.wait

    async def _counted_wait(*args: Any, **kwargs: Any) -> Any:
        nonlocal waits
        waits += 1
        return await wait(*args, **kwargs)

    async def _slow_kill() -> KillResult:
        await asyncio.sleep(0.2)
        return KillResult(KillOutcome.SIGNALLED)

    monkeypatch.setattr(asyncio, "wait", _counted_wait)
    with anyio.move_on_after(0.01) as scope:
        await kill_for_failure(_slow_kill(), RuntimeError("the call failed"), "x1")

    assert scope.cancelled_caught
    # One wait the scope cancelled, then the shielded wait the kill ends.
    assert waits == 2


class _Kills:
    """A cleanup kill that counts its runs and reports a signalled exec."""

    def __init__(self) -> None:
        self.runs = 0

    async def kill(self) -> KillResult:
        self.runs += 1
        return KillResult(KillOutcome.SIGNALLED)


async def test_a_call_that_ends_normally_is_never_killed() -> None:
    kills = _Kills()
    async with kill_on_failure(kills.kill, "e1"):
        pass
    assert kills.runs == 0


async def test_a_failed_call_is_killed_and_its_error_raised_as_it_came() -> None:
    kills = _Kills()
    error = RuntimeError("the stdin write failed")
    with pytest.raises(RuntimeError) as raised:
        async with kill_on_failure(kills.kill, "e1"):
            raise error
    assert raised.value is error
    assert kills.runs == 1


async def test_a_failed_call_is_killed_and_raises_its_mapped_error_caused_by_the_original() -> None:
    kills = _Kills()
    timeout = TimeoutError()

    def failure_of(exc: BaseException) -> BaseException:
        return SandboxError("the exec timed out") if isinstance(exc, TimeoutError) else exc

    with pytest.raises(SandboxError, match=r"^the exec timed out$") as raised:
        async with kill_on_failure(kills.kill, "e1", failure_of=failure_of):
            raise timeout
    assert raised.value.__cause__ is timeout
    assert kills.runs == 1


async def test_a_call_that_failed_before_its_exec_started_raises_with_no_kill() -> None:
    kills = _Kills()
    with pytest.raises(SandboxError, match=r"^refused$"):
        async with kill_on_failure(
            kills.kill,
            "e1",
            failure_of=lambda exc: SandboxError("refused"),
            exec_started=lambda exc: not isinstance(exc, ConnectionRefusedError),
        ):
            raise ConnectionRefusedError
    assert kills.runs == 0


async def test_a_cancelled_call_is_killed_and_its_cancellation_raised() -> None:
    kills = _Kills()
    started = asyncio.Event()

    async def call() -> None:
        async with kill_on_failure(kills.kill, "e1"):
            started.set()
            await asyncio.sleep(10)

    task = asyncio.ensure_future(call())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert kills.runs == 1


async def test_a_kill_that_fails_is_noted_on_the_error_the_failed_call_raises() -> None:
    async def kill() -> KillResult:
        raise SandboxError("no route to the runtime")

    with pytest.raises(RuntimeError) as raised:
        async with kill_on_failure(kill, "e1"):
            raise RuntimeError("the call failed")
    assert raised.value.__notes__ == ["the kill of the sandbox exec also failed: no route to the runtime"]
