"""The exec checks of the sandbox conformance suite: the exec seam itself.

Each check drives a live :class:`~tai42_kit.sandbox.base.ManagedSandbox`'s execs through one
behaviour every consumer relies on: the ``spec.env`` credential channel, a one-shot exec's stdin
(far past any buffer, and left unread by its command), and the interactive byte-stream seam (stdin
echo, one exit item, an idempotent ``kill`` after exit, the output received before a ``kill`` read
after it). What an exec's deadline does is checked in :mod:`~tai42_kit.sandbox.conformance_deadline`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from pydantic import SecretStr
from tai42_contract.sandbox import (
    SandboxError,
    SandboxExecHandle,
    SandboxStreamChunk,
    SandboxStreamExit,
)

from tai42_kit.sandbox.base import ManagedSandbox
from tai42_kit.sandbox.conformance_harness import SandboxConformanceConfig, conformance_spec, stop_descendant
from tai42_kit.sandbox.session import ManagedSandboxSession

# The one-shot stdin the large-stdin case feeds ``cat``: far past any pipe or transport buffer,
# so a provider that writes the whole stdin before reading any output never finishes it.
_LARGE_STDIN = b"conf-stdin\n" * 400_000

# The ``timeout_seconds`` of the unread-stdin case's ``head -c 1``: an exec whose output ended
# returns at once, so only a provider that holds the exec open on the unwritten stdin runs past it.
_UNREAD_STDIN_TIMEOUT_SECONDS = 10.0

# What the kill-keeps-output case's exec writes before it is killed, nothing reading it yet.
_BEFORE_KILL_OUTPUT = b"conf-before-kill"

# How long the kill-keeps-output case leaves its exec unread before it kills it: long enough for
# the provider to receive the exec's output.
_BEFORE_KILL_SETTLE_SECONDS = 2.0

# The kill-keeps-output case's exec's own bound, far past the case, so only the kill ends it.
_BEFORE_KILL_TIMEOUT_SECONDS = 60.0

# The bound of the kill-keeps-output case's read after the kill: the exec's output ends at it.
_AFTER_KILL_READ_SECONDS = 10.0

# The file, in the session's working directory, where the kill-keeps-output case's held exec
# records the pid of the descendant that holds its output.
_KILL_HOLDER_PIDFILE = ".tai42-conformance-kill-holder"

# The kill-keeps-output case's execs: one that writes its output and runs on, and one whose
# descendant, out of its process group (``setsid``), holds its output past the kill.
_BEFORE_KILL_ARGV = ("sh", "-c", f"printf {_BEFORE_KILL_OUTPUT.decode()}; exec sleep 600")
_BEFORE_KILL_HELD_ARGV = (
    "sh",
    "-c",
    f'setsid sleep 120 & echo "$!" > {_KILL_HOLDER_PIDFILE}; printf {_BEFORE_KILL_OUTPUT.decode()}; exec sleep 600',
)

# The exec that stops the held exec's descendant once the kill-keeps-output case is done.
_STOP_KILL_HOLDER_ARGV = ("sh", "-c", f'kill -KILL "$(cat {_KILL_HOLDER_PIDFILE})"')


async def check_exec_and_env(sandbox: ManagedSandbox, config: SandboxConformanceConfig) -> None:
    """A session runs ``exec`` to completion, with ``spec.env`` visible as that exec's base env.

    This pins ``spec.env`` as every exec's credential channel.
    """
    session = await sandbox.create_session(conformance_spec(config, env={"CONF_SECRET": SecretStr("conf-value")}))
    try:
        result = await session.exec(["printenv", "CONF_SECRET"], timeout_seconds=30)
        if result.exit_code != 0:
            raise AssertionError(f"printenv exited {result.exit_code}")
        if "conf-value" not in result.stdout:
            raise AssertionError("spec.env value was not the exec's base env")
    finally:
        await session.destroy()


async def check_one_shot_large_stdin(sandbox: ManagedSandbox, config: SandboxConformanceConfig) -> None:
    """A one-shot ``exec`` feeds a stdin far larger than any buffer to a command that echoes it.

    ``cat`` takes no more input while its output waits unread, so the stdin is fed while the
    output is read; the whole stdin comes back on stdout and the exec exits 0.
    """
    session = await sandbox.create_session(conformance_spec(config))
    try:
        result = await session.exec(["cat"], stdin=_LARGE_STDIN, timeout_seconds=60)
        if result.exit_code != 0:
            raise AssertionError(f"cat of a large one-shot stdin exited {result.exit_code}")
        if result.stdout != _LARGE_STDIN.decode():
            raise AssertionError(
                f"a large one-shot stdin did not come back whole: {len(result.stdout)} of "
                f"{len(_LARGE_STDIN)} bytes on stdout"
            )
    finally:
        await session.destroy()


async def check_one_shot_unread_stdin(sandbox: ManagedSandbox, config: SandboxConformanceConfig) -> None:
    """A one-shot ``exec`` of a command that exits without reading most of its stdin returns its result.

    ``head -c 1`` takes one byte of a stdin far larger than any buffer and exits: the command
    chose not to read the rest, as ``communicate`` takes it, so the exec returns its exit code and
    output within its timeout instead of raising for the unwritten input or waiting on it.
    """
    session = await sandbox.create_session(conformance_spec(config))
    try:
        result = await session.exec(
            ["head", "-c", "1"], stdin=_LARGE_STDIN, timeout_seconds=_UNREAD_STDIN_TIMEOUT_SECONDS
        )
        if (result.exit_code, result.stdout) != (0, _LARGE_STDIN[:1].decode()):
            raise AssertionError(
                f"head -c 1 of a large one-shot stdin gave exit {result.exit_code} and stdout {result.stdout!r}"
            )
    finally:
        await session.destroy()


async def _kill_twice_after_exit(handle: SandboxExecHandle) -> None:
    """Kill an exited exec twice: idempotent once the exec has exited, neither call raises."""
    for _ in range(2):
        try:
            await handle.kill()
        except Exception as exc:
            raise AssertionError(f"kill after exit raised {exc!r}") from exc


async def check_interactive_exec(sandbox: ManagedSandbox, config: SandboxConformanceConfig) -> None:
    """The interactive seam: stdin echo, one exit item, an idempotent ``kill`` after exit, typed write-after-exit.

    Written stdin is echoed on the output stream, the stream ends with one exit item, ``kill``
    after exit returns without error and so does a second, and write-after-exit raises a typed
    :class:`SandboxError`.
    """
    session = await sandbox.create_session(conformance_spec(config))
    try:
        handle = await session.exec_start(["cat"], timeout_seconds=30)
        await handle.write_stdin(b"conf-ping\n")
        await handle.close_stdin()

        stdout = bytearray()
        exit_code: int | None = None
        async for item in handle.output:
            if isinstance(item, SandboxStreamChunk):
                if item.stream == "stdout":
                    stdout.extend(item.data)
            elif isinstance(item, SandboxStreamExit):
                exit_code = item.exit_code

        if exit_code is None:
            raise AssertionError("the interactive stream carried no exit item")
        if b"conf-ping" not in bytes(stdout):
            raise AssertionError("written stdin was not echoed on the output stream")

        await _kill_twice_after_exit(handle)

        try:
            await handle.write_stdin(b"after-exit")
        except SandboxError:
            pass
        else:
            raise AssertionError("write_stdin after exit did not raise a typed SandboxError")
    finally:
        await session.destroy()


async def check_kill_keeps_received_output(sandbox: ManagedSandbox, config: SandboxConformanceConfig) -> None:
    """The output an interactive exec gave before a ``kill``, unread then, is read after it, then one exit item.

    The exec writes its output and runs on; nothing reads it until the provider has received it,
    then a ``kill`` ends the exec, and a read after the kill yields that output, in order, then
    ends with one :class:`SandboxStreamExit`. It holds as well for an exec whose descendant,
    out of its process group, still holds its output past the kill: the case then stops that
    descendant itself.
    """
    session = await sandbox.create_session(conformance_spec(config))
    try:
        await _assert_kill_keeps_received_output(await _start(session, _BEFORE_KILL_ARGV), "an exec")
        # Started before the stop's guard: an exec that never started has no descendant to stop.
        held = await _start(session, _BEFORE_KILL_HELD_ARGV)
        try:
            await _assert_kill_keeps_received_output(held, "an exec whose descendant holds its output")
        finally:
            await stop_descendant(session, _STOP_KILL_HOLDER_ARGV, "kill-keeps-output")
    finally:
        await session.destroy()


async def _start(session: ManagedSandboxSession, argv: Sequence[str]) -> SandboxExecHandle:
    """Start ``argv`` as an interactive exec with the case's bound."""
    return await session.exec_start(list(argv), timeout_seconds=_BEFORE_KILL_TIMEOUT_SECONDS)


async def _assert_kill_keeps_received_output(handle: SandboxExecHandle, what: str) -> None:
    """Leave ``handle``'s output unread while the provider receives it, kill the exec, and assert the read after."""
    await asyncio.sleep(_BEFORE_KILL_SETTLE_SECONDS)
    await handle.kill()
    try:
        async with asyncio.timeout(_AFTER_KILL_READ_SECONDS):
            items = [item async for item in handle.output]
    except TimeoutError as exc:
        raise AssertionError(
            f"the output of {what} did not end within {_AFTER_KILL_READ_SECONDS}s of its kill"
        ) from exc
    stdout = b"".join(item.data for item in items if isinstance(item, SandboxStreamChunk) and item.stream == "stdout")
    if stdout != _BEFORE_KILL_OUTPUT:
        raise AssertionError(
            f"the output of {what} read after its kill gave stdout {stdout!r}, not the {_BEFORE_KILL_OUTPUT!r} "
            "it wrote before the kill"
        )
    exits = [item for item in items if isinstance(item, SandboxStreamExit)]
    if len(exits) != 1 or not isinstance(items[-1], SandboxStreamExit):
        raise AssertionError(f"the output of {what} read after its kill did not end with one exit item")
