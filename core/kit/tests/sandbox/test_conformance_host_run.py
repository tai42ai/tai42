"""The conformance checks against a provider whose execs are real host processes.

The in-memory fakes run nothing, so a check's effect on the processes it starts shows only
against execs that really run: each session here runs its execs in its own host directory.
"""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import SecretStr
from tai42_contract.sandbox import ExecResult, SandboxExecHandle

from tai42_kit.sandbox import (
    ExecDeadline,
    HostExecHandle,
    HostProcess,
    ManagedSandbox,
    ManagedSandboxSession,
    SandboxConformanceConfig,
    permissive_policy,
)
from tai42_kit.sandbox.conformance_deadline import (
    check_interactive_timeout_counts_large_output,
    check_interactive_timeout_counts_output,
    check_kill_after_deadline,
    check_late_read_of_large_output,
    check_output_closed_exec_killed_at_deadline,
    check_output_holder_at_deadline_times_out,
    check_signalled_exec_with_output_holder_times_out,
    check_slow_read_of_large_output,
    check_unread_exec_killed_at_deadline,
)
from tai42_kit.sandbox.conformance_group_stop import (
    check_interactive_deadline_kills_what_the_command_left_in_its_group,
    check_kill_kills_what_the_command_left_in_its_group,
)

from .fakes import FakeSandbox, FakeSandboxSession

_GONE_SECONDS = 5.0


class _HostRunSession(FakeSandboxSession):
    """Runs every exec as a host process in :attr:`workdir`; its teardown kills each one, as a host session's does."""

    workdir: ClassVar[Path]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.processes: list[HostProcess] = []

    async def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, SecretStr] | None = None,
        stdin: bytes | None = None,
        timeout_seconds: float,
    ) -> ExecResult:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self.workdir,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(stdin), timeout_seconds)
        assert proc.returncode is not None
        return ExecResult(exit_code=proc.returncode, stdout=stdout.decode(), stderr=stderr.decode())

    async def exec_start(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, SecretStr] | None = None,
        timeout_seconds: float,
    ) -> SandboxExecHandle:
        process = await HostProcess.spawn(argv, cwd=str(self.workdir), env=dict(os.environ), stdin=True)
        self.processes.append(process)
        return HostExecHandle(process, deadline=ExecDeadline.from_now(timeout_seconds))


class _HostRunSandbox(FakeSandbox):
    session_cls: ClassVar[type[FakeSandboxSession]] = _HostRunSession

    async def _destroy_session_resources(self, session: ManagedSandboxSession, *, remove_workspace: bool) -> None:
        assert isinstance(session, _HostRunSession)
        for process in session.processes:
            await process.kill()
        await super()._destroy_session_resources(session, remove_workspace=remove_workspace)


def _live_pids_in(workdir: Path) -> list[int]:
    """The pids of every live (non-zombie) host process whose cwd is ``workdir``."""
    pids: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if Path(os.readlink(entry / "cwd")) != workdir:
                continue
            state = (entry / "stat").read_text().rsplit(")", 1)[1].split()[0]
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if state != "Z":
            pids.append(int(entry.name))
    return pids


async def _until_none_live_in(workdir: Path) -> None:
    while _live_pids_in(workdir):
        await asyncio.sleep(0.02)


async def test_the_kill_after_deadline_check_stops_the_pipe_holder_it_starts(tmp_path, monkeypatch) -> None:
    workdir = (tmp_path / "workspace").resolve()
    workdir.mkdir()
    monkeypatch.setattr(_HostRunSession, "workdir", workdir, raising=False)
    sandbox = _HostRunSandbox()
    sandbox.bind_policy(permissive_policy())
    try:
        await check_kill_after_deadline(sandbox, SandboxConformanceConfig(image="host:run"))
        await asyncio.wait_for(_until_none_live_in(workdir), _GONE_SECONDS)
    finally:
        for pid in _live_pids_in(workdir):
            os.kill(pid, signal.SIGKILL)


async def test_a_host_exec_nothing_reads_passes_the_unread_exec_check(tmp_path, monkeypatch) -> None:
    workdir = (tmp_path / "workspace").resolve()
    workdir.mkdir()
    monkeypatch.setattr(_HostRunSession, "workdir", workdir, raising=False)
    sandbox = _HostRunSandbox()
    sandbox.bind_policy(permissive_policy())
    await check_unread_exec_killed_at_deadline(sandbox, SandboxConformanceConfig(image="host:run"))


async def test_a_host_exec_timeout_passes_the_output_count_check(tmp_path, monkeypatch) -> None:
    workdir = (tmp_path / "workspace").resolve()
    workdir.mkdir()
    monkeypatch.setattr(_HostRunSession, "workdir", workdir, raising=False)
    sandbox = _HostRunSandbox()
    sandbox.bind_policy(permissive_policy())
    await check_interactive_timeout_counts_output(sandbox, SandboxConformanceConfig(image="host:run"))


async def test_a_host_exec_that_closed_its_output_passes_the_closed_output_check(tmp_path, monkeypatch) -> None:
    workdir = (tmp_path / "workspace").resolve()
    workdir.mkdir()
    monkeypatch.setattr(_HostRunSession, "workdir", workdir, raising=False)
    sandbox = _HostRunSandbox()
    sandbox.bind_policy(permissive_policy())
    await check_output_closed_exec_killed_at_deadline(sandbox, SandboxConformanceConfig(image="host:run"))


async def test_a_host_exec_ended_behind_its_unread_output_passes_the_large_output_late_read_check(
    tmp_path, monkeypatch
) -> None:
    workdir = (tmp_path / "workspace").resolve()
    workdir.mkdir()
    monkeypatch.setattr(_HostRunSession, "workdir", workdir, raising=False)
    sandbox = _HostRunSandbox()
    sandbox.bind_policy(permissive_policy())
    await check_late_read_of_large_output(sandbox, SandboxConformanceConfig(image="host:run"))


async def test_a_host_exec_ended_in_time_passes_the_large_output_slow_read_check(tmp_path, monkeypatch) -> None:
    workdir = (tmp_path / "workspace").resolve()
    workdir.mkdir()
    monkeypatch.setattr(_HostRunSession, "workdir", workdir, raising=False)
    sandbox = _HostRunSandbox()
    sandbox.bind_policy(permissive_policy())
    await check_slow_read_of_large_output(sandbox, SandboxConformanceConfig(image="host:run"))


async def test_a_host_exec_timeout_passes_the_large_output_count_check(tmp_path, monkeypatch) -> None:
    workdir = (tmp_path / "workspace").resolve()
    workdir.mkdir()
    monkeypatch.setattr(_HostRunSession, "workdir", workdir, raising=False)
    sandbox = _HostRunSandbox()
    sandbox.bind_policy(permissive_policy())
    await check_interactive_timeout_counts_large_output(sandbox, SandboxConformanceConfig(image="host:run"))


@pytest.mark.parametrize(
    "check", [check_output_holder_at_deadline_times_out, check_signalled_exec_with_output_holder_times_out]
)
async def test_a_host_exec_whose_output_a_process_holds_passes_the_output_holder_check_and_stops_its_writer(
    tmp_path, monkeypatch, check
) -> None:
    workdir = (tmp_path / "workspace").resolve()
    workdir.mkdir()
    monkeypatch.setattr(_HostRunSession, "workdir", workdir, raising=False)
    sandbox = _HostRunSandbox()
    sandbox.bind_policy(permissive_policy())
    try:
        await check(sandbox, SandboxConformanceConfig(image="host:run"))
        await asyncio.wait_for(_until_none_live_in(workdir), _GONE_SECONDS)
    finally:
        for pid in _live_pids_in(workdir):
            os.kill(pid, signal.SIGKILL)


@pytest.mark.parametrize(
    "check",
    [
        check_interactive_deadline_kills_what_the_command_left_in_its_group,
        check_kill_kills_what_the_command_left_in_its_group,
    ],
)
async def test_a_host_exec_whose_command_left_a_process_in_its_group_passes_the_group_stop_check(
    tmp_path, monkeypatch, check: Callable[[ManagedSandbox, SandboxConformanceConfig], Awaitable[None]]
) -> None:
    workdir = (tmp_path / "workspace").resolve()
    workdir.mkdir()
    monkeypatch.setattr(_HostRunSession, "workdir", workdir, raising=False)
    sandbox = _HostRunSandbox()
    sandbox.bind_policy(permissive_policy())
    try:
        await check(sandbox, SandboxConformanceConfig(image="host:run"))
        await asyncio.wait_for(_until_none_live_in(workdir), _GONE_SECONDS)
    finally:
        for pid in _live_pids_in(workdir):
            os.kill(pid, signal.SIGKILL)
