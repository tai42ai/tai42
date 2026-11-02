"""The process-based fake provider's one-shot ``exec``, driven in-process.

Every other sandbox module drives the fake through the running SUT; these cases need the
exec's host process itself, so the fixture module is imported here against a stub app that
takes its import-time registrations.
"""

from __future__ import annotations

import asyncio
import importlib
import signal
from collections.abc import Callable
from types import ModuleType
from typing import Any

import pytest
from pydantic import SecretStr
from tai42_contract.app import tai42_app
from tai42_contract.sandbox import SandboxError, SandboxSessionSpec
from tai42_kit.sandbox import HostProcess, ManagedSandboxSession, permissive_policy

pytestmark = [pytest.mark.backendless, pytest.mark.needs("no-stack")]


class _StubSandboxes:
    def register_sandbox(self, cls: type) -> type:
        return cls


class _StubLifecycle:
    def on_shutdown(self, fn: Callable[[], None]) -> Callable[[], None]:
        return fn


class _StubApp:
    def __init__(self) -> None:
        self.sandboxes = _StubSandboxes()
        self.lifecycle = _StubLifecycle()


def _fixture_module() -> ModuleType:
    with tai42_app.bound(_StubApp()):
        return importlib.import_module("tai42_e2e_fixtures.sandbox_provider")


async def _session(sandbox: Any) -> ManagedSandboxSession:
    sandbox.bind_policy(permissive_policy())
    spec = SandboxSessionSpec(image="host", workspace_key="one-shot", durability="ephemeral", ttl_seconds=60)
    return await sandbox.create_session(spec)


def _record_spawns(
    session: ManagedSandboxSession, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[HostProcess], asyncio.Event]:
    """The processes ``session`` spawns from now on, and an event set once the first is spawned."""
    spawned: list[HostProcess] = []
    first_spawned = asyncio.Event()
    spawn = session._spawn  # type: ignore[attr-defined]

    async def _recording_spawn(*args: Any, **kwargs: Any) -> HostProcess:
        process = await spawn(*args, **kwargs)
        spawned.append(process)
        first_spawned.set()
        return process

    monkeypatch.setattr(session, "_spawn", _recording_spawn)
    return spawned, first_spawned


async def test_a_one_shot_exec_whose_command_closes_its_stdin_returns_its_result() -> None:
    sandbox = _fixture_module().FakeSandbox()
    session = await _session(sandbox)
    try:
        result = await session.exec(
            ["sh", "-c", "exec 0<&-; printf done"], stdin=b"x" * (4 * 1024 * 1024), timeout_seconds=30
        )
        assert (result.exit_code, result.stdout) == (0, "done")
    finally:
        await session.destroy()
        sandbox.dispose()


async def test_a_one_shot_exec_whose_stdin_write_fails_kills_its_process(monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox = _fixture_module().FakeSandbox()
    session = await _session(sandbox)
    spawned, _ = _record_spawns(session, monkeypatch)

    async def _failed_write(self: HostProcess, data: bytes) -> None:
        raise SandboxError("sandbox exec stdin write failed: the host refused it")

    monkeypatch.setattr(HostProcess, "write", _failed_write)
    try:
        with pytest.raises(SandboxError, match=r"^sandbox exec stdin write failed: the host refused it$"):
            await session.exec(["sleep", "30"], stdin=b"x", timeout_seconds=30)
        [process] = spawned
        assert process.returncode == -signal.SIGKILL
    finally:
        for process in spawned:
            await process.kill()
        await session.destroy()
        sandbox.dispose()


async def test_a_cancelled_one_shot_exec_kills_its_process(monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox = _fixture_module().FakeSandbox()
    session = await _session(sandbox)
    spawned, first_spawned = _record_spawns(session, monkeypatch)
    try:
        call = asyncio.ensure_future(session.exec(["sleep", "30"], timeout_seconds=30))
        async with asyncio.timeout(5):
            await first_spawned.wait()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        [process] = spawned
        assert process.returncode == -signal.SIGKILL
    finally:
        for process in spawned:
            await process.kill()
        await session.destroy()
        sandbox.dispose()


@pytest.mark.parametrize("door", ["exec", "exec_start"])
async def test_an_env_the_host_cannot_hold_raises_a_typed_error_naming_no_value(door: str) -> None:
    sandbox = _fixture_module().FakeSandbox()
    session = await _session(sandbox)
    try:
        start = session.exec if door == "exec" else session.exec_start
        with pytest.raises(SandboxError) as raised:
            await start(["true"], env={"ACME": SecretStr("secret\0value")}, timeout_seconds=30)
        assert str(raised.value) == "sandbox exec could not spawn 'true': embedded null byte"
        assert isinstance(raised.value.__cause__, ValueError)
    finally:
        await session.destroy()
        sandbox.dispose()
