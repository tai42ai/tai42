"""The killed exec's status scripts, run by a shell in a process namespace of their own.

Their files sit in a temporary directory. The kill's ``/proc`` scan must see only the test's own processes,
as inside the exec's container it sees only that container's.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from contextlib import AbstractAsyncContextManager
from pathlib import Path

import pytest
from tai42_contract.sandbox import SandboxError

from tai42_kit.sandbox.exec_group_scripts import (
    KILL_NOTHING_RUNNING,
    KILL_NOTHING_RUNNING_OUTPUT_HELD,
    KILL_SIGNALLED,
    KILL_SIGNALLED_OUTPUT_HELD,
    KILLED_EXIT_CODE,
    report_signalled,
    signal_own_group,
    start_in_own_group,
    wait_output_released,
)
from tai42_kit.sandbox.exec_kill import KillOutcome, KillResult
from tai42_kit.sandbox.exec_status_scripts import (
    STATUS_WAIT_SECONDS,
    end_with_argv_status,
    kill_report,
    record_argv_status,
    report_argv_status,
)

from .process_namespace import PROCESS_NAMESPACE_MARKS
from .wrapped_exec import wrapped_exec

pytestmark = [*PROCESS_NAMESPACE_MARKS, pytest.mark.usefixtures("ls_command")]

_SCRIPT_TIMEOUT = 10


def _wrapper(directory: Path) -> str:
    """A wrapper as a provider builds it: the argv in its own group, its status recorded, then its end."""
    where = str(directory)
    return start_in_own_group(where) + record_argv_status(where) + wait_output_released() + end_with_argv_status(where)


async def _run_kill(directory: Path, exec_id: str) -> tuple[int, bytes, bytes]:
    """Run the kill of exec ``exec_id`` as a provider builds it: its exit status, stdout and stderr."""
    where = str(directory)
    proc = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        signal_own_group(where) + report_argv_status(where) + report_signalled(),
        exec_id,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), _SCRIPT_TIMEOUT)
    assert proc.returncode is not None
    return proc.returncode, stdout, stderr


def _reported(finding: str, status: int) -> tuple[int, bytes, bytes]:
    """What a kill that signalled the group gives: status 0, the argv's status and the finding on its stdout."""
    return 0, f"{status}\n{finding}\n".encode(), b""


def _wrapped(directory: Path, exec_id: str, *argv: str) -> AbstractAsyncContextManager[asyncio.subprocess.Process]:
    """Run ``argv`` under :func:`_wrapper` (:func:`wrapped_exec`)."""
    return wrapped_exec(_wrapper(directory), directory, exec_id, *argv)


async def _until_written(path: Path) -> str:
    """The text a process wrote to ``path``, once it has."""
    while not path.exists() or not path.read_text():
        await asyncio.sleep(0.01)
    return path.read_text()


async def _until_gone(pid: int) -> None:
    """Wait until ``pid`` has exited: no process, or a zombie awaiting its reaper."""
    stat = Path(f"/proc/{pid}/stat")
    while True:
        try:
            if stat.read_text().rsplit(")", 1)[1].split()[0] == "Z":
                return
        except (FileNotFoundError, ProcessLookupError):
            return
        await asyncio.sleep(0.01)


async def test_a_kill_of_a_running_argv_reports_its_killed_status_before_its_finding(tmp_path) -> None:
    started = tmp_path / "started"
    async with _wrapped(tmp_path, "running", "sh", "-c", f"echo $$ > {started}; exec sleep 30") as wrapper:
        await asyncio.wait_for(_until_written(started), _SCRIPT_TIMEOUT)
        assert await _run_kill(tmp_path, "running") == _reported(KILL_SIGNALLED, KILLED_EXIT_CODE)
        assert await asyncio.wait_for(wrapper.wait(), _SCRIPT_TIMEOUT) == KILLED_EXIT_CODE
    # The wrapper removed its pidfile and its recorded status. The kill removed its mark, unless
    # it read the recorded status just as the wrapper was writing the mark: the mark then stays,
    # holding the argv's status.
    mark = tmp_path / "tai-exec.running.kill"
    left = sorted(path.name for path in tmp_path.iterdir())
    assert left == ["started"] or (left == ["started", mark.name] and mark.read_text().strip() == str(KILLED_EXIT_CODE))


@pytest.mark.parametrize(
    ("leave", "finding"),
    [("sh -c", KILL_SIGNALLED), ("setsid sh -c", KILL_SIGNALLED_OUTPUT_HELD)],
    ids=["in-its-group", "out-of-its-group"],
)
async def test_a_kill_after_the_argv_exited_reports_the_status_the_argv_ended_with(
    tmp_path, leave: str, finding: str
) -> None:
    # The argv exits 3 at once, leaving a process that holds the output, in its group or out of
    # it: the wrapper has recorded the argv's status and waits, so the kill reports that status,
    # whether or not a process still holds the output once it is done.
    holder = tmp_path / "holder"
    argv = ("sh", "-c", f"{leave} 'echo $$ > {holder}; exec sleep 30' & exit 3")
    async with _wrapped(tmp_path, "exited", *argv) as wrapper:
        holder_pid = int(await asyncio.wait_for(_until_written(holder), _SCRIPT_TIMEOUT))
        try:
            await asyncio.wait_for(_until_gone(int((tmp_path / "tai-exec.exited").read_text())), _SCRIPT_TIMEOUT)
            assert await _run_kill(tmp_path, "exited") == _reported(finding, 3)
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.kill(holder_pid, signal.SIGKILL)
        assert await asyncio.wait_for(wrapper.wait(), _SCRIPT_TIMEOUT) == 3


async def test_a_kill_reads_the_status_the_wrapper_left_in_its_mark(tmp_path) -> None:
    # A recorded group with no status file: the status comes in the kill's mark instead, as the
    # wrapper writes it there once it has removed the pidfile the kill already read.
    proc = await asyncio.create_subprocess_exec("sleep", "30", start_new_session=True)
    mark = tmp_path / "tai-exec.marked.kill"
    try:
        (tmp_path / "tai-exec.marked").write_text(f"{proc.pid}\n")
        kill = asyncio.ensure_future(_run_kill(tmp_path, "marked"))
        while not mark.exists():
            await asyncio.sleep(0.01)
        (tmp_path / "status").write_text("5\n")
        (tmp_path / "status").rename(mark)
        assert await kill == _reported(KILL_SIGNALLED, 5)
        assert await proc.wait() == -signal.SIGKILL
    finally:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
    assert not mark.exists()


async def test_a_kill_whose_argv_status_never_comes_fails_with_one_line(tmp_path) -> None:
    # A recorded group with no wrapper behind it: the kill signals it, waits its bound for the
    # status, and fails, since the status cannot be known.
    proc = await asyncio.create_subprocess_exec("sleep", "30", start_new_session=True)
    try:
        (tmp_path / "tai-exec.unreported").write_text(f"{proc.pid}\n")
        assert await _run_kill(tmp_path, "unreported") == (
            1,
            b"",
            (
                f"the sandbox exec was killed but its exit status was not reported within {STATUS_WAIT_SECONDS:g}s\n"
            ).encode(),
        )
        assert await proc.wait() == -signal.SIGKILL
    finally:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()


async def test_a_wrapper_writes_its_argvs_status_into_a_mark_a_kill_left(tmp_path) -> None:
    # A kill left its mark once the pid was recorded: the wrapper writes the argv's status into
    # it at its end, and removes its pidfile and its recorded status.
    started = tmp_path / "started"
    mark = tmp_path / "tai-exec.late.kill"
    async with _wrapped(tmp_path, "late", "sh", "-c", f"echo $$ > {started}; sleep 0.3; exit 6") as wrapper:
        await asyncio.wait_for(_until_written(started), _SCRIPT_TIMEOUT)
        mark.touch()
        assert await asyncio.wait_for(wrapper.wait(), _SCRIPT_TIMEOUT) == 6
    assert mark.read_text() == "6\n"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["started", "tai-exec.late.kill"]


@pytest.mark.parametrize(
    ("stdout", "result"),
    [
        (b"137\nsignalled\n", KillResult(KillOutcome.SIGNALLED, exit_code=137)),
        (b"0\nsignalled output-held\n", KillResult(KillOutcome.SIGNALLED, exit_code=0, output_held=True)),
        (f"{KILL_NOTHING_RUNNING}\n".encode(), KillResult(KillOutcome.NOTHING_RUNNING)),
        (f"{KILL_NOTHING_RUNNING_OUTPUT_HELD}\n".encode(), KillResult(KillOutcome.NOTHING_RUNNING, output_held=True)),
    ],
)
def test_a_kill_report_gives_the_finding_with_the_signalled_argvs_status(stdout: bytes, result: KillResult) -> None:
    assert kill_report(stdout) == result


@pytest.mark.parametrize("stdout", [b"", b"stopped\n", b"0\nstopped\n"])
def test_a_kill_report_naming_no_finding_raises(stdout: bytes) -> None:
    with pytest.raises(SandboxError, match="unknown finding"):
        kill_report(stdout)


@pytest.mark.parametrize("stdout", [b"signalled\n", b"0\n1\nsignalled\n", b"x\nsignalled\n", b"\xff\nsignalled\n"])
def test_a_signalled_kill_report_with_no_single_status_line_raises(stdout: bytes) -> None:
    with pytest.raises(SandboxError, match="unreadable exit status"):
        kill_report(stdout)
