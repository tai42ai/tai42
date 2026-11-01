"""Session-level tests for the host-subprocess runtime — real subprocesses, no engine.

Each test drives a genuine ``LocalSandbox`` session over a temp workspace root and
exercises a runtime-I/O behavior of :class:`LocalSandboxSession` /
:class:`~tai42_kit.sandbox.HostExecHandle` it returns: the per-exec env overlay, the empty-argv guard, the
interactive reader's stderr path, the interactive-exec timeout (deadline branch and
process-group kill), the in-flight ``write_stdin`` broken-pipe handler, and the
non-``FileNotFound`` OSError paths of ``put_file`` / ``get_file``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from typing import Any

import pytest
from pydantic import SecretStr
from tai42_contract.sandbox import (
    SandboxError,
    SandboxExecTimeoutError,
    SandboxFileNotFoundError,
    SandboxSessionSpec,
    SandboxStreamChunk,
    SandboxStreamExit,
)
from tai42_kit.sandbox import HostProcess

import tai42_sandbox_local
from tai42_sandbox_local.sessions import LocalSandboxSession

LocalSandbox = tai42_sandbox_local.LocalSandbox

# A command that leaves a descendant in its own process group, with its output sent to
# /dev/null, prints the descendant's pid, and exits.
_DETACHED_SURVIVOR = ["sh", "-c", 'sleep 120 >/dev/null 2>&1 & echo "$!"']
_PROMPT_SECONDS = 5.0


def _kill_pid(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


async def _is_gone(pid: int) -> bool:
    """Whether ``pid`` no longer exists within the prompt bound (the survivors here are reaped by init)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _PROMPT_SECONDS
    while loop.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        await asyncio.sleep(0.05)
    return False


def _spec(
    *,
    workspace_key: str = "ws-sess",
    durability: str = "ephemeral",
    env: dict[str, SecretStr] | None = None,
) -> SandboxSessionSpec:
    return SandboxSessionSpec(
        image="host",
        workspace_key=workspace_key,
        durability=durability,  # pyright: ignore[reportArgumentType]
        network="egress",  # pyright: ignore[reportArgumentType]
        isolation="none",  # pyright: ignore[reportArgumentType]
        env=env or {},
        cpu=None,
        memory_mb=None,
        labels={},
        ttl_seconds=300,
    )


# -- env overlay -----------------------------------------------------------------


async def test_per_exec_env_overlays_spec_env(sandbox: LocalSandbox) -> None:
    """The per-exec ``env`` is applied over the clean base and WINS over ``spec.env``
    on a key collision, while a non-colliding spec key survives."""
    session = await sandbox.create_session(
        _spec(env={"SPEC_ONLY": SecretStr("from-spec"), "SHARED": SecretStr("spec-wins?")})
    )
    try:
        result = await session.exec(
            ["printenv"],
            env={"CALL_ONLY": SecretStr("from-call"), "SHARED": SecretStr("call-wins")},
            timeout_seconds=30,
        )
        lines = set(result.stdout.splitlines())
        assert "SPEC_ONLY=from-spec" in lines, "a non-colliding spec.env key was dropped"
        assert "CALL_ONLY=from-call" in lines, "the per-exec env was not applied"
        assert "SHARED=call-wins" in lines, "the per-exec env did not win the collision"
    finally:
        await session.destroy()


@pytest.mark.parametrize(
    ("env", "reason"),
    [
        pytest.param({"ACME=X": "secret-value"}, "illegal environment variable name", id="name-holds-equals"),
        pytest.param({"": "secret-value"}, "illegal environment variable name", id="empty-name"),
        pytest.param({"ACME": "secret\0value"}, "embedded null byte", id="value-holds-nul"),
        pytest.param(
            {"\xe9": "secret-value", "\udcc3\udca9": "secret-value"},
            "env names 'é' and '\\udcc3\\udca9' are the same name on the host: b'\\xc3\\xa9'",
            id="names-the-host-holds-as-one",
        ),
    ],
)
@pytest.mark.parametrize("door", ["exec", "exec_start"])
async def test_an_env_the_host_cannot_hold_raises_a_typed_error_naming_no_value(
    sandbox: LocalSandbox, env: dict[str, str], reason: str, door: str
) -> None:
    session = await sandbox.create_session(_spec())
    try:
        secret_env = {name: SecretStr(value) for name, value in env.items()}
        start = session.exec if door == "exec" else session.exec_start
        with pytest.raises(SandboxError) as raised:
            await start(["true"], env=secret_env, timeout_seconds=30)
        assert str(raised.value) == f"sandbox exec could not spawn 'true': {reason}"
        assert isinstance(raised.value.__cause__, ValueError)
    finally:
        await session.destroy()


# -- argv guard ------------------------------------------------------------------


async def test_empty_argv_rejected(sandbox: LocalSandbox) -> None:
    session = await sandbox.create_session(_spec())
    try:
        with pytest.raises(SandboxError, match="non-empty argv"):
            await session.exec([], timeout_seconds=30)
    finally:
        await session.destroy()


# -- interactive reader / stderr -------------------------------------------------


async def test_interactive_reader_captures_stderr(sandbox: LocalSandbox) -> None:
    """The interactive reader's stderr branch tags a stderr frame as ``"stderr"``."""
    session = await sandbox.create_session(_spec())
    try:
        handle = await session.exec_start(["sh", "-c", "echo to-err 1>&2"], timeout_seconds=30)
        stderr = bytearray()
        async for item in handle.output:
            if isinstance(item, SandboxStreamChunk) and item.stream == "stderr":
                stderr.extend(item.data)
        assert b"to-err" in bytes(stderr), "the stderr stream was not captured/tagged"
    finally:
        await session.destroy()


# -- interactive exec timeout ----------------------------------------------------


async def test_interactive_exec_timeout_kills_silent_process(sandbox: LocalSandbox) -> None:
    """A silent interactive exec that outlives its deadline is killed and the output
    iterator raises the timeout error carrying LENGTHS (the queue-wait deadline branch)."""
    session = await sandbox.create_session(_spec())
    try:
        handle = await session.exec_start(["sleep", "30"], timeout_seconds=0.2)
        with pytest.raises(SandboxExecTimeoutError) as excinfo:
            async for _item in handle.output:
                pass
        assert excinfo.value.timeout_seconds == 0.2
        assert excinfo.value.stdout_len == 0
        # The process group was killed, so a follow-up write is refused (already exited).
        with pytest.raises(SandboxError):
            await handle.write_stdin(b"after-timeout")
    finally:
        await session.destroy()


async def test_interactive_exec_deadline_runs_from_its_start_not_its_first_read(sandbox: LocalSandbox) -> None:
    """An exec whose output is first read after its deadline passed times out on that read at
    once, and its whole process group is killed: the deadline is fixed when the exec starts."""
    session = await sandbox.create_session(_spec())
    try:
        handle = await session.exec_start(["sh", "-c", "sleep 120 & echo $! > child.pid; wait"], timeout_seconds=1.0)
        await asyncio.sleep(1.5)
        loop = asyncio.get_running_loop()
        read_at = loop.time()
        with pytest.raises(SandboxExecTimeoutError) as excinfo:
            async for _item in handle.output:
                pass
        assert loop.time() - read_at < 0.5, "the deadline started at the first read, not at the exec's start"
        assert excinfo.value.timeout_seconds == 1.0
        child_pid = int((await session.get_file("child.pid")).decode())
        assert await _is_gone(child_pid), "the timed-out exec's process group was not killed"
    finally:
        await session.destroy()


async def test_interactive_exec_that_ended_within_its_deadline_gives_its_output_and_exit_however_late_it_is_read(
    sandbox: LocalSandbox,
) -> None:
    session = await sandbox.create_session(_spec())
    try:
        handle = await session.exec_start(["sh", "-c", "echo done; exit 3"], timeout_seconds=0.5)
        await asyncio.sleep(1.0)
        items = [item async for item in handle.output]
        assert items == [SandboxStreamChunk(stream="stdout", data=b"done\n"), SandboxStreamExit(exit_code=3)]
    finally:
        await session.destroy()


async def test_interactive_exec_timeout_kills_flooding_process(sandbox: LocalSandbox) -> None:
    """A flooding interactive exec keeps the drain loop busy until the deadline elapses
    between frames (the ``remaining <= 0`` branch), then is killed with a nonzero
    stdout length recorded."""
    session = await sandbox.create_session(_spec())
    try:
        handle = await session.exec_start(["yes"], timeout_seconds=0.3)
        with pytest.raises(SandboxExecTimeoutError) as excinfo:
            async for _item in handle.output:
                pass
        assert excinfo.value.stdout_len > 0, "a flooding exec recorded no stdout length"
    finally:
        await session.destroy()


# -- teardown reaps a live interactive exec --------------------------------------


async def test_session_destroy_ends_a_live_interactive_exec(sandbox: LocalSandbox) -> None:
    """Destroying a session with a still-running interactive exec kills it: the exec's
    output then ends promptly with exactly one exit item."""
    session = await sandbox.create_session(_spec())
    handle = await session.exec_start(["sleep", "30"], timeout_seconds=30)
    await session.destroy()
    async with asyncio.timeout(5):
        items = [item async for item in handle.output]
    assert items == [SandboxStreamExit(exit_code=-signal.SIGKILL)]


# -- teardown and the exec's process group ----------------------------------------


async def test_destroying_a_session_stops_what_its_exited_interactive_exec_left_in_its_group(
    sandbox: LocalSandbox,
) -> None:
    session = await sandbox.create_session(_spec())
    handle = await session.exec_start(_DETACHED_SURVIVOR, timeout_seconds=30)
    items = [item async for item in handle.output]
    assert items[-1] == SandboxStreamExit(exit_code=0)
    [first] = [item for item in items if isinstance(item, SandboxStreamChunk)]
    survivor_pid = int(first.data)
    try:
        await session.destroy()
        assert await _is_gone(survivor_pid), "the session destroy left the exec's group member running"
    finally:
        _kill_pid(survivor_pid)


# -- write_stdin broken pipe -----------------------------------------------------


async def test_write_stdin_broken_pipe_surfaces_typed(sandbox: LocalSandbox) -> None:
    """A ``write_stdin`` whose drain is in flight when the child exits without reading
    surfaces the broken pipe as a typed :class:`SandboxError` (not a raw OSError).

    The child never reads stdin and exits after a beat; a payload far larger than the
    pipe buffer forces ``drain`` to block, so the guard passes at call time and the
    connection loss is observed mid-write."""
    session = await sandbox.create_session(_spec())
    try:
        handle = await session.exec_start(["sh", "-c", "sleep 0.3"], timeout_seconds=30)
        with pytest.raises(SandboxError, match="stdin"):
            await handle.write_stdin(b"x" * (4 * 1024 * 1024))
    finally:
        await session.destroy()


async def test_a_one_shot_exec_whose_command_closes_its_stdin_returns_its_result(sandbox: LocalSandbox) -> None:
    """A command that closes its stdin before reading it chose not to: the exec returns its result."""
    session = await sandbox.create_session(_spec())
    try:
        result = await session.exec(
            ["sh", "-c", "exec 0<&-; printf done"], stdin=b"x" * (4 * 1024 * 1024), timeout_seconds=30
        )
        assert (result.exit_code, result.stdout) == (0, "done")
    finally:
        await session.destroy()


async def test_a_one_shot_exec_whose_stdin_write_fails_kills_its_process(
    sandbox: LocalSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A one-shot ``exec`` whose stdin write fails while the command still reads raises, and kills the process."""
    session = await sandbox.create_session(_spec())
    assert isinstance(session, LocalSandboxSession)
    spawned: list[HostProcess] = []
    spawn = session._spawn

    async def _recording_spawn(*args: Any, **kwargs: Any) -> HostProcess:
        process = await spawn(*args, **kwargs)
        spawned.append(process)
        return process

    async def _failed_write(self: HostProcess, data: bytes) -> None:
        raise SandboxError("sandbox exec stdin write failed: the host refused it")

    monkeypatch.setattr(session, "_spawn", _recording_spawn)
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


# -- file transfer OSError (non-miss) --------------------------------------------


async def test_put_file_non_miss_oserror_surfaces_typed(sandbox: LocalSandbox) -> None:
    """A ``put_file`` whose parent component is a regular file raises a typed error
    NAMING the path (a non-``FileNotFound`` OSError, not a miss)."""
    session = await sandbox.create_session(_spec())
    try:
        await session.put_file("occupied", b"i am a file")
        with pytest.raises(SandboxError, match="put_file failed for 'occupied/child'"):
            await session.put_file("occupied/child", b"nope")
    finally:
        await session.destroy()


async def test_get_file_directory_oserror_surfaces_typed(sandbox: LocalSandbox) -> None:
    """A ``get_file`` on a directory raises a typed error NAMING the path (an
    ``IsADirectoryError`` is a non-``FileNotFound`` OSError, distinct from a miss)."""
    session = await sandbox.create_session(_spec())
    try:
        await session.put_file("adir/inner.txt", b"x")
        with pytest.raises(SandboxError, match="get_file failed for 'adir'"):
            await session.get_file("adir")
    finally:
        await session.destroy()


async def test_get_file_miss_raises_typed_not_found(sandbox: LocalSandbox) -> None:
    """A genuine miss surfaces as the typed subclass carrying the requested path."""
    session = await sandbox.create_session(_spec())
    try:
        with pytest.raises(SandboxFileNotFoundError) as excinfo:
            await session.get_file("absent.txt")
        assert excinfo.value.path == "absent.txt"
        assert str(excinfo.value) == "sandbox file 'absent.txt' not found"
    finally:
        await session.destroy()


async def test_get_file_real_error_raises_base_sandbox_error(sandbox: LocalSandbox) -> None:
    """A real read failure (a directory, not an absent path) stays the base error and is
    never mistaken for the not-found subclass."""
    session = await sandbox.create_session(_spec())
    try:
        await session.put_file("adir/inner.txt", b"x")
        with pytest.raises(SandboxError) as excinfo:
            await session.get_file("adir")
        assert not isinstance(excinfo.value, SandboxFileNotFoundError)
    finally:
        await session.destroy()
