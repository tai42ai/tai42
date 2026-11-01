"""Spawning host children in tests, and watching what they leave in their process group."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
from collections.abc import AsyncIterator, Iterator

import pytest
from tai42_contract.sandbox import (
    SandboxStreamChunk,
    SandboxStreamExit,
)

from tai42_kit.sandbox import ExecDeadline, HostExecHandle, HostProcess, run_host_process

# A child that leaves a descendant in its own session holding the child's stdout and stderr (the
# shell gives a background job ``/dev/null`` for its stdin), then echoes stdin until EOF. The
# descendant prints its pid once it is in its own session.
PIPE_HOLDER = ("sh", "-c", "setsid sh -c 'echo \"$$\"; exec sleep 120' & exec cat")


# A child that leaves a descendant in its own session holding the child's stdin as well, never
# reading it, then sleeps. The descendant prints its pid once it is in its own session.
STDIN_HOLDER = ("sh", "-c", "exec 3<&0; setsid sh -c 'echo \"$$\"; exec sleep 120' 0<&3 & exec sleep 120")


# The same, with the descendant's output sent to /dev/null once it printed its pid, and a child
# that exits half a second in, so its output ends at its exit while the descendant holds stdin.
EXITING_STDIN_HOLDER = (
    "sh",
    "-c",
    "exec 3<&0; setsid sh -c 'echo \"$$\"; exec sleep 120 >/dev/null 2>&1' 0<&3 & exec sleep 0.5",
)


# A child that leaves a descendant in its own process group holding its stdout, prints the
# descendant's pid, and exits.
GROUP_SURVIVOR = ("sh", "-c", 'sleep 120 & echo "$!"')


# The same, with the descendant's output sent to /dev/null, so the child's output ends at its exit.
DETACHED_SURVIVOR = ("sh", "-c", 'sleep 120 >/dev/null 2>&1 & echo "$!"')


# The warning a kill logs when the group's anchor was gone before it.
HOLD_ENDED = (
    "sandbox exec group hold for pid {pid} ended before the kill: processes the command left running "
    "in its group may still run"
)


# Far below the holder's lifetime: a kill that waited on the holder's pipes would miss it.
PROMPT_SECONDS = 5.0


# Every process a test spawned, killed when the test ends so none outlives it with its group held.
SPAWNED: list[HostProcess] = []


async def spawn(*argv: str, stdin: bool = False) -> HostProcess:
    process = await HostProcess.spawn(argv, cwd=os.getcwd(), env={"PATH": os.environ["PATH"]}, stdin=stdin)
    SPAWNED.append(process)
    return process


def exec_handle(process: HostProcess, *, timeout_seconds: float) -> HostExecHandle:
    """The interactive handle over the started ``process``, its ``timeout_seconds`` deadline fixed now."""
    return HostExecHandle(process, deadline=ExecDeadline.from_now(timeout_seconds))


async def open_the_loops_own_fds() -> None:
    """Run one child to its end, so the fds the loop opens at its first spawn and keeps are open.

    uvloop opens ``/dev/null`` at its first spawn and holds it for the loop's life; an fd
    snapshot taken after this call holds it, so it never counts as an fd a later spawn left open.
    """
    process = await HostProcess.spawn(["true"], cwd=os.getcwd(), env={"PATH": os.environ["PATH"]}, stdin=False)
    await run_host_process(process, stdin=None, timeout_seconds=PROMPT_SECONDS)


def group_members(pgid: int) -> set[int]:
    """The pids of every process whose process-group id is ``pgid``."""
    found = subprocess.run(["pgrep", "-g", str(pgid)], capture_output=True, text=True, check=False)
    if found.returncode not in (0, 1):
        raise AssertionError(f"pgrep failed: {found.stderr}")
    return {int(pid) for pid in found.stdout.split()}


async def group_becomes(pgid: int, members: set[int]) -> bool:
    """Whether the group ``pgid`` holds exactly ``members`` within the prompt bound."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PROMPT_SECONDS
    while group_members(pgid) != members:
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.05)
    return True


@contextlib.contextmanager
def recorded_killpg(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[int]]:
    """Record the pgid of every ``os.killpg`` that sends a signal while still sending it; signal 0 sends none."""
    calls: list[int] = []
    real = os.killpg

    def _killpg(pgid: int, sig: int) -> None:
        if sig != 0:
            calls.append(pgid)
        real(pgid, sig)

    monkeypatch.setattr(os, "killpg", _killpg)
    yield calls


async def drain(process: HostProcess) -> list[SandboxStreamChunk]:
    chunks: list[SandboxStreamChunk] = []
    while (chunk := await process.next_output()) is not None:
        chunks.append(chunk)
    return chunks


async def collect(output: AsyncIterator[SandboxStreamChunk | SandboxStreamExit]) -> list[object]:
    return [item async for item in output]


def kill_pid(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


async def is_gone(pid: int) -> bool:
    """Whether ``pid`` no longer exists within the prompt bound (the survivors here are reaped by init)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PROMPT_SECONDS
    while loop.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        await asyncio.sleep(0.05)
    return False


async def first_pid(process: HostProcess) -> int:
    first = await process.next_output()
    assert first is not None
    return int(first.data.split()[0])


async def exited(process: HostProcess) -> None:
    while process.returncode is None:
        await asyncio.sleep(0.01)


# What a write into the child's stdin raises once its exit closed it: a write pending at the exit,
# and a write made after it.
PENDING_WRITE_FAILED = r"^sandbox exec stdin write failed: the exec's stdin pipe is closed$"
STDIN_CLOSED = r"^sandbox exec stdin is closed or the exec has exited; cannot write$"


# What a run, and an interactive exec's output, raise once the command exited with input still
# unwritten while a process it started held its stdin.
STDIN_CUT = (
    r"^sandbox exec stdin was cut: the exec exited with code 0 while a process it started still held "
    r"its stdin, with [1-9][0-9]* bytes of its input unwritten$"
)


@contextlib.asynccontextmanager
async def holding_descendant(argv: tuple[str, ...]) -> AsyncIterator[tuple[HostProcess, int]]:
    """A started child whose descendant prints its pid first, and that pid; the descendant is killed by pid on exit."""
    process = await spawn(*argv, stdin=True)
    descendant_pid = await first_pid(process)
    try:
        yield process, descendant_pid
    finally:
        kill_pid(descendant_pid)
        await process.kill()
