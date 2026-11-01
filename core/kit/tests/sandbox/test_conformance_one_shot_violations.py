"""The one-shot exec checks of what an exec's end and its timeout give have teeth.

These drive :func:`check_exec_timeout_counts_output` and
:func:`check_output_held_after_exit_read_to_end` against deliberately non-conformant fakes: a
timeout that counts less than the exec produced, or an exec ended at its command's exit while a
process it started still holds its output.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from pydantic import SecretStr
from tai42_contract.sandbox import ExecResult, SandboxExecTimeoutError

from tai42_kit.sandbox import conformance_deadline
from tai42_kit.sandbox.conformance_deadline import (
    check_exec_timeout_counts_output,
    check_output_held_after_exit_read_to_end,
)

from .fakes import FAKE_CONFORMANCE_CONFIG, FakeSandbox, FakeSandboxSession, with_permissive_policy

_WRITTEN = 2 * conformance_deadline._WRITTEN_CHUNK_LEN


class _OneShotSession(FakeSandboxSession):
    """Runs the one-shot cases' execs as :meth:`one_shot` says, every other exec as the fake does."""

    async def one_shot(self, argv: tuple[str, ...], timeout_seconds: float) -> ExecResult | None:
        return None

    async def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, SecretStr] | None = None,
        stdin: bytes | None = None,
        timeout_seconds: float,
    ) -> ExecResult:
        result = await self.one_shot(tuple(argv), timeout_seconds)
        if result is not None:
            return result
        return await super().exec(argv, cwd=cwd, env=env, stdin=stdin, timeout_seconds=timeout_seconds)

    def record_written(self) -> None:
        self._files[self._resolve(conformance_deadline._WRITTEN_RECORD)] = str(_WRITTEN).encode()


class _CountFrozenAtTimeoutSession(_OneShotSession):
    async def one_shot(self, argv: tuple[str, ...], timeout_seconds: float) -> ExecResult | None:
        if argv == conformance_deadline._RECORDED_WRITER_ARGV:
            self.record_written()
            # Counts only the first chunk: what had reached it by the timeout.
            raise SandboxExecTimeoutError(
                timeout_seconds=timeout_seconds, stdout_len=conformance_deadline._WRITTEN_CHUNK_LEN, stderr_len=0
            )
        return None


class _CountsStderrSession(_OneShotSession):
    async def one_shot(self, argv: tuple[str, ...], timeout_seconds: float) -> ExecResult | None:
        if argv == conformance_deadline._RECORDED_WRITER_ARGV:
            self.record_written()
            raise SandboxExecTimeoutError(timeout_seconds=timeout_seconds, stdout_len=_WRITTEN, stderr_len=1)
        return None


class _WriterNotKilledSession(_OneShotSession):
    async def one_shot(self, argv: tuple[str, ...], timeout_seconds: float) -> ExecResult | None:
        if argv == conformance_deadline._RECORDED_WRITER_ARGV:
            return ExecResult(exit_code=0, stdout="", stderr="")
        return None


class _EndsAtCommandExitSession(_OneShotSession):
    async def one_shot(self, argv: tuple[str, ...], timeout_seconds: float) -> ExecResult | None:
        if argv == conformance_deadline._HELD_TO_END_ARGV:
            # Ends the exec at its command's exit, the writer's later lines lost.
            return ExecResult(exit_code=conformance_deadline._HELD_TO_END_EXIT_CODE, stdout="1\n", stderr="")
        return None


class _ReturnsBeforeTheWriterEndsSession(_OneShotSession):
    async def one_shot(self, argv: tuple[str, ...], timeout_seconds: float) -> ExecResult | None:
        if argv == conformance_deadline._HELD_TO_END_ARGV:
            lines = "".join(f"{i}\n" for i in range(1, conformance_deadline._HELD_TO_END_LINES + 1))
            return ExecResult(exit_code=conformance_deadline._HELD_TO_END_EXIT_CODE, stdout=lines, stderr="")
        return None


def _sandbox(session_cls: type[FakeSandboxSession]) -> FakeSandbox:
    """A fake provider whose sessions are ``session_cls``, its permissive policy bound."""
    sandbox_cls = type("_OneShotSandbox", (FakeSandbox,), {"session_cls": session_cls})
    return with_permissive_policy(sandbox_cls())


async def test_the_fake_passes_the_timeout_count_check() -> None:
    await check_exec_timeout_counts_output(with_permissive_policy(FakeSandbox()), FAKE_CONFORMANCE_CONFIG)


@pytest.mark.parametrize(
    ("session_cls", "match"),
    [
        (_CountFrozenAtTimeoutSession, "counted stdout 4096 and stderr 0 bytes of the 8192 to 12288"),
        (_CountsStderrSession, "counted stdout 8192 and stderr 1 bytes"),
        (_WriterNotKilledSession, "writing past its timeout was not killed"),
    ],
    ids=["count-frozen-at-timeout", "counts-stderr", "not-killed"],
)
async def test_a_timeout_that_miscounts_the_output_produced_is_caught(
    session_cls: type[FakeSandboxSession], match: str
) -> None:
    with pytest.raises(AssertionError, match=match):
        await check_exec_timeout_counts_output(_sandbox(session_cls), FAKE_CONFORMANCE_CONFIG)


async def test_the_fake_passes_the_read_to_end_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conformance_deadline, "_HELD_TO_END_SECONDS", 0.05)
    await check_output_held_after_exit_read_to_end(with_permissive_policy(FakeSandbox()), FAKE_CONFORMANCE_CONFIG)


async def test_an_exec_ended_at_its_commands_exit_while_its_output_is_held_is_caught() -> None:
    with pytest.raises(AssertionError, match=r"returned exit 3 and stdout '1\\n'"):
        await check_output_held_after_exit_read_to_end(_sandbox(_EndsAtCommandExitSession), FAKE_CONFORMANCE_CONFIG)


async def test_an_exec_that_returns_before_the_holder_of_its_output_ends_is_caught() -> None:
    with pytest.raises(AssertionError, match="held for 3s returned in"):
        await check_output_held_after_exit_read_to_end(
            _sandbox(_ReturnsBeforeTheWriterEndsSession), FAKE_CONFORMANCE_CONFIG
        )
