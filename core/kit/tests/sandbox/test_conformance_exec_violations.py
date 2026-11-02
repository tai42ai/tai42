"""The exec checks have teeth: each FAILS loudly against a provider that breaks exactly the
exec property it certifies.

These drive the checks of :mod:`tai42_kit.sandbox.conformance_exec` against deliberately
non-conformant fakes: one that swallows a write after exit, one whose kill after exit is not
idempotent, one that cuts a one-shot exec's stdin short or raises for its unread stdin, one whose
kill drops the output it received before the kill. A held exec that fails to start is reported as
itself, never as the stop of the descendant it never started.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

import pytest
from pydantic import SecretStr
from tai42_contract.sandbox import (
    ExecResult,
    SandboxError,
    SandboxExecHandle,
)

from tai42_kit.sandbox import conformance_exec
from tai42_kit.sandbox.conformance_exec import (
    check_interactive_exec,
    check_kill_keeps_received_output,
    check_one_shot_large_stdin,
    check_one_shot_unread_stdin,
)

from .fakes import FAKE_CONFORMANCE_CONFIG, FakeExecHandle, FakeSandbox, FakeSandboxSession, with_permissive_policy


class _LenientHandle(FakeExecHandle):
    async def write_stdin(self, data: bytes) -> None:
        self._buffer.extend(data)  # never raises, even after exit


class _LenientSession(FakeSandboxSession):
    handle_cls: ClassVar[type[FakeExecHandle]] = _LenientHandle


class _LenientSandbox(FakeSandbox):
    session_cls: ClassVar[type[FakeSandboxSession]] = _LenientSession


class _KillOnceHandle(FakeExecHandle):
    """Raises on a kill after the exec has exited and was killed once: a kill that is not idempotent."""

    _killed = False

    async def kill(self) -> None:
        if self._killed:
            raise SandboxError("the exec was already killed")
        self._killed = True
        await super().kill()


class _KillOnceSession(FakeSandboxSession):
    handle_cls: ClassVar[type[FakeExecHandle]] = _KillOnceHandle


class _KillOnceSandbox(FakeSandbox):
    session_cls: ClassVar[type[FakeSandboxSession]] = _KillOnceSession


class _KillDropsOutputHandle(FakeExecHandle):
    """Drops the output it received and nothing read yet at a kill: the read after the kill misses it."""

    async def kill(self) -> None:
        self._buffer.clear()
        await super().kill()


class _KillDropsOutputSession(FakeSandboxSession):
    handle_cls: ClassVar[type[FakeExecHandle]] = _KillDropsOutputHandle


class _KillDropsOutputSandbox(FakeSandbox):
    session_cls: ClassVar[type[FakeSandboxSession]] = _KillDropsOutputSession


class _ShortStdinSession(FakeSandboxSession):
    async def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, SecretStr] | None = None,
        stdin: bytes | None = None,
        timeout_seconds: float,
    ) -> ExecResult:
        # Only the part of the stdin a pipe holds reaches the command.
        short = stdin[:65536] if stdin is not None else None
        return await super().exec(argv, cwd=cwd, env=env, stdin=short, timeout_seconds=timeout_seconds)


class _ShortStdinSandbox(FakeSandbox):
    session_cls: ClassVar[type[FakeSandboxSession]] = _ShortStdinSession


class _UnreadStdinRaisesSession(FakeSandboxSession):
    async def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, SecretStr] | None = None,
        stdin: bytes | None = None,
        timeout_seconds: float,
    ) -> ExecResult:
        # A provider that takes the command's unread stdin as a failed write.
        if stdin and argv[0] == "head":
            raise SandboxError("sandbox exec stdin write failed: the exec's stdin pipe is closed")
        return await super().exec(argv, cwd=cwd, env=env, stdin=stdin, timeout_seconds=timeout_seconds)


class _UnreadStdinRaisesSandbox(FakeSandbox):
    session_cls: ClassVar[type[FakeSandboxSession]] = _UnreadStdinRaisesSession


async def test_a_swallowed_write_after_exit_is_caught() -> None:
    with pytest.raises(AssertionError, match="write_stdin after exit"):
        await check_interactive_exec(with_permissive_policy(_LenientSandbox()), FAKE_CONFORMANCE_CONFIG)


async def test_a_kill_after_exit_that_is_not_idempotent_is_caught() -> None:
    with pytest.raises(AssertionError, match="kill after exit raised"):
        await check_interactive_exec(with_permissive_policy(_KillOnceSandbox()), FAKE_CONFORMANCE_CONFIG)


async def test_a_one_shot_stdin_that_does_not_come_back_whole_is_caught() -> None:
    with pytest.raises(AssertionError, match="did not come back whole"):
        await check_one_shot_large_stdin(with_permissive_policy(_ShortStdinSandbox()), FAKE_CONFORMANCE_CONFIG)


async def test_a_one_shot_exec_that_raises_for_its_command_s_unread_stdin_is_caught() -> None:
    with pytest.raises(SandboxError, match="stdin pipe is closed"):
        await check_one_shot_unread_stdin(with_permissive_policy(_UnreadStdinRaisesSandbox()), FAKE_CONFORMANCE_CONFIG)


async def test_a_kill_that_drops_the_output_received_before_it_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conformance_exec, "_BEFORE_KILL_SETTLE_SECONDS", 0.01)
    with pytest.raises(AssertionError, match=r"read after its kill gave stdout b'', not the b'conf-before-kill'"):
        await check_kill_keeps_received_output(
            with_permissive_policy(_KillDropsOutputSandbox()), FAKE_CONFORMANCE_CONFIG
        )


async def test_a_kill_that_keeps_the_output_received_before_it_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conformance_exec, "_BEFORE_KILL_SETTLE_SECONDS", 0.01)
    await check_kill_keeps_received_output(with_permissive_policy(FakeSandbox()), FAKE_CONFORMANCE_CONFIG)


class _HeldStartFailsSession(FakeSandboxSession):
    async def exec_start(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, SecretStr] | None = None,
        timeout_seconds: float,
    ) -> SandboxExecHandle:
        # The held exec never starts, so it writes no pidfile for the stop to read.
        if tuple(argv) == conformance_exec._BEFORE_KILL_HELD_ARGV:
            raise SandboxError("sandbox exec start failed: the runtime refused the exec")
        return await super().exec_start(argv, cwd=cwd, env=env, timeout_seconds=timeout_seconds)

    async def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, SecretStr] | None = None,
        stdin: bytes | None = None,
        timeout_seconds: float,
    ) -> ExecResult:
        if tuple(argv) == conformance_exec._STOP_KILL_HOLDER_ARGV:
            return ExecResult(exit_code=1, stdout="", stderr="cat: no such file")
        return await super().exec(argv, cwd=cwd, env=env, stdin=stdin, timeout_seconds=timeout_seconds)


class _HeldStartFailsSandbox(FakeSandbox):
    session_cls: ClassVar[type[FakeSandboxSession]] = _HeldStartFailsSession


async def test_a_held_exec_that_fails_to_start_is_reported_as_its_own_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conformance_exec, "_BEFORE_KILL_SETTLE_SECONDS", 0.01)
    with pytest.raises(SandboxError, match="the runtime refused the exec"):
        await check_kill_keeps_received_output(
            with_permissive_policy(_HeldStartFailsSandbox()), FAKE_CONFORMANCE_CONFIG
        )
