"""The group-stop checks have teeth: each FAILS loudly against a provider whose stop spares what a command left.

These drive the checks of :mod:`tai42_kit.sandbox.conformance_group_stop` against deliberately
non-conformant fakes: an exec ended at its command's exit while a process it left in its group
holds its output, a stop that notes that process as a holder of the output, a stop that leaves
it running, and a ``kill()`` whose output never ends or ends without its exit item.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence

import pytest
from pydantic import SecretStr
from tai42_contract.sandbox import ExecResult, SandboxExecTimeoutError, SandboxStreamChunk, SandboxStreamExit

from tai42_kit.sandbox import ManagedSandbox, conformance_group_stop
from tai42_kit.sandbox.conformance_group_stop import (
    check_interactive_deadline_kills_what_the_command_left_in_its_group,
    check_kill_kills_what_the_command_left_in_its_group,
    check_one_shot_deadline_kills_what_the_command_left_in_its_group,
)
from tai42_kit.sandbox.exec_kill import OUTPUT_HELD_NOTE

from .fakes import FAKE_CONFORMANCE_CONFIG, FakeExecHandle, FakeSandbox, FakeSandboxSession, with_permissive_policy


@pytest.fixture(autouse=True)
def _short_left_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """The process each case leaves in its group sleeps a fraction of a second, so the checks run fast."""
    monkeypatch.setattr(conformance_group_stop, "_LEFT_IN_GROUP_SLEEP_SECONDS", 0.3)
    monkeypatch.setattr(conformance_group_stop, "_LEFT_IN_GROUP_MARGIN_SECONDS", 0.1)
    monkeypatch.setattr(conformance_group_stop, "_LEFT_IN_GROUP_EXITED_SECONDS", 0.0)
    monkeypatch.setattr(conformance_group_stop, "_LEFT_IN_GROUP_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(conformance_group_stop, "_LEFT_IN_GROUP_END_SECONDS", 0.2)


def _late_marker(session: FakeSandboxSession, case: str) -> str:
    return session._resolve(conformance_group_stop._markers(case)[1])


class _OneShotSession(FakeSandboxSession):
    """Runs the one-shot group-stop case's exec as :meth:`left_in_group` says, every other exec as the fake does."""

    async def left_in_group(self, timeout_seconds: float) -> ExecResult:
        raise NotImplementedError

    async def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, SecretStr] | None = None,
        stdin: bytes | None = None,
        timeout_seconds: float,
    ) -> ExecResult:
        if list(argv) == conformance_group_stop._left_in_group_argv("one-shot"):
            self._files[self._resolve(conformance_group_stop._markers("one-shot")[0])] = b""
            return await self.left_in_group(timeout_seconds)
        return await super().exec(argv, cwd=cwd, env=env, stdin=stdin, timeout_seconds=timeout_seconds)


class _EndsAtCommandExitSession(_OneShotSession):
    async def left_in_group(self, timeout_seconds: float) -> ExecResult:
        return ExecResult(exit_code=0, stdout="conf-left-ready\n", stderr="")


class _NotesHolderSession(_OneShotSession):
    async def left_in_group(self, timeout_seconds: float) -> ExecResult:
        timeout = SandboxExecTimeoutError(timeout_seconds=timeout_seconds, stdout_len=0, stderr_len=0)
        timeout.add_note(OUTPUT_HELD_NOTE)
        raise timeout


class _SparesLeftProcessSession(_OneShotSession):
    async def left_in_group(self, timeout_seconds: float) -> ExecResult:
        # The deadline's stop kills the command alone: the process it left runs on to its late marker.
        self._files[_late_marker(self, "one-shot")] = b""
        raise SandboxExecTimeoutError(timeout_seconds=timeout_seconds, stdout_len=0, stderr_len=0)


class _NeverEndsOneShotSession(_OneShotSession):
    async def left_in_group(self, timeout_seconds: float) -> ExecResult:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover - the wait above never returns


class _SparesLeftProcessHandle(FakeExecHandle):
    """A stop that ends the exec but spares the process its command left: its late effect still runs."""

    def _end(self) -> None:
        # The deadline timer alone is the exec's; every other timer is the left process's own.
        self._timers = self._timers[:1]
        super()._end()


class _NotesHolderHandle(FakeExecHandle):
    """Notes a holder of the output on every timeout."""

    async def _stream(self) -> AsyncIterator[SandboxStreamChunk | SandboxStreamExit]:
        yield SandboxStreamChunk(stream="stdout", data=bytes(self._buffer))
        await self._ended.wait()
        timeout = SandboxExecTimeoutError(
            timeout_seconds=self._timeout_seconds, stdout_len=len(self._buffer), stderr_len=0
        )
        timeout.add_note(OUTPUT_HELD_NOTE)
        raise timeout


class _EndsAtCommandExitHandle(FakeExecHandle):
    """Ends the output with its exit item once the command's line is out, as at the command's exit."""

    async def _stream(self) -> AsyncIterator[SandboxStreamChunk | SandboxStreamExit]:
        yield SandboxStreamChunk(stream="stdout", data=bytes(self._buffer))
        yield SandboxStreamExit(exit_code=0)


class _KillNeverEndsHandle(FakeExecHandle):
    """A ``kill()`` that leaves the output open, as while the process the command left still holds it."""

    async def kill(self) -> None:
        return None

    async def _stream(self) -> AsyncIterator[SandboxStreamChunk | SandboxStreamExit]:
        yield SandboxStreamChunk(stream="stdout", data=bytes(self._buffer))
        await asyncio.Event().wait()


class _KillEndsWithoutExitHandle(FakeExecHandle):
    """Ends a killed exec's output with no exit item."""

    async def _stream(self) -> AsyncIterator[SandboxStreamChunk | SandboxStreamExit]:
        yield SandboxStreamChunk(stream="stdout", data=bytes(self._buffer))
        await self._ended.wait()


def _sandbox(
    *, session_cls: type[FakeSandboxSession] = FakeSandboxSession, handle_cls: type[FakeExecHandle] | None = None
) -> ManagedSandbox:
    """A fake provider whose sessions are ``session_cls``, running ``handle_cls`` execs, its permissive policy bound."""
    if handle_cls is not None:
        session_cls = type("_GroupStopSession", (session_cls,), {"handle_cls": handle_cls})
    sandbox_cls: type[FakeSandbox] = type("_GroupStopSandbox", (FakeSandbox,), {"session_cls": session_cls})
    return with_permissive_policy(sandbox_cls())


async def test_the_fake_passes_the_group_stop_checks() -> None:
    for check in (
        check_one_shot_deadline_kills_what_the_command_left_in_its_group,
        check_interactive_deadline_kills_what_the_command_left_in_its_group,
        check_kill_kills_what_the_command_left_in_its_group,
    ):
        await check(_sandbox(), FAKE_CONFORMANCE_CONFIG)


@pytest.mark.parametrize(
    ("session_cls", "match"),
    [
        (_EndsAtCommandExitSession, "holding its output returned exit 0 instead of raising its timeout"),
        (_NotesHolderSession, "noting that a process still held its output"),
        (_SparesLeftProcessSession, "was not killed by a one-shot exec's deadline"),
        (_NeverEndsOneShotSession, "did not raise its timeout within"),
    ],
    ids=["ends-at-command-exit", "notes-holder", "spares-left-process", "never-ends"],
)
async def test_a_one_shot_deadline_that_spares_what_the_command_left_is_caught(
    session_cls: type[FakeSandboxSession], match: str
) -> None:
    with pytest.raises(AssertionError, match=match):
        await check_one_shot_deadline_kills_what_the_command_left_in_its_group(
            _sandbox(session_cls=session_cls), FAKE_CONFORMANCE_CONFIG
        )


@pytest.mark.parametrize(
    ("handle_cls", "match"),
    [
        (_EndsAtCommandExitHandle, "holding its output ended with"),
        (_NotesHolderHandle, "noting that a process still held its output"),
        (_SparesLeftProcessHandle, "was not killed by an interactive exec's deadline"),
        (_KillNeverEndsHandle, "did not raise its timeout within"),
    ],
    ids=["ends-at-command-exit", "notes-holder", "spares-left-process", "never-ends"],
)
async def test_an_interactive_deadline_that_spares_what_the_command_left_is_caught(
    handle_cls: type[FakeExecHandle], match: str
) -> None:
    with pytest.raises(AssertionError, match=match):
        await check_interactive_deadline_kills_what_the_command_left_in_its_group(
            _sandbox(handle_cls=handle_cls), FAKE_CONFORMANCE_CONFIG
        )


@pytest.mark.parametrize(
    ("handle_cls", "match"),
    [
        (_KillNeverEndsHandle, "did not end its output within"),
        (_KillEndsWithoutExitHandle, "with one exit item"),
        (_SparesLeftProcessHandle, r"was not killed by kill\(\)"),
    ],
    ids=["never-ends", "no-exit-item", "spares-left-process"],
)
async def test_a_kill_that_spares_what_the_command_left_is_caught(handle_cls: type[FakeExecHandle], match: str) -> None:
    with pytest.raises(AssertionError, match=match):
        await check_kill_kills_what_the_command_left_in_its_group(
            _sandbox(handle_cls=handle_cls), FAKE_CONFORMANCE_CONFIG
        )
