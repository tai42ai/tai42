"""Session-level tests for the host-subprocess runtime — real subprocesses, no engine.

Each test drives a genuine ``LocalSandbox`` session over a temp workspace root and
exercises a runtime-I/O behavior of :class:`LocalSandboxSession` /
:class:`LocalSandboxExecHandle`: the per-exec env overlay, the empty-argv guard, the
interactive reader's stderr path, the interactive-exec timeout (deadline branch and
process-group kill), the in-flight ``write_stdin`` broken-pipe handler, and the
non-``FileNotFound`` OSError paths of ``put_file`` / ``get_file``.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr
from tai42_contract.sandbox import (
    SandboxError,
    SandboxExecTimeoutError,
    SandboxSessionSpec,
    SandboxStreamChunk,
)

import tai42_sandbox_local
from tai42_sandbox_local.sessions import LocalSandboxExecHandle

LocalSandbox = tai42_sandbox_local.LocalSandbox


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
        assert isinstance(handle, LocalSandboxExecHandle)
        assert handle._proc.returncode is None  # guard would-pass precondition
        with pytest.raises(SandboxError, match="stdin"):
            await handle.write_stdin(b"x" * (4 * 1024 * 1024))
    finally:
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
