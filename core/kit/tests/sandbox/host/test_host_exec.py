"""The exec doors over a host process: a one-shot run and the interactive exec handle."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path
from typing import cast

import pytest
from tai42_contract.sandbox import (
    ExecResult,
    SandboxError,
    SandboxExecTimeoutError,
    SandboxStreamChunk,
    SandboxStreamExit,
)

from tai42_kit.sandbox import run_host_process
from tai42_kit.sandbox.exec_kill import OUTPUT_HELD_NOTE

from .children import (
    DETACHED_SURVIVOR,
    EXITING_STDIN_HOLDER,
    PENDING_WRITE_FAILED,
    PIPE_HOLDER,
    PROMPT_SECONDS,
    STDIN_CLOSED,
    STDIN_CUT,
    STDIN_HOLDER,
    collect,
    exec_handle,
    group_becomes,
    holding_descendant,
    is_gone,
    kill_pid,
    open_the_loops_own_fds,
    recorded_killpg,
    spawn,
)


async def test_an_exec_past_its_deadline_is_killed_and_times_out() -> None:
    process = await spawn("sh", "-c", "printf ab; exec sleep 30")
    handle = exec_handle(process, timeout_seconds=0.3)
    output = handle.output
    assert await anext(output) == SandboxStreamChunk(stream="stdout", data=b"ab")
    with pytest.raises(SandboxExecTimeoutError) as excinfo:
        await anext(output)
    assert (excinfo.value.stdout_len, excinfo.value.stderr_len) == (2, 0)
    assert not hasattr(excinfo.value, "__notes__")
    assert process.returncode == -signal.SIGKILL


async def test_an_exec_first_read_past_its_deadline_times_out_at_once_and_its_group_is_killed() -> None:
    process = await spawn("sh", "-c", "sleep 120 & wait")
    handle = exec_handle(process, timeout_seconds=1.0)
    pgid = process._transport.get_pid()
    # The exec runs past its deadline before its output is first read.
    await asyncio.sleep(1.5)
    loop = asyncio.get_running_loop()
    read_at = loop.time()
    with pytest.raises(SandboxExecTimeoutError):
        await anext(handle.output)
    assert loop.time() - read_at < 0.5, "the deadline started at the first read, not at the exec's start"
    assert await group_becomes(pgid, set())


async def test_an_exec_nobody_reads_is_killed_at_its_deadline_and_takes_no_write_after_it() -> None:
    process = await spawn("sh", "-c", "printf ab; cat > /dev/null", stdin=True)
    handle = exec_handle(process, timeout_seconds=0.3)
    # Nothing reads the output: the deadline alone ends the exec.
    await asyncio.sleep(1.0)
    assert process.returncode == -signal.SIGKILL
    with pytest.raises(SandboxError, match="cannot write"):
        await handle.write_stdin(b"past the deadline\n")
    with pytest.raises(SandboxExecTimeoutError) as excinfo:
        await anext(handle.output)
    assert (excinfo.value.stdout_len, excinfo.value.stderr_len) == (2, 0)


async def test_a_write_racing_the_deadline_kill_raises_once_the_kill_is_done() -> None:
    process = await spawn("sh", "-c", "exec cat > /dev/null", stdin=True)
    handle = exec_handle(process, timeout_seconds=0.2)
    await asyncio.sleep(0.2)
    # The deadline has passed: the write waits out the deadline's kill, then finds stdin gone.
    with pytest.raises(SandboxError, match="cannot write"):
        await handle.write_stdin(b"at the deadline\n")
    assert process.returncode == -signal.SIGKILL


async def test_a_deadline_kill_nobody_reads_that_fails_is_logged_and_noted_on_the_timeout_the_next_read_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    process = await spawn("sh", "-c", "exec sleep 30")

    async def refused_kill() -> None:
        raise OSError("kill refused")

    monkeypatch.setattr(process, "kill", refused_kill)
    handle = exec_handle(process, timeout_seconds=0.1)
    try:
        with caplog.at_level(logging.ERROR, logger="tai42_kit"):
            await asyncio.sleep(0.3)
        assert [record.getMessage() for record in caplog.records] == ["the deadline kill of a sandbox exec failed"]
        with pytest.raises(SandboxExecTimeoutError) as excinfo:
            await anext(handle.output)
        assert excinfo.value.__notes__ == ["the kill of the sandbox exec also failed: kill refused"]
    finally:
        monkeypatch.undo()  # the spawned child is killed for real as the test ends


async def test_a_callers_kill_before_the_deadline_ends_the_deadline_timer() -> None:
    process = await spawn("sh", "-c", "exec sleep 30")
    handle = exec_handle(process, timeout_seconds=0.2)
    await handle.kill()
    assert not handle._deadline_stop.armed
    assert await collect(handle.output) == [SandboxStreamExit(exit_code=-signal.SIGKILL)]


async def test_an_exec_that_ended_within_its_deadline_gives_its_output_and_exit_however_late_it_is_read() -> None:
    process = await spawn("sh", "-c", "printf done; printf err >&2; exit 3")
    handle = exec_handle(process, timeout_seconds=0.5)
    # The exec ends in milliseconds; its output is first read well after its deadline.
    await asyncio.sleep(1.0)
    items = await collect(handle.output)
    chunks = cast("list[SandboxStreamChunk]", items[:-1])
    assert sorted((chunk.stream, chunk.data) for chunk in chunks) == [("stderr", b"err"), ("stdout", b"done")]
    assert items[-1] == SandboxStreamExit(exit_code=3)


# Past the output buffer's high-water mark, yet within what the buffer and one pipe hold, so the
# command writes all of it and ends while nobody reads.
_PAST_THE_BUFFER = 160 * 1024


async def test_an_exec_that_ended_within_its_deadline_gives_output_past_its_buffer_however_late_it_is_read() -> None:
    process = await spawn("head", "-c", str(_PAST_THE_BUFFER), "/dev/zero")
    handle = exec_handle(process, timeout_seconds=0.5)
    # The exec ends in milliseconds, part of its output still in its pipe; it is first read well
    # after its deadline.
    await asyncio.sleep(1.0)
    items = await collect(handle.output)
    chunks = cast("list[SandboxStreamChunk]", items[:-1])
    assert sum(len(chunk.data) for chunk in chunks) == _PAST_THE_BUFFER
    assert items[-1] == SandboxStreamExit(exit_code=0)


async def test_an_exec_past_its_deadline_times_out_counting_the_output_its_pipes_held_unread() -> None:
    # Once the buffer stops reading, the command writes a little more to each pipe: those bytes
    # stay unread in the pipes until the deadline's kill closes them.
    process = await spawn(
        "sh",
        "-c",
        f"head -c {_PAST_THE_BUFFER} /dev/zero; read line; printf ab; printf cde >&2; exec sleep 30",
        stdin=True,
    )
    handle = exec_handle(process, timeout_seconds=1.0)
    async with asyncio.timeout(PROMPT_SECONDS):
        while not process._protocol._reading_paused:
            await asyncio.sleep(0.01)
    await handle.write_stdin(b"go\n")
    # Nothing reads the output: the deadline alone kills the exec.
    await asyncio.sleep(1.5)
    with pytest.raises(SandboxExecTimeoutError) as excinfo:
        await anext(handle.output)
    assert (excinfo.value.stdout_len, excinfo.value.stderr_len) == (_PAST_THE_BUFFER + 2, 3)


async def test_an_exec_that_closed_its_output_but_runs_on_is_killed_at_its_deadline() -> None:
    process = await spawn("sh", "-c", "exec >/dev/null 2>&1; exec sleep 30")
    handle = exec_handle(process, timeout_seconds=0.3)
    await asyncio.sleep(0.8)
    assert process.returncode == -signal.SIGKILL
    with pytest.raises(SandboxExecTimeoutError):
        await anext(handle.output)


async def test_a_killed_exec_past_its_deadline_ends_with_one_exit_and_no_timeout() -> None:
    process = await spawn("sh", "-c", "printf ab; exec sleep 30")
    handle = exec_handle(process, timeout_seconds=0.2)
    output = handle.output
    # The deadline passes after the first read, before the kill.
    assert await anext(output) == SandboxStreamChunk(stream="stdout", data=b"ab")
    await asyncio.sleep(0.4)
    async with asyncio.timeout(PROMPT_SECONDS):
        await handle.kill()
        rest = [item async for item in output]
    assert rest == [SandboxStreamExit(exit_code=-signal.SIGKILL)]


async def test_a_deadline_kill_a_callers_kill_overtakes_ends_with_the_exit_not_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = await spawn("sh", "-c", "printf ab; exec sleep 30")
    handle = exec_handle(process, timeout_seconds=0.2)
    process_kill = process.kill
    deadline_kill_started = asyncio.Event()
    callers_kill_returned = asyncio.Event()

    async def _kill() -> None:
        if not deadline_kill_started.is_set():
            # The deadline's kill: held in flight until the caller's kill has returned.
            deadline_kill_started.set()
            await callers_kill_returned.wait()
        await process_kill()

    monkeypatch.setattr(process, "kill", _kill)
    output = handle.output
    assert await anext(output) == SandboxStreamChunk(stream="stdout", data=b"ab")
    rest = asyncio.ensure_future(collect(output))
    async with asyncio.timeout(PROMPT_SECONDS):
        await deadline_kill_started.wait()
        await handle.kill()
        callers_kill_returned.set()
        assert await rest == [SandboxStreamExit(exit_code=-signal.SIGKILL)]


async def test_the_handle_streams_stdin_through_to_its_exit() -> None:
    process = await spawn("cat", stdin=True)
    handle = exec_handle(process, timeout_seconds=5)
    await handle.write_stdin(b"ping")
    await handle.close_stdin()
    items = [item async for item in handle.output]
    assert items == [SandboxStreamChunk(stream="stdout", data=b"ping"), SandboxStreamExit(exit_code=0)]
    assert handle.output is handle.output  # the one iterator


async def test_a_run_returns_the_output_and_exit_code() -> None:
    process = await spawn("sh", "-c", "printf out; printf err >&2; exit 3")
    result = await run_host_process(process, stdin=None, timeout_seconds=PROMPT_SECONDS)
    assert result == ExecResult(exit_code=3, stdout="out", stderr="err")


async def test_a_run_feeds_its_stdin_and_then_closes_it() -> None:
    process = await spawn("cat", stdin=True)
    result = await run_host_process(process, stdin=b"piped-in", timeout_seconds=PROMPT_SECONDS)
    assert result == ExecResult(exit_code=0, stdout="piped-in", stderr="")


async def test_a_run_echoes_a_stdin_larger_than_the_pipes_and_the_output_buffer() -> None:
    # ``cat`` writes its input back as it reads it, so it takes no more stdin while its output
    # waits unread: a stdin fed whole before any read never finishes.
    data = b"x" * 4_000_000
    process = await spawn("cat", stdin=True)
    result = await run_host_process(process, stdin=data, timeout_seconds=PROMPT_SECONDS)
    assert result == ExecResult(exit_code=0, stdout=data.decode(), stderr="")


async def test_a_run_past_its_timeout_counts_the_output_the_process_produced() -> None:
    process = await spawn("sh", "-c", "head -c 300000 /dev/zero; exec sleep 30")
    with pytest.raises(SandboxExecTimeoutError) as excinfo:
        await run_host_process(process, stdin=None, timeout_seconds=1.0)
    assert (excinfo.value.stdout_len, excinfo.value.stderr_len) == (300_000, 0)


async def test_a_run_past_its_timeout_kills_the_process_and_times_out() -> None:
    process = await spawn("sh", "-c", "printf ab; exec sleep 30")
    with pytest.raises(SandboxExecTimeoutError) as excinfo:
        await run_host_process(process, stdin=None, timeout_seconds=0.3)
    assert (excinfo.value.stdout_len, excinfo.value.stderr_len) == (2, 0)
    assert not hasattr(excinfo.value, "__notes__")
    assert process.returncode == -signal.SIGKILL


async def test_a_run_past_its_timeout_closes_a_stdin_a_descendant_holds_with_its_input_unwritten() -> None:
    await open_the_loops_own_fds()
    open_fds = set(os.listdir("/proc/self/fd"))
    async with holding_descendant(STDIN_HOLDER) as (process, _holder_pid):
        with pytest.raises(SandboxExecTimeoutError):
            # More input than the pipe holds, which nobody reads.
            await run_host_process(process, stdin=b"y" * 1_000_000, timeout_seconds=0.3)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + PROMPT_SECONDS
        while set(os.listdir("/proc/self/fd")) != open_fds:
            assert loop.time() < deadline, "the timed-out run left a pipe of its exec open"
            await asyncio.sleep(0.05)


async def test_a_handle_kill_fails_a_stdin_write_pending_on_a_descendant_and_every_later_one() -> None:
    async with holding_descendant(STDIN_HOLDER) as (process, _holder_pid):
        handle = exec_handle(process, timeout_seconds=PROMPT_SECONDS * 4)
        writing = asyncio.ensure_future(handle.write_stdin(b"x" * 1_000_000))
        await asyncio.sleep(0.2)
        assert not writing.done()
        async with asyncio.timeout(PROMPT_SECONDS):
            await handle.kill()
            with pytest.raises(SandboxError, match=PENDING_WRITE_FAILED):
                await writing
            with pytest.raises(SandboxError, match=STDIN_CLOSED):
                await handle.write_stdin(b"later")


async def test_a_run_whose_command_exits_with_its_input_unwritten_on_a_stdin_a_descendant_holds_raises() -> None:
    async with holding_descendant(EXITING_STDIN_HOLDER) as (process, _holder_pid):
        # More input than the pipe holds, which the descendant holds and never reads.
        with pytest.raises(SandboxError, match=STDIN_CUT):
            await run_host_process(process, stdin=b"y" * 1_000_000, timeout_seconds=PROMPT_SECONDS)


@pytest.mark.parametrize("stdin", [b"y" * 10, b"", None], ids=["written", "empty", "never-fed"])
async def test_a_run_whose_command_exits_with_its_input_written_on_a_stdin_a_descendant_holds_returns(
    stdin: bytes | None,
) -> None:
    async with holding_descendant(EXITING_STDIN_HOLDER) as (process, _holder_pid):
        result = await run_host_process(process, stdin=stdin, timeout_seconds=PROMPT_SECONDS)
        assert result == ExecResult(exit_code=0, stdout="", stderr="")


async def test_a_handle_whose_command_exits_fails_a_stdin_write_pending_on_a_descendant_and_every_later_one() -> None:
    async with holding_descendant(EXITING_STDIN_HOLDER) as (process, _holder_pid):
        handle = exec_handle(process, timeout_seconds=PROMPT_SECONDS * 4)
        writing = asyncio.ensure_future(handle.write_stdin(b"x" * 1_000_000))
        async with asyncio.timeout(PROMPT_SECONDS):
            with pytest.raises(SandboxError, match=STDIN_CUT):
                await collect(handle.output)
            with pytest.raises(SandboxError, match=PENDING_WRITE_FAILED):
                await writing
            with pytest.raises(SandboxError, match=STDIN_CLOSED):
                await handle.write_stdin(b"later")


async def test_a_handle_killed_after_its_command_cut_its_stdin_still_raises_the_cut() -> None:
    async with holding_descendant(EXITING_STDIN_HOLDER) as (process, _holder_pid):
        handle = exec_handle(process, timeout_seconds=PROMPT_SECONDS * 4)
        writing = asyncio.ensure_future(handle.write_stdin(b"x" * 1_000_000))
        async with asyncio.timeout(PROMPT_SECONDS):
            # The pending write fails at the command's exit, the point the cut is recorded.
            with pytest.raises(SandboxError, match=PENDING_WRITE_FAILED):
                await writing
            await handle.kill()
            with pytest.raises(SandboxError, match=STDIN_CUT):
                await collect(handle.output)


async def test_a_handle_whose_command_exits_with_written_input_still_buffered_on_a_descendant_raises_the_cut() -> None:
    async with holding_descendant(EXITING_STDIN_HOLDER) as (process, _holder_pid):
        handle = exec_handle(process, timeout_seconds=PROMPT_SECONDS * 4)
        stdin = cast("asyncio.WriteTransport", process._transport.get_pipe_transport(0))
        async with asyncio.timeout(PROMPT_SECONDS):
            # Chunks smaller than the transport buffers before a write waits, until the stdin
            # channel is full and the transport holds the rest: the close waits on that rest.
            while not stdin.get_write_buffer_size():
                await handle.write_stdin(b"x" * 16384)
                # A loop that writes at its next pass has written the chunk by then.
                await asyncio.sleep(0)
            await handle.close_stdin()
            with pytest.raises(SandboxError, match=STDIN_CUT):
                await collect(handle.output)


async def test_a_handle_whose_command_exits_with_its_input_written_on_a_descendant_ends_with_its_exit() -> None:
    async with holding_descendant(EXITING_STDIN_HOLDER) as (process, _holder_pid):
        handle = exec_handle(process, timeout_seconds=PROMPT_SECONDS * 4)
        async with asyncio.timeout(PROMPT_SECONDS):
            await handle.write_stdin(b"x" * 10)
            assert await collect(handle.output) == [SandboxStreamExit(exit_code=0)]


async def test_a_run_past_its_timeout_stops_what_its_exited_command_left_running_in_its_group(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "survivor.pid"
    process = await spawn("sh", "-c", f'sleep 120 & echo "$!" > {pid_file}; echo started')
    try:
        with pytest.raises(SandboxExecTimeoutError):
            await run_host_process(process, stdin=None, timeout_seconds=0.5)
        assert process.exit_code() == 0
        assert await is_gone(int(pid_file.read_text())), "the timeout left the command's group member running"
    finally:
        if pid_file.exists():
            kill_pid(int(pid_file.read_text()))


async def test_a_run_whose_command_closes_its_stdin_stops_the_feed_and_returns_its_result() -> None:
    # Closing its stdin is the command's choice, as ``communicate`` takes it: the rest of the
    # input is not fed, and the run ends with the command's own end.
    process = await spawn("sh", "-c", "exec 0<&-; printf done", stdin=True)
    result = await run_host_process(process, stdin=b"x" * 4_000_000, timeout_seconds=PROMPT_SECONDS)
    assert result == ExecResult(exit_code=0, stdout="done", stderr="")


async def test_a_run_whose_command_exits_without_reading_its_stdin_returns_its_result() -> None:
    # The command exits before, while or after the stdin is written: every run returns.
    for _ in range(20):
        process = await spawn("sh", "-c", "echo hi", stdin=True)
        result = await run_host_process(process, stdin=b"input", timeout_seconds=PROMPT_SECONDS)
        assert result == ExecResult(exit_code=0, stdout="hi\n", stderr="")


async def test_a_run_whose_command_reads_part_of_a_large_stdin_returns_its_result() -> None:
    process = await spawn("head", "-c", "1", stdin=True)
    result = await run_host_process(process, stdin=b"y" * 2_000_000, timeout_seconds=PROMPT_SECONDS)
    assert result == ExecResult(exit_code=0, stdout="y", stderr="")


async def test_a_run_whose_stdin_cannot_be_written_kills_the_process_and_raises() -> None:
    # A process spawned without a stdin pipe has no stdin the command could have closed.
    process = await spawn("sleep", "30")
    with pytest.raises(SandboxError, match=r"^sandbox exec stdin is closed or the exec has exited; cannot write$"):
        await run_host_process(process, stdin=b"x", timeout_seconds=PROMPT_SECONDS)
    assert process.returncode == -signal.SIGKILL


async def test_a_cancelled_run_kills_the_process() -> None:
    process = await spawn("sleep", "30")
    run = asyncio.ensure_future(run_host_process(process, stdin=None, timeout_seconds=PROMPT_SECONDS))
    await asyncio.sleep(0.1)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert process.returncode == -signal.SIGKILL


async def test_a_completed_run_releases_its_group_so_a_later_kill_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    process = await spawn(*DETACHED_SURVIVOR)
    pid = process._transport.get_pid()
    result = await run_host_process(process, stdin=None, timeout_seconds=PROMPT_SECONDS)
    survivor_pid = int(result.stdout)
    try:
        assert await group_becomes(pid, {survivor_pid}), "the run's group still holds more than its survivor"
        with recorded_killpg(monkeypatch) as calls, caplog.at_level(logging.WARNING, logger="tai42_kit.sandbox"):
            await process.kill()
        assert calls == []
        assert caplog.messages == []
    finally:
        kill_pid(survivor_pid)


async def test_an_exec_whose_output_a_descendant_holds_at_its_deadline_times_out_noting_the_holder() -> None:
    async with holding_descendant(PIPE_HOLDER) as (process, _holder_pid):
        handle = exec_handle(process, timeout_seconds=0.3)
        with pytest.raises(SandboxExecTimeoutError) as excinfo:
            await asyncio.wait_for(collect(handle.output), PROMPT_SECONDS)
    assert excinfo.value.__notes__ == [OUTPUT_HELD_NOTE]


# A command that exits at once, leaving a process in its group that holds its output, its pid
# written first on that output.
_LEFT_IN_GROUP = ("sh", "-c", "sh -c 'echo $$; exec sleep 30' & exit 0")


async def test_a_run_whose_command_left_a_process_in_its_group_times_out_with_it_killed_and_no_holder_noted() -> None:
    process = await spawn(*_LEFT_IN_GROUP)
    with pytest.raises(SandboxExecTimeoutError) as excinfo:
        await asyncio.wait_for(run_host_process(process, stdin=None, timeout_seconds=0.5), PROMPT_SECONDS)
    assert not hasattr(excinfo.value, "__notes__")
    assert excinfo.value.stdout_len > 0


async def test_an_exec_whose_command_left_a_process_in_its_group_times_out_with_it_killed_and_no_holder_noted() -> None:
    process = await spawn(*_LEFT_IN_GROUP)
    handle = exec_handle(process, timeout_seconds=0.5)
    output = handle.output
    first = await anext(output)
    assert isinstance(first, SandboxStreamChunk)
    left_pid = int(first.data)
    with pytest.raises(SandboxExecTimeoutError) as excinfo:
        await asyncio.wait_for(anext(output), PROMPT_SECONDS)
    assert not hasattr(excinfo.value, "__notes__")
    assert await is_gone(left_pid)


async def test_a_run_whose_output_a_descendant_holds_at_its_timeout_notes_the_holder() -> None:
    async with holding_descendant(PIPE_HOLDER) as (process, _holder_pid):
        with pytest.raises(SandboxExecTimeoutError) as excinfo:
            await asyncio.wait_for(run_host_process(process, stdin=None, timeout_seconds=0.3), PROMPT_SECONDS)
    assert excinfo.value.__notes__ == [OUTPUT_HELD_NOTE]
