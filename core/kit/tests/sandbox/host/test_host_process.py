"""The host process: its pipes, its spawn, and a kill that reaches its whole group."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import errno
import logging
import os
import resource
import signal
import subprocess
import threading
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from tai42_contract.sandbox import (
    SandboxError,
    SandboxStreamChunk,
    SandboxStreamExit,
)

from tai42_kit.sandbox import HostProcess, anchored_exec, run_host_process
from tai42_kit.sandbox.host_process import _OUTPUT_HIGH_WATER, _PipeProtocol

from .children import (
    GROUP_SURVIVOR,
    HOLD_ENDED,
    PENDING_WRITE_FAILED,
    PIPE_HOLDER,
    PROMPT_SECONDS,
    SPAWNED,
    STDIN_CLOSED,
    STDIN_CUT,
    STDIN_HOLDER,
    collect,
    drain,
    exec_handle,
    exited,
    first_pid,
    group_becomes,
    group_members,
    holding_descendant,
    is_gone,
    kill_pid,
    open_the_loops_own_fds,
    recorded_killpg,
    spawn,
)


async def test_output_chunks_are_tallied_and_end_after_the_exit() -> None:
    process = await spawn("sh", "-c", "printf out; printf errs >&2; exit 3")
    chunks = await drain(process)
    assert b"".join(c.data for c in chunks if c.stream == "stdout") == b"out"
    assert b"".join(c.data for c in chunks if c.stream == "stderr") == b"errs"
    assert (process.stdout_len, process.stderr_len) == (3, 4)
    assert process.exit_code() == 3


async def test_a_child_whose_output_nobody_reads_blocks_until_it_is_read() -> None:
    size = 10_000_000  # far past the output buffer and the pipe buffers
    process = await spawn("sh", "-c", f"head -c {size} /dev/zero; head -c {size} /dev/zero >&2")
    try:
        # Nobody reads, so past the high-water mark the buffer stops taking the child's output and
        # the child blocks on its pipe. The loop delivers every read it already took off the pipe
        # when reading pauses (uvloop takes several in one batch), so what was taken is bounded by
        # the pipe, not by the mark, and it stays the same while the child waits.
        async with asyncio.timeout(PROMPT_SECONDS):
            while process.stdout_len <= _OUTPUT_HIGH_WATER:
                await asyncio.sleep(0.01)
        taken = process.stdout_len + process.stderr_len
        await asyncio.sleep(1.0)
        assert process.stdout_len + process.stderr_len == taken < size
        assert process.returncode is None
        chunks = await drain(process)
    finally:
        await process.kill()
    assert sum(len(c.data) for c in chunks if c.stream == "stdout") == size
    assert sum(len(c.data) for c in chunks if c.stream == "stderr") == size
    assert process.exit_code() == 0


async def test_written_stdin_reaches_the_child_until_it_is_closed() -> None:
    process = await spawn("cat", stdin=True)
    await process.write(b"hello")
    process.close_stdin()
    chunks = await drain(process)
    assert b"".join(c.data for c in chunks) == b"hello"
    assert process.exit_code() == 0
    with pytest.raises(SandboxError, match="stdin is closed or the exec has exited"):
        await process.write(b"late")


async def test_a_child_spawned_without_stdin_reads_eof_not_the_host_stdin() -> None:
    read_end, write_end = os.pipe()
    os.write(write_end, b"HOST-PROCESS-STDIN")
    os.close(write_end)
    saved_stdin = os.dup(0)
    os.dup2(read_end, 0)
    try:
        process = await spawn("cat")
    finally:
        os.dup2(saved_stdin, 0)
        os.close(saved_stdin)
        os.close(read_end)
    chunks = await drain(process)
    assert b"".join(c.data for c in chunks) == b""
    assert process.exit_code() == 0


async def test_a_write_into_a_pipe_the_child_closed_raises() -> None:
    # The child closes its stdin and keeps running, so the write meets a broken pipe.
    process = await spawn("sh", "-c", "exec 0<&-; echo closed; sleep 30", stdin=True)
    try:
        assert await process.next_output() == SandboxStreamChunk(stream="stdout", data=b"closed\n")
        with pytest.raises(SandboxError, match="stdin"):
            await process.write(b"x" * 1_000_000)
    finally:
        await process.kill()


async def test_a_write_larger_than_the_pipe_waits_for_the_child_to_read_it() -> None:
    data = b"x" * 4_000_000  # far past the pipe buffer, so the write pauses until the child reads
    process = await spawn("sh", "-c", "wc -c", stdin=True)
    await process.write(data)
    process.close_stdin()
    chunks = await drain(process)
    assert int(b"".join(c.data for c in chunks)) == len(data)


async def test_a_write_the_child_stops_reading_mid_way_raises() -> None:
    # The write fills the pipe and waits; the child then closes its stdin, so the wait ends in the loss.
    process = await spawn("sh", "-c", "sleep 0.3; exec 0<&-; sleep 30", stdin=True)
    try:
        with pytest.raises(
            SandboxError, match=r"^sandbox exec stdin write failed: the exec's stdin pipe is closed$"
        ) as raised:
            await process.write(b"x" * 4_000_000)
        assert isinstance(raised.value.__cause__, BrokenPipeError)
    finally:
        await process.kill()


class _ExitedChild(asyncio.SubprocessTransport):
    """A child that exited with code 0, its stdin the given pipe transport and no other pipe."""

    def __init__(self, stdin: asyncio.WriteTransport) -> None:
        super().__init__()
        self._stdin = stdin

    def get_pipe_transport(self, fd: int) -> asyncio.BaseTransport | None:
        return self._stdin if fd == 0 else None

    def get_returncode(self) -> int | None:
        return 0

    def close(self) -> None:
        pass


@pytest.mark.parametrize("reader_left", [True, False], ids=["a-reader-left", "no-reader-left"])
async def test_an_exit_with_stdin_unwritten_is_a_cut_only_while_a_reader_is_left(reader_left: bool) -> None:
    # The exit is handled before the write side learns the pipe lost its reader, an order the
    # event loop may take when both arrive together: only a reader left makes the dropped bytes a cut.
    read_end, write_end = os.pipe()
    stdin, _ = await asyncio.get_running_loop().connect_write_pipe(asyncio.BaseProtocol, os.fdopen(write_end, "wb"))
    try:
        stdin.write(b"x" * 1_000_000)  # past the pipe buffer, so most of it stays unwritten
        if not reader_left:
            os.close(read_end)
        protocol = _PipeProtocol(asyncio.get_running_loop())
        protocol.connection_made(_ExitedChild(stdin))
        protocol.process_exited()
        protocol.pipe_connection_lost(1, None)
        protocol.pipe_connection_lost(2, None)
        if reader_left:
            with pytest.raises(SandboxError, match=STDIN_CUT):
                await protocol.take()
        else:
            assert await protocol.take() is None
    finally:
        if not stdin.is_closing():
            stdin.abort()
        if reader_left:
            os.close(read_end)


# Past the output buffer's high-water mark, yet within what the buffer and one pipe hold, so the
# child writes all of it and goes on while nobody reads.
_PAST_THE_BUFFER = 160 * 1024


async def test_a_child_is_running_until_it_exits_with_no_process_left_holding_its_output() -> None:
    process = await spawn("sh", "-c", f"read line; exec head -c {_PAST_THE_BUFFER} /dev/zero", stdin=True)
    assert process.running
    process.close_stdin()
    await exited(process)
    # Output the child wrote is still unread in its pipe: the child is over all the same.
    assert not process.running
    chunks = await drain(process)
    assert sum(len(chunk.data) for chunk in chunks) == _PAST_THE_BUFFER


async def test_a_child_whose_descendant_holds_its_output_is_running_after_its_exit() -> None:
    async with holding_descendant(PIPE_HOLDER) as (process, _holder_pid):
        process.close_stdin()
        await exited(process)
        assert process.running


async def test_a_child_that_closed_its_output_is_running_until_it_exits() -> None:
    process = await spawn("sh", "-c", "exec >/dev/null 2>&1; exec sleep 30")
    async with asyncio.timeout(PROMPT_SECONDS):
        while process._protocol._open_outputs:
            await asyncio.sleep(0.01)
    assert process.running
    await process.kill()
    assert not process.running


async def test_a_kill_counts_the_output_the_child_left_unread_in_its_pipes() -> None:
    # Once the buffer stops reading, the child writes a little more to each pipe: those bytes stay
    # unread in the pipes until the kill closes them.
    process = await spawn(
        "sh",
        "-c",
        f"head -c {_PAST_THE_BUFFER} /dev/zero; read line; printf ab; printf cde >&2; exec sleep 30",
        stdin=True,
    )
    async with asyncio.timeout(PROMPT_SECONDS):
        while not process._protocol._reading_paused:
            await asyncio.sleep(0.01)
    await process.write(b"go\n")
    await asyncio.sleep(0.5)
    await process.kill()
    assert (process.stdout_len, process.stderr_len) == (_PAST_THE_BUFFER + 2, 3)


async def test_kill_ends_the_output_at_once_while_a_descendant_holds_the_pipes() -> None:
    async with holding_descendant(PIPE_HOLDER) as (process, _holder_pid):
        async with asyncio.timeout(PROMPT_SECONDS):
            await process.kill()
            assert await process.next_output() is None
        assert process.exit_code() == -signal.SIGKILL
        await process.kill()  # idempotent once the child has exited
        assert process.exit_code() == -signal.SIGKILL


async def test_a_kill_after_the_child_exited_ends_the_output_while_a_descendant_holds_the_pipes() -> None:
    async with holding_descendant(PIPE_HOLDER) as (process, _holder_pid):
        process.close_stdin()
        await exited(process)
        async with asyncio.timeout(PROMPT_SECONDS):
            await process.kill()
            assert await process.next_output() is None
        assert process.exit_code() == 0


async def test_a_kill_cancelled_while_it_waits_for_the_exit_still_ends_the_output() -> None:
    async with holding_descendant(PIPE_HOLDER) as (process, _holder_pid):
        killing = asyncio.ensure_future(process.kill())
        await asyncio.sleep(0)
        # A cancel at every loop pass while the kill waits for the child's exit.
        while not killing.done():
            killing.cancel()
            await asyncio.sleep(0)
        with pytest.raises(asyncio.CancelledError):
            await killing
        async with asyncio.timeout(PROMPT_SECONDS):
            assert await process.next_output() is None
        assert process.exit_code() == -signal.SIGKILL


@pytest.mark.parametrize("stdin_closed_first", [False, True], ids=["stdin-open", "stdin-closing"])
async def test_a_kill_fails_a_write_pending_on_a_stdin_a_descendant_holds_and_every_later_one(
    stdin_closed_first: bool,
) -> None:
    async with holding_descendant(STDIN_HOLDER) as (process, _holder_pid):
        # More than the pipe holds, which nobody reads: the write stays pending.
        writing = asyncio.ensure_future(process.write(b"x" * 1_000_000))
        await asyncio.sleep(0.2)
        assert not writing.done()
        if stdin_closed_first:
            # A close waits for the pipe to take what is buffered.
            process.close_stdin()
        async with asyncio.timeout(PROMPT_SECONDS):
            await process.kill()
            with pytest.raises(SandboxError, match=PENDING_WRITE_FAILED):
                await writing
            assert await process.next_output() is None
        with pytest.raises(SandboxError, match=STDIN_CLOSED):
            await process.write(b"later")


async def test_kill_after_the_child_exited_stops_what_it_left_running_in_its_group() -> None:
    process = await spawn(*GROUP_SURVIVOR)
    survivor_pid = await first_pid(process)
    try:
        await exited(process)
        await process.kill()
        assert await is_gone(survivor_pid), "the kill left the child's group member running"
    finally:
        kill_pid(survivor_pid)


async def test_a_kill_after_the_anchor_was_killed_from_outside_sends_nothing_to_the_group_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    process = await spawn(*GROUP_SURVIVOR)
    pid = process._transport.get_pid()
    survivor_pid = await first_pid(process)
    try:
        await exited(process)
        for other in group_members(pid) - {survivor_pid}:
            kill_pid(other)
        assert await group_becomes(pid, {survivor_pid})
        with recorded_killpg(monkeypatch) as calls, caplog.at_level(logging.WARNING, logger="tai42_kit.sandbox"):
            await process.kill()
        assert calls == []
        assert HOLD_ENDED.format(pid=pid) in caplog.messages
    finally:
        kill_pid(survivor_pid)


async def test_a_kill_after_the_group_emptied_never_signals_a_process_that_reused_the_pgid() -> None:
    process = await spawn("true")
    handle = exec_handle(process, timeout_seconds=PROMPT_SECONDS)
    assert await collect(handle.output) == [SandboxStreamExit(exit_code=0)]
    pid = process._transport.get_pid()
    for other in group_members(pid):
        kill_pid(other)
    assert await group_becomes(pid, set())
    # The group is empty, so its id is free: the next process started with that pid, as its
    # own group leader, is no part of the exec.
    try:
        with open("/proc/sys/kernel/ns_last_pid", "w") as next_pid:
            next_pid.write(str(pid - 1))
    except (PermissionError, FileNotFoundError):
        pytest.skip("writing /proc/sys/kernel/ns_last_pid needs CAP_CHECKPOINT_RESTORE over the pid namespace")
    unrelated = subprocess.Popen(["sleep", "100"], start_new_session=True)
    try:
        assert unrelated.pid == pid, "another process took the freed pid first"
        await handle.kill()
        await asyncio.sleep(0.3)
        assert unrelated.poll() is None, "the kill signalled a process that reused the exec's pgid"
    finally:
        unrelated.kill()
        unrelated.wait()


async def test_a_second_kill_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    process = await spawn("sleep", "30")
    with recorded_killpg(monkeypatch) as calls:
        await process.kill()
        await process.kill()
    assert calls == [process._transport.get_pid()]


async def test_a_kill_still_reaches_what_survived_the_commands_own_group_signal() -> None:
    process = await spawn(
        "sh", "-c", 'trap "" TERM; sh -c "trap \\"\\" TERM; exec sleep 120" & echo "$!"; kill -TERM 0'
    )
    survivor_pid = await first_pid(process)
    try:
        await exited(process)
        assert process.exit_code() == 0
        await process.kill()
        assert await is_gone(survivor_pid), "the kill left the command's TERM-proof survivor running"
    finally:
        kill_pid(survivor_pid)


async def test_no_process_keeps_the_exec_group_after_a_kill_or_a_completed_run() -> None:
    killed = await spawn(*GROUP_SURVIVOR)
    survivor_pid = await first_pid(killed)
    try:
        await exited(killed)
        await killed.kill()
        assert await group_becomes(killed._transport.get_pid(), set())
    finally:
        kill_pid(survivor_pid)
    completed = await spawn("true")
    await run_host_process(completed, stdin=None, timeout_seconds=PROMPT_SECONDS)
    assert await group_becomes(completed._transport.get_pid(), set())


async def test_a_spawn_with_no_program_raises_before_anything_starts() -> None:
    with pytest.raises(ValueError, match="argv"):
        await HostProcess.spawn([], cwd=os.getcwd(), env={"PATH": os.environ["PATH"]}, stdin=False)


async def test_a_kill_after_the_anchor_ended_still_kills_the_running_command_alone(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    process = await spawn("sleep", "30")
    pid = process._transport.get_pid()
    [anchor] = group_members(pid) - {pid}
    kill_pid(anchor)
    assert await is_gone(anchor)
    with recorded_killpg(monkeypatch) as calls, caplog.at_level(logging.WARNING, logger="tai42_kit.sandbox"):
        await asyncio.wait_for(process.kill(), timeout=PROMPT_SECONDS)
    assert process.returncode == -signal.SIGKILL
    assert calls == []
    assert HOLD_ENDED.format(pid=pid) in caplog.messages


# Prepended to the bootstrap, a stall before it does anything, so its spawn waits on the report.
_BOOTSTRAP_STALLS = "import time\ntime.sleep(60)\n"


async def test_a_spawn_cancelled_while_it_waits_for_the_bootstrap_stops_it_and_closes_its_fds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await open_the_loops_own_fds()
    monkeypatch.setattr(anchored_exec, "ANCHORED_EXEC", _BOOTSTRAP_STALLS + anchored_exec.ANCHORED_EXEC)
    marker = str(tmp_path / "command-ran")
    open_fds = set(os.listdir("/dev/fd"))
    spawning = asyncio.ensure_future(spawn("touch", marker))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PROMPT_SECONDS
    while subprocess.run(["pgrep", "-f", marker], capture_output=True, check=False).returncode != 0:
        assert loop.time() < deadline, "the bootstrap never started"
        await asyncio.sleep(0.05)
    spawning.cancel()
    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(PROMPT_SECONDS):
            await spawning
    while subprocess.run(["pgrep", "-f", marker], capture_output=True, check=False).returncode != 1:
        assert loop.time() < deadline + PROMPT_SECONDS, "the cancelled spawn left its bootstrap running"
        await asyncio.sleep(0.05)
    assert set(os.listdir("/dev/fd")) == open_fds, "the cancelled spawn left an fd of its own open"


async def test_a_spawn_cancelled_again_while_it_cleans_up_still_stops_its_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(anchored_exec, "ANCHORED_EXEC", _BOOTSTRAP_STALLS + anchored_exec.ANCHORED_EXEC)
    marker = str(tmp_path / "command-ran")
    # More env than a pipe holds, so its write is still pending at the cancels.
    env = {"PATH": os.environ["PATH"], "ACME_LARGE": "x" * 100_000, "ACME_OTHER": "y" * 100_000}
    loop = asyncio.get_running_loop()
    try:
        spawning = asyncio.ensure_future(HostProcess.spawn(["touch", marker], cwd=os.getcwd(), env=env, stdin=False))
        deadline = loop.time() + PROMPT_SECONDS
        while subprocess.run(["pgrep", "-f", marker], capture_output=True, check=False).returncode != 0:
            assert loop.time() < deadline, "the bootstrap never started"
            await asyncio.sleep(0.05)
        # A cancel, then another at every loop pass while the spawn cleans up.
        while not spawning.done():
            spawning.cancel()
            await asyncio.sleep(0)
        with pytest.raises(asyncio.CancelledError):
            await spawning
        while subprocess.run(["pgrep", "-f", marker], capture_output=True, check=False).returncode != 1:
            assert loop.time() < deadline + PROMPT_SECONDS, "the twice-cancelled spawn left its bootstrap running"
            await asyncio.sleep(0.05)
    finally:
        subprocess.run(["pkill", "-KILL", "-f", marker], check=False)


def _open_fds() -> set[int]:
    """The fds this process has open, each probed: a listing of them holds one of its own."""
    open_fds: set[int] = set()
    for fd in range(max(int(name) for name in os.listdir("/proc/self/fd")) + 1):
        try:
            os.fstat(fd)
        except OSError:
            continue
        open_fds.add(fd)
    return open_fds


@pytest.mark.parametrize(
    "free_fds",
    [
        pytest.param(2, id="room-for-the-hold-socket-pair"),
        pytest.param(4, id="room-for-the-report-pipe-too"),
    ],
)
async def test_a_spawn_out_of_fds_closes_every_fd_it_made(free_fds: int) -> None:
    open_fds = _open_fds()
    # The fd limit that leaves exactly ``free_fds`` numbers free below it.
    limit = next(n for n in range(len(open_fds) + free_fds + 1) if n - len(open_fds & set(range(n))) == free_fds)
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (limit, hard))
    try:
        with pytest.raises(OSError, match="Too many open files") as raised:
            await HostProcess.spawn(["true"], cwd=os.getcwd(), env={}, stdin=False)
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
    assert raised.value.errno == errno.EMFILE
    assert _open_fds() == open_fds, "the spawn left an fd it made open"


@contextlib.asynccontextmanager
async def _default_executor_busy() -> AsyncIterator[None]:
    """The running loop's default executor, one worker, held busy for the block."""
    loop = asyncio.get_running_loop()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(executor)
    release = threading.Event()
    busy = loop.run_in_executor(None, release.wait)
    try:
        yield
    finally:
        release.set()
        await busy


async def test_a_spawn_does_not_wait_on_a_busy_default_executor() -> None:
    async with _default_executor_busy():
        process = await asyncio.wait_for(
            HostProcess.spawn(["true"], cwd=os.getcwd(), env={"PATH": os.environ["PATH"]}, stdin=False),
            timeout=PROMPT_SECONDS,
        )
        SPAWNED.append(process)
        result = await run_host_process(process, stdin=None, timeout_seconds=PROMPT_SECONDS)
    assert result.exit_code == 0


# Prepended to the bootstrap, a stall before it reads its env, with a child of its own that leaves
# the exec's group and keeps every fd of the bootstrap open, the env pipe's read end among them,
# after the kill.
_BOOTSTRAP_STALLS_WITH_A_CHILD = """\
import os, time
if os.fork() == 0:
    os.setsid()
    time.sleep(60)
    os._exit(0)
time.sleep(60)
"""


async def test_a_spawn_cancelled_before_its_env_was_written_closes_its_fds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await open_the_loops_own_fds()
    monkeypatch.setattr(anchored_exec, "ANCHORED_EXEC", _BOOTSTRAP_STALLS_WITH_A_CHILD + anchored_exec.ANCHORED_EXEC)
    marker = str(tmp_path / "command-ran")
    # More env than a pipe holds, so its write is still pending at the cancel.
    env = {"PATH": os.environ["PATH"], "ACME_LARGE": "x" * 100_000, "ACME_OTHER": "y" * 100_000}
    try:
        async with _default_executor_busy():
            open_fds = set(os.listdir("/dev/fd"))
            spawning = asyncio.ensure_future(
                HostProcess.spawn(["touch", marker], cwd=os.getcwd(), env=env, stdin=False)
            )
            loop = asyncio.get_running_loop()
            deadline = loop.time() + PROMPT_SECONDS
            while len(subprocess.run(["pgrep", "-f", marker], capture_output=True, check=False).stdout.split()) < 2:
                assert loop.time() < deadline, "the bootstrap and its child never started"
                await asyncio.sleep(0.05)
            spawning.cancel()
            with pytest.raises(asyncio.CancelledError):
                async with asyncio.timeout(PROMPT_SECONDS):
                    await spawning
            assert set(os.listdir("/dev/fd")) == open_fds, "the cancelled spawn left an fd of its own open"
    finally:
        subprocess.run(["pkill", "-KILL", "-f", marker], check=False)
