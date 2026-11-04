"""The in-container exec wrapper and sibling kill, run under a real shell in a process namespace of their own.

The scripts' ``/proc`` scan must see only the test's own processes, as in the exec's container it
sees only that container's. So the module runs only in a container of its own, with
``SANDBOX_DOCKER_PROCESS_NAMESPACE=1`` set. Each script's ``/tmp`` pidfile path is moved into the
test's own directory, and the wrapper is started as a session leader, as the engine starts every
exec. The container needs ``setsid``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from tai42_kit.sandbox.exec_group_scripts import KILL_NOTHING_RUNNING, KILL_SIGNALLED, KILL_SIGNALLED_OUTPUT_HELD
from tai42_kit.sandbox.exec_status_scripts import STATUS_WAIT_SECONDS

from tai42_sandbox_docker.exec_control import EXEC_WRAPPER, KILL_WRAPPER

pytestmark = [
    pytest.mark.process_namespace,
    pytest.mark.skipif(
        os.environ.get("SANDBOX_DOCKER_PROCESS_NAMESPACE") not in ("1", "true", "True"),
        reason=(
            "SANDBOX_DOCKER_PROCESS_NAMESPACE is not set: the exec scripts' /proc scan must see only the "
            "test's own processes, so these tests run in a process namespace of their own; run "
            "`pytest -m process_namespace` in a `docker run --init` container (CONTRIBUTING.md)"
        ),
    ),
]

_SCRIPT_TIMEOUT = 10


def _host_script(script: str, pidfile_dir: Path) -> str:
    """``script`` with its in-container pidfile path moved into ``pidfile_dir``."""
    return script.replace("/tmp/tai-exec", f"{pidfile_dir}/tai-exec")


async def _run_kill(pidfile_dir: Path, exec_id: str) -> tuple[int, bytes, bytes]:
    """Run the kill of exec ``exec_id``: its exit status, its stdout and its stderr."""
    proc = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        _host_script(KILL_WRAPPER, pidfile_dir),
        exec_id,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), _SCRIPT_TIMEOUT)
    assert proc.returncode is not None
    return proc.returncode, stdout, stderr


def _found(finding: str, *lines: str) -> tuple[int, bytes, bytes]:
    """What a kill that reports ``finding`` after the stdout ``lines`` gives: status 0, those lines, no stderr."""
    return 0, "".join(f"{line}\n" for line in (*lines, finding)).encode(), b""


@contextlib.asynccontextmanager
async def _wrapped(pidfile_dir: Path, exec_id: str, *argv: str) -> AsyncIterator[asyncio.subprocess.Process]:
    """Run ``argv`` under the exec wrapper as a session leader; its group is killed on exit."""
    proc = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        _host_script(EXEC_WRAPPER, pidfile_dir),
        exec_id,
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        yield proc
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()


async def _until_exists(path: Path) -> None:
    while not path.exists():
        await asyncio.sleep(0.01)


async def _until_gone(pid: int) -> None:
    """Wait until ``pid`` has exited: no process, or a zombie awaiting its reaper."""
    stat = Path(f"/proc/{pid}/stat")
    while True:
        try:
            state = stat.read_text().rsplit(")", 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            return
        if state == "Z":
            return
        await asyncio.sleep(0.01)


async def test_a_kill_before_the_wrapper_records_its_pid_keeps_the_argv_from_starting(tmp_path) -> None:
    # The kill comes first: it finds no pidfile, and the wrapper, starting after it, sees the
    # kill's mark and ends with the killed status without ever running its argv. The kill has
    # gone by then, so the mark stays, holding the status the wrapper ended with.
    started = tmp_path / "started"
    assert await _run_kill(tmp_path, "early") == _found(KILL_NOTHING_RUNNING)
    async with _wrapped(tmp_path, "early", "sh", "-c", f"touch {started}; sleep 30") as wrapper:
        assert await asyncio.wait_for(wrapper.wait(), _SCRIPT_TIMEOUT) == 128 + signal.SIGKILL
    assert not started.exists()
    assert [path.name for path in tmp_path.iterdir()] == ["tai-exec.early.kill"]
    assert (tmp_path / "tai-exec.early.kill").read_text() == f"{128 + signal.SIGKILL}\n"


async def test_a_kill_kills_the_commands_whole_process_group_and_its_wrapper_reports_the_killed_status(
    tmp_path,
) -> None:
    # The wrapper is outside the command's process group: it survives the kill, reaps the
    # command and exits with its killed status, which the kill reports, and the wrapper removes
    # its files and the kill its mark.
    child_pidfile = tmp_path / "child"
    argv = ("sh", "-c", f"sleep 30 & echo $! > {child_pidfile}; wait")
    async with _wrapped(tmp_path, "group", *argv) as wrapper:
        await asyncio.wait_for(_until_exists(child_pidfile), _SCRIPT_TIMEOUT)
        assert await _run_kill(tmp_path, "group") == _found(KILL_SIGNALLED, str(128 + signal.SIGKILL))
        assert await asyncio.wait_for(wrapper.wait(), _SCRIPT_TIMEOUT) == 128 + signal.SIGKILL
        await asyncio.wait_for(_until_gone(int(child_pidfile.read_text())), _SCRIPT_TIMEOUT)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["child"]


async def test_a_wrapped_argv_reads_its_stdin_and_reports_its_own_status(tmp_path) -> None:
    proc = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        _host_script(EXEC_WRAPPER, tmp_path),
        "plain",
        "sh",
        "-c",
        "cat; exit 5",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(b"ping"), _SCRIPT_TIMEOUT)
    assert (stdout, proc.returncode) == (b"ping", 5)
    assert list(tmp_path.iterdir()) == []


async def test_a_kill_after_the_wrapper_has_ended_finds_nothing_running(tmp_path) -> None:
    async with _wrapped(tmp_path, "ended", "true") as wrapper:
        assert await asyncio.wait_for(wrapper.wait(), _SCRIPT_TIMEOUT) == 0
    assert await _run_kill(tmp_path, "ended") == _found(KILL_NOTHING_RUNNING)


async def test_a_kill_of_a_recorded_process_that_has_gone_finds_nothing_running(tmp_path) -> None:
    # The pidfile and the mark are the wrapper's to remove: the kill leaves both.
    gone = await asyncio.create_subprocess_exec("true", start_new_session=True)
    await gone.wait()
    (tmp_path / "tai-exec.gone").write_text(f"{gone.pid}\n")
    assert await _run_kill(tmp_path, "gone") == _found(KILL_NOTHING_RUNNING)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["tai-exec.gone", "tai-exec.gone.kill"]


async def test_a_kill_after_the_command_exited_reports_its_own_status_while_an_escaped_process_holds_its_output(
    tmp_path,
) -> None:
    # The command exits 3 at once, leaving a process out of its group that holds the output:
    # the kill reports the command's own status, and the output still held.
    holder = tmp_path / "holder"
    argv = ("sh", "-c", f"setsid sh -c 'echo $$ > {holder}; exec sleep 30' & exit 3")
    async with _wrapped(tmp_path, "escaped", *argv) as wrapper:
        await asyncio.wait_for(_until_exists(holder), _SCRIPT_TIMEOUT)
        while not holder.read_text():
            await asyncio.sleep(0.01)
        try:
            await asyncio.wait_for(_until_gone(int((tmp_path / "tai-exec.escaped").read_text())), _SCRIPT_TIMEOUT)
            assert await _run_kill(tmp_path, "escaped") == _found(KILL_SIGNALLED_OUTPUT_HELD, "3")
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(holder.read_text()), signal.SIGKILL)
        assert await asyncio.wait_for(wrapper.wait(), _SCRIPT_TIMEOUT) == 3


async def test_a_kill_whose_command_status_never_comes_fails_with_one_line(tmp_path) -> None:
    # A recorded group with no wrapper behind it to record the command's status: the kill
    # signals it, waits its bound for the status, and fails, since the status cannot be known.
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


# The shells a session image may carry as ``sh``: each one present on the host is run.
_SHELLS = (("sh",), ("dash",), ("bash",), ("busybox", "sh"))


@pytest.mark.parametrize("shell", _SHELLS, ids=" ".join)
async def test_a_killed_command_leaves_nothing_on_its_stderr(tmp_path, shell: tuple[str, ...]) -> None:
    # A shell that waits on a job ended by a signal reports it on its own stderr ("Killed"):
    # the wrapper's stderr is not the command's, so the killed exec's stderr stays empty.
    if shutil.which(shell[0]) is None:
        pytest.skip(f"{shell[0]} is not on this host")
    started = tmp_path / "started"
    proc = await asyncio.create_subprocess_exec(
        *shell,
        "-c",
        _host_script(EXEC_WRAPPER, tmp_path),
        "quiet",
        "sh",
        "-c",
        f"touch {started}; exec sleep 30",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        await asyncio.wait_for(_until_exists(started), _SCRIPT_TIMEOUT)
        assert await _run_kill(tmp_path, "quiet") == _found(KILL_SIGNALLED, str(128 + signal.SIGKILL))
        stdout, stderr = await asyncio.wait_for(proc.communicate(), _SCRIPT_TIMEOUT)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
    assert (proc.returncode, stdout, stderr) == (128 + signal.SIGKILL, b"", b"")


@pytest.mark.parametrize("shell", _SHELLS, ids=" ".join)
async def test_a_wrapped_command_keeps_its_own_stderr_and_no_wrapper_descriptor(
    tmp_path, shell: tuple[str, ...]
) -> None:
    # The command writes to the exec's own stderr, and holds none of the descriptors the
    # wrapper keeps for itself (3 and 4).
    if shutil.which(shell[0]) is None:
        pytest.skip(f"{shell[0]} is not on this host")
    proc = await asyncio.create_subprocess_exec(
        *shell,
        "-c",
        _host_script(EXEC_WRAPPER, tmp_path),
        "fds",
        "sh",
        "-c",
        'echo oops >&2; for fd in 3 4; do [ -e /proc/$$/fd/$fd ] && echo "open $fd"; done; exit 4',
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), _SCRIPT_TIMEOUT)
    assert (proc.returncode, stdout, stderr) == (4, b"", b"oops\n")


async def test_a_group_kill_racing_the_commands_own_exit_leaves_the_wrapper_its_real_status(tmp_path) -> None:
    # The command has exited 0 and its leader is not yet reaped when the group kill lands, as
    # when the kill's zombie check passes just before the command ends: the kill succeeds on
    # the group the zombie still holds, and the wrapper still exits with the command's real
    # status, which the handle reports.
    started = tmp_path / "started"
    async with _wrapped(tmp_path, "race", "sh", "-c", f"touch {started}; sleep 0.2") as wrapper:
        await asyncio.wait_for(_until_exists(started), _SCRIPT_TIMEOUT)
        leader = int((tmp_path / "tai-exec.race").read_text())
        os.kill(wrapper.pid, signal.SIGSTOP)
        try:
            await asyncio.wait_for(_until_gone(leader), _SCRIPT_TIMEOUT)
            os.killpg(leader, signal.SIGKILL)
        finally:
            os.kill(wrapper.pid, signal.SIGCONT)
        assert await asyncio.wait_for(wrapper.wait(), _SCRIPT_TIMEOUT) == 0


async def test_a_wrapper_whose_command_left_a_process_holding_its_output_ends_with_that_process(tmp_path) -> None:
    # The command exits at once; the process it left holds the output, so the wrapper waits for
    # it, its pidfile still there, and ends with the command's status once that process ends.
    holder = tmp_path / "holder"
    argv = ("sh", "-c", f"sh -c 'echo $$ > {holder}; sleep 0.5' & exit 3")
    async with _wrapped(tmp_path, "held", *argv) as wrapper:
        await asyncio.wait_for(_until_exists(holder), _SCRIPT_TIMEOUT)
        await asyncio.wait_for(_until_gone(int((tmp_path / "tai-exec.held").read_text())), _SCRIPT_TIMEOUT)
        assert (tmp_path / "tai-exec.held").exists()
        assert await asyncio.wait_for(wrapper.wait(), _SCRIPT_TIMEOUT) == 3
    assert sorted(path.name for path in tmp_path.iterdir()) == ["holder"]


async def test_a_kill_after_the_command_exited_kills_the_process_it_left_in_its_group(tmp_path) -> None:
    # The command exits at once, leaving a process in its group that holds the output: the
    # group keeps its id while the wrapper waits, so the kill reaches that process and reports
    # the command's own status, and the wrapper ends with it.
    holder = tmp_path / "holder"
    argv = ("sh", "-c", f"sh -c 'echo $$ > {holder}; exec sleep 30' & exit 3")
    async with _wrapped(tmp_path, "held", *argv) as wrapper:
        await asyncio.wait_for(_until_exists(holder), _SCRIPT_TIMEOUT)
        while not holder.read_text():
            await asyncio.sleep(0.01)
        await asyncio.wait_for(_until_gone(int((tmp_path / "tai-exec.held").read_text())), _SCRIPT_TIMEOUT)
        try:
            assert await _run_kill(tmp_path, "held") == _found(KILL_SIGNALLED, "3")
            await asyncio.wait_for(_until_gone(int(holder.read_text())), _SCRIPT_TIMEOUT)
            assert await asyncio.wait_for(wrapper.wait(), _SCRIPT_TIMEOUT) == 3
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(holder.read_text()), signal.SIGKILL)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["holder"]
