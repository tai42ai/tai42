"""The exec group scripts, run by a host shell with their files in a temporary directory."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from tai42_kit.sandbox.exec_group_scripts import (
    KILL_NOTHING_RUNNING,
    KILL_NOTHING_RUNNING_OUTPUT_HELD,
    KILL_SIGNALLED,
    KILL_SIGNALLED_OUTPUT_HELD,
    KILLED_EXIT_CODE,
    kill_finding,
    kill_mark,
    output_identity,
    pidfile,
    report_signalled,
    signal_own_group,
    start_in_own_group,
    wait_output_released,
)
from tai42_kit.sandbox.exec_kill import KillOutcome, KillResult

_SCRIPT_TIMEOUT = 10


def _wrapper(directory: Path) -> str:
    """A wrapper that, once nothing holds its output, removes its pidfile and mark and exits with the argv's status."""
    files = f"{pidfile(str(directory))} {kill_mark(str(directory))}"
    return start_in_own_group(str(directory)) + wait_output_released() + f"rm -f {files}; " + 'exit "$r"'


async def _run_kill(directory: Path, exec_id: str, *, path: str | None = None) -> tuple[int, bytes, bytes]:
    """Run the kill of exec ``exec_id``, its commands found on ``path`` if given: its exit status, stdout and stderr."""
    proc = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        signal_own_group(str(directory)) + report_signalled(),
        exec_id,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=None if path is None else {**os.environ, "PATH": path},
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), _SCRIPT_TIMEOUT)
    assert proc.returncode is not None
    return proc.returncode, stdout, stderr


def _found(finding: str) -> tuple[int, bytes, bytes]:
    """What a kill that reported ``finding`` gives: status 0, its line on its stdout, nothing on its stderr."""
    return 0, finding.encode() + b"\n", b""


@contextlib.asynccontextmanager
async def _wrapped(directory: Path, exec_id: str, *argv: str) -> AsyncIterator[asyncio.subprocess.Process]:
    """Run ``argv`` under :func:`_wrapper` as a session leader, its output on pipes; its group is killed on exit.

    The exit then reads the output to its end, so its pipes are closed once every process
    holding them is gone; a process still holding them fails the test once the bound passes.
    """
    proc = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        _wrapper(directory),
        exec_id,
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        yield proc
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await asyncio.wait_for(proc.communicate(), _SCRIPT_TIMEOUT)


async def _until_exists(path: Path) -> None:
    while not path.exists():
        await asyncio.sleep(0.01)


def _is_dead(pid: int) -> bool:
    """Whether ``pid`` is gone, or a zombie its reaper has not collected yet (seen where ``/proc`` is)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    if not Path("/proc/self/stat").exists():
        return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return True
    # The state follows the last ``)``, since the command name may hold any character.
    return stat.rpartition(")")[2].split()[0] == "Z"


async def _until_dead(pid: int) -> None:
    while not _is_dead(pid):
        await asyncio.sleep(0.01)


def _group_members(pgid: int) -> list[int]:
    """The live processes, zombies aside, whose process group is ``pgid``."""
    members = []
    for stat_path in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat_path.read_text().rpartition(")")[2].split()
        except (FileNotFoundError, ProcessLookupError):
            continue
        if fields[0] != "Z" and int(fields[2]) == pgid:
            members.append(int(stat_path.parent.name))
    return members


async def _until_group_gone(pgid: int) -> None:
    while _group_members(pgid):
        await asyncio.sleep(0.01)


async def _ended_from_outside(wrapper: asyncio.subprocess.Process, directory: Path, exec_id: str) -> None:
    """SIGKILL ``wrapper``, as a stop from outside the exec would, and wait until its group's anchor has ended too."""
    pgid = int((directory / f"tai-exec.{exec_id}").read_text())
    wrapper.kill()
    await asyncio.wait_for(_until_dead(wrapper.pid), _SCRIPT_TIMEOUT)
    await asyncio.wait_for(_until_group_gone(pgid), _SCRIPT_TIMEOUT)


def test_the_files_are_named_by_the_exec_id_in_the_directory_given() -> None:
    assert pidfile("/run/x") == '/run/x/tai-exec."$0"'
    assert kill_mark("/run/x") == '/run/x/tai-exec."$0".kill'


async def test_a_kill_signals_the_whole_process_group_and_the_wrapper_exits_with_the_killed_status(
    tmp_path: Path,
) -> None:
    child_pidfile = tmp_path / "child"
    argv = ("sh", "-c", f"sleep 30 & echo $! > {child_pidfile}; wait")
    async with _wrapped(tmp_path, "group", *argv) as wrapper:
        await asyncio.wait_for(_until_exists(child_pidfile), _SCRIPT_TIMEOUT)
        assert await _run_kill(tmp_path, "group") == _found(KILL_SIGNALLED)
        assert await asyncio.wait_for(wrapper.wait(), _SCRIPT_TIMEOUT) == KILLED_EXIT_CODE
        # The argv's own child is in the group too, so the one kill stopped it as well.
        await asyncio.wait_for(_until_dead(int(child_pidfile.read_text())), _SCRIPT_TIMEOUT)
    # The wrapper removed its pidfile, the mark and, nothing holding the output once the group
    # was gone, its output identity.
    assert sorted(path.name for path in tmp_path.iterdir()) == ["child"]


async def test_a_kill_before_the_pid_is_recorded_keeps_the_argv_from_starting(tmp_path: Path) -> None:
    started = tmp_path / "started"
    assert await _run_kill(tmp_path, "early") == _found(KILL_NOTHING_RUNNING)
    async with _wrapped(tmp_path, "early", "sh", "-c", f"touch {started}; sleep 30") as wrapper:
        assert await asyncio.wait_for(wrapper.wait(), _SCRIPT_TIMEOUT) == KILLED_EXIT_CODE
    assert not started.exists()


async def test_a_wrapped_argv_reads_its_stdin_and_reports_its_own_status(tmp_path: Path) -> None:
    leader = tmp_path / "leader"
    proc = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        _wrapper(tmp_path),
        "plain",
        "sh",
        "-c",
        f"echo $$ > {leader}; cat; echo oops >&2; exit 5",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(b"ping"), _SCRIPT_TIMEOUT)
    assert (stdout, stderr, proc.returncode) == (b"ping", b"oops\n", 5)
    assert list(tmp_path.iterdir()) == [leader]
    # The argv's group, its id the argv's own pid, ends with the wrapper: its anchor is gone too.
    await asyncio.wait_for(_until_group_gone(int(leader.read_text())), _SCRIPT_TIMEOUT)


async def test_a_kill_of_a_recorded_process_that_has_exited_finds_nothing_running(tmp_path: Path) -> None:
    gone = await asyncio.create_subprocess_exec("true", start_new_session=True)
    await gone.wait()
    (tmp_path / "tai-exec.gone").write_text(f"{gone.pid}\n")
    assert await _run_kill(tmp_path, "gone") == _found(KILL_NOTHING_RUNNING)


async def test_a_kill_after_the_wrapper_has_ended_finds_nothing_running(tmp_path: Path) -> None:
    async with _wrapped(tmp_path, "ended", "true") as wrapper:
        assert await asyncio.wait_for(wrapper.wait(), _SCRIPT_TIMEOUT) == 0
    assert await _run_kill(tmp_path, "ended") == _found(KILL_NOTHING_RUNNING)


async def test_a_kill_after_the_wrapper_was_ended_from_outside_finds_nothing_running(tmp_path: Path) -> None:
    # The wrapper is ended before it removes the pidfile: the group's anchor ends with it, so
    # the kill finds no process left in the recorded group.
    started = tmp_path / "started"
    async with _wrapped(tmp_path, "outside", "sh", "-c", f"touch {started}; exit 0") as wrapper:
        await asyncio.wait_for(_until_exists(started), _SCRIPT_TIMEOUT)
        await asyncio.wait_for(_until_exists(tmp_path / "tai-exec.outside"), _SCRIPT_TIMEOUT)
        await _ended_from_outside(wrapper, tmp_path, "outside")
    assert await _run_kill(tmp_path, "outside") == _found(KILL_NOTHING_RUNNING)


async def test_a_kill_whose_output_identity_the_wrapper_removes_as_it_is_read_finds_nothing_running(
    tmp_path: Path,
) -> None:
    # The identity is a FIFO, so the kill's read waits on it; the wrapper's end removes it then,
    # as it does once nothing holds the output, and the read finds no identity left.
    identity = tmp_path / "tai-exec.released.out"
    os.mkfifo(identity)

    def release() -> None:
        fd = os.open(identity, os.O_WRONLY)
        identity.unlink()
        os.close(fd)

    released = asyncio.get_running_loop().run_in_executor(None, release)
    assert await _run_kill(tmp_path, "released") == _found(KILL_NOTHING_RUNNING)
    await asyncio.wait_for(released, _SCRIPT_TIMEOUT)


async def test_a_kill_whose_output_identity_cannot_be_read_fails(tmp_path: Path) -> None:
    identity = tmp_path / "tai-exec.unreadable.out"
    identity.mkdir()
    status, stdout, stderr = await _run_kill(tmp_path, "unreadable")
    assert (status, stdout) == (1, b"")
    assert f"cannot read the sandbox exec output identity {identity}".encode() in stderr


async def test_a_kill_whose_mark_cannot_be_written_fails(tmp_path: Path) -> None:
    status, stdout, stderr = await _run_kill(tmp_path / "missing", "nowhere")
    assert (status, stdout) == (1, b"")
    assert stderr


@contextlib.contextmanager
def _stopped_at_exit(pidfile_path: Path) -> Iterator[None]:
    """Stop the process whose pid ``pidfile_path`` holds once the block ends, should it have started."""
    try:
        yield
    finally:
        if pidfile_path.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pidfile_path.read_text()), signal.SIGKILL)


def _stat_start_time(pid: int) -> str:
    return Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[19]


def test_the_output_identity_is_named_by_the_exec_id_in_the_directory_given() -> None:
    assert output_identity("/run/x") == '/run/x/tai-exec."$0".out'


async def test_the_wrapper_records_its_pid_start_time_and_output_links_before_the_argv_runs(tmp_path: Path) -> None:
    started = tmp_path / "started"
    async with _wrapped(tmp_path, "ident", "sh", "-c", f"touch {started}; sleep 30") as wrapper:
        await asyncio.wait_for(_until_exists(started), _SCRIPT_TIMEOUT)
        lines = (tmp_path / "tai-exec.ident.out").read_text().splitlines()
        assert lines == [
            str(wrapper.pid),
            _stat_start_time(wrapper.pid),
            os.readlink(f"/proc/{wrapper.pid}/fd/1"),
            # The wrapper keeps the exec's stderr on fd 4, its own going to /dev/null.
            os.readlink(f"/proc/{wrapper.pid}/fd/4"),
        ]
        assert lines[2].startswith("pipe:[")
        assert await _run_kill(tmp_path, "ident") == _found(KILL_SIGNALLED)


async def _run_unstarted(directory: Path, exec_id: str, started: Path) -> tuple[int | None, bytes]:
    """Run, under :func:`_wrapper` in ``directory``, an argv creating ``started``: its exit status and its stderr."""
    proc = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        _wrapper(directory),
        exec_id,
        "touch",
        str(started),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), _SCRIPT_TIMEOUT)
    return proc.returncode, stderr


async def test_a_wrapper_that_cannot_record_its_output_identity_never_starts_the_argv(tmp_path: Path) -> None:
    started = tmp_path / "started"
    # A directory where the identity's temporary file goes cannot be written.
    (tmp_path / "tai-exec.unrecorded.out.tmp").mkdir()
    status, stderr = await _run_unstarted(tmp_path, "unrecorded", started)
    assert status == 126
    assert b"the sandbox exec cannot record its output identity" in stderr
    assert not started.exists()


async def test_a_wrapper_that_cannot_open_its_hold_never_starts_the_argv(tmp_path: Path) -> None:
    started = tmp_path / "started"
    status, stderr = await _run_unstarted(tmp_path / "missing", "unheld", started)
    assert status == 126
    assert b"the sandbox exec cannot open its hold" in stderr
    assert not started.exists()


async def test_a_kill_of_a_group_whose_escaped_process_holds_the_output_reports_it_held(tmp_path: Path) -> None:
    holder = tmp_path / "holder"
    argv = ("sh", "-c", f"setsid sleep 30 & echo $! > {holder}; sleep 30")
    # The holder is stopped inside the wrapper's block: the wrapper's end is awaited once its
    # output pipes close, which the holder keeps open.
    async with _wrapped(tmp_path, "held", *argv) as wrapper:
        with _stopped_at_exit(holder):
            await asyncio.wait_for(_until_exists(holder), _SCRIPT_TIMEOUT)
            while not holder.read_text():
                await asyncio.sleep(0.01)
            assert await _run_kill(tmp_path, "held") == _found(KILL_SIGNALLED_OUTPUT_HELD)
            # The escaped process still holds the output, so the wrapper waits, its identity kept.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(wrapper.wait()), 0.3)
            assert (tmp_path / "tai-exec.held.out").exists()
            os.kill(int(holder.read_text()), signal.SIGKILL)
            assert await asyncio.wait_for(wrapper.wait(), _SCRIPT_TIMEOUT) == KILLED_EXIT_CODE
    assert sorted(path.name for path in tmp_path.iterdir()) == ["holder"]


async def test_a_kill_after_the_wrapper_was_ended_from_outside_reports_an_escaped_holder_until_it_ends(
    tmp_path: Path,
) -> None:
    holder = tmp_path / "holder"
    async with _wrapped(tmp_path, "left", "sh", "-c", f"setsid sleep 30 & echo $! > {holder}") as wrapper:
        with _stopped_at_exit(holder):
            await asyncio.wait_for(_until_exists(holder), _SCRIPT_TIMEOUT)
            while not holder.read_text():
                await asyncio.sleep(0.01)
            await _ended_from_outside(wrapper, tmp_path, "left")
            assert await _run_kill(tmp_path, "left") == _found(KILL_NOTHING_RUNNING_OUTPUT_HELD)
            holder_pid = int(holder.read_text())
            os.kill(holder_pid, signal.SIGKILL)
            await asyncio.wait_for(_until_dead(holder_pid), _SCRIPT_TIMEOUT)
            assert await _run_kill(tmp_path, "left") == _found(KILL_NOTHING_RUNNING)
            # Nothing holds the output once that kill reports so, so it removed the identity.
            assert not (tmp_path / "tai-exec.left.out").exists()


async def test_a_kill_waits_for_the_group_it_signalled_to_let_go_of_the_output(tmp_path: Path) -> None:
    # The argv's own child holds the output too; it is in the killed group, so the kill waits
    # for it to die rather than report it.
    child = tmp_path / "child"
    argv = ("sh", "-c", f"sleep 30 & echo $! > {child}; wait")
    async with _wrapped(tmp_path, "grouped", *argv):
        await asyncio.wait_for(_until_exists(child), _SCRIPT_TIMEOUT)
        assert await _run_kill(tmp_path, "grouped") == _found(KILL_SIGNALLED)


async def test_a_kill_fails_on_a_process_whose_open_files_it_cannot_read(tmp_path: Path) -> None:
    # A process that made itself non-dumpable hides its open files from its own user.
    hidden = tmp_path / "hidden"
    hide = (
        "import ctypes, os, time; ctypes.CDLL(None).prctl(4, 0, 0, 0, 0); "
        f"open({str(hidden)!r}, 'w').write(str(os.getpid())); time.sleep(30)"
    )
    argv = ("sh", "-c", f'setsid python3 -c "{hide}" < /dev/null > /dev/null 2>&1 & sleep 30')
    with _stopped_at_exit(hidden):
        async with _wrapped(tmp_path, "hidden", *argv):
            await asyncio.wait_for(_until_exists(hidden), _SCRIPT_TIMEOUT)
            while not hidden.read_text():
                await asyncio.sleep(0.01)
            status, stdout, stderr = await _run_kill(tmp_path, "hidden")
    assert (status, stdout) == (1, b"")
    assert f"cannot read the open files of process {hidden.read_text()}".encode() in stderr


async def test_a_kill_that_fails_on_a_listing_of_many_lines_reports_all_of_it_on_one_line(tmp_path: Path) -> None:
    holder = tmp_path / "holder"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # An ``ls`` whose listing of the holder's open files fails over two lines.
    ls = bin_dir / "ls"
    ls.write_text(
        "#!/bin/sh\n"
        f'if [ "$2" = "/proc/$(cat {holder})/fd" ]; then\n'
        '  echo "ls: first failure" >&2; echo "ls: second failure" >&2; exit 2\n'
        "fi\n"
        'exec /bin/ls "$@"\n'
    )
    ls.chmod(0o755)
    argv = ("sh", "-c", f"setsid sleep 30 & echo $! > {holder}; sleep 30")
    async with _wrapped(tmp_path, "listed", *argv):
        with _stopped_at_exit(holder):
            await asyncio.wait_for(_until_exists(holder), _SCRIPT_TIMEOUT)
            while not holder.read_text():
                await asyncio.sleep(0.01)
            status, stdout, stderr = await _run_kill(tmp_path, "listed", path=f"{bin_dir}:{os.environ['PATH']}")
    assert (status, stdout) == (1, b"")
    assert (
        stderr
        == (
            f"cannot read the open files of process {holder.read_text().strip()}: "
            "ls: first failure; ls: second failure\n"
        ).encode()
    )


async def _run_wrapped(directory: Path, exec_id: str, *argv: str) -> tuple[int, bytes, bytes, float]:
    """Run ``argv`` under the wrapper to its end and its output's: exit status, stdout, stderr and seconds taken."""
    started = time.monotonic()
    async with _wrapped(directory, exec_id, *argv) as wrapper:
        status = await asyncio.wait_for(wrapper.wait(), _SCRIPT_TIMEOUT)
        took = time.monotonic() - started
        assert wrapper.stdout is not None
        assert wrapper.stderr is not None
        stdout, stderr = await asyncio.wait_for(
            asyncio.gather(wrapper.stdout.read(), wrapper.stderr.read()), _SCRIPT_TIMEOUT
        )
    return status, stdout, stderr, took


async def test_a_later_exec_removes_an_identity_whose_holder_has_since_ended(tmp_path: Path) -> None:
    holder = tmp_path / "holder"
    async with _wrapped(tmp_path, "left", "sh", "-c", f"setsid sleep 30 & echo $! > {holder}") as wrapper:
        with _stopped_at_exit(holder):
            await asyncio.wait_for(_until_exists(holder), _SCRIPT_TIMEOUT)
            while not holder.read_text():
                await asyncio.sleep(0.01)
            # The wrapper is ended from outside while the escaped process holds the output, so
            # the identity stays, and a later exec's start keeps it while it is held.
            await _ended_from_outside(wrapper, tmp_path, "left")
            assert (await _run_wrapped(tmp_path, "while-held", "true"))[:3] == (0, b"", b"")
            assert (tmp_path / "tai-exec.left.out").exists()
            holder_pid = int(holder.read_text())
            os.kill(holder_pid, signal.SIGKILL)
            await asyncio.wait_for(_until_dead(holder_pid), _SCRIPT_TIMEOUT)
    # Nothing holds that output any more, and no kill follows: the next exec's start removes it.
    # The pidfile of the wrapper ended from outside stays: no step removes it.
    assert (await _run_wrapped(tmp_path, "later", "true"))[:3] == (0, b"", b"")
    assert sorted(path.name for path in tmp_path.iterdir()) == ["holder", "tai-exec.left"]


async def test_a_later_exec_keeps_the_identity_of_an_exec_still_running(tmp_path: Path) -> None:
    started = tmp_path / "started"
    async with _wrapped(tmp_path, "running", "sh", "-c", f"touch {started}; exec sleep 30"):
        await asyncio.wait_for(_until_exists(started), _SCRIPT_TIMEOUT)
        assert (await _run_wrapped(tmp_path, "beside", "true"))[:3] == (0, b"", b"")
        assert (tmp_path / "tai-exec.running.out").exists()
        assert await _run_kill(tmp_path, "running") == _found(KILL_SIGNALLED)


async def test_a_later_exec_fails_on_an_identity_it_cannot_read(tmp_path: Path) -> None:
    started = tmp_path / "started"
    identity = tmp_path / "tai-exec.unreadable.out"
    identity.mkdir()
    status, _, stderr, _ = await _run_wrapped(tmp_path, "after", "touch", str(started))
    assert status == 126
    assert f"cannot read the sandbox exec output identity {identity}".encode() in stderr
    assert not started.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["tai-exec.unreadable.out"]


async def test_a_wrapper_whose_output_nothing_holds_ends_with_the_argv(tmp_path: Path) -> None:
    status, stdout, _, took = await _run_wrapped(tmp_path, "plain", "sh", "-c", "echo out; exit 4")
    assert (status, stdout) == (4, b"out\n")
    assert took < 1
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("start", ["", "setsid "], ids=["in-group", "escaped"])
async def test_a_wrapper_ends_once_the_process_holding_its_output_ends(tmp_path: Path, start: str) -> None:
    # The argv exits at once, leaving a process that holds its output and writes to it later.
    holder = tmp_path / "holder"
    argv = ("sh", "-c", f"{start}sh -c 'echo $$ > {holder}; sleep 0.6; echo late' & exit 3")
    status, stdout, _, took = await _run_wrapped(tmp_path, "held", *argv)
    assert (status, stdout) == (3, b"late\n")
    assert took >= 0.6
    assert _is_dead(int(holder.read_text()))
    # Nothing holds the output once the wrapper ends, so it removed the identity.
    assert sorted(path.name for path in tmp_path.iterdir()) == ["holder"]


async def test_a_wrapper_waits_while_it_cannot_tell_whether_a_process_holds_its_output(
    tmp_path: Path,
) -> None:
    # The argv leaves a process that hides its open files for longer than a kill's settle: the
    # wrapper cannot tell whether it holds the output, and waits until it has exited.
    hidden = tmp_path / "hidden"
    hide = (
        "import ctypes, os, time; ctypes.CDLL(None).prctl(4, 0, 0, 0, 0); "
        f"open({str(hidden)!r}, 'w').write(str(os.getpid())); time.sleep(3)"
    )
    argv = ("sh", "-c", f'python3 -c "{hide}" < /dev/null > /dev/null 2>&1 & while [ ! -s {hidden} ]; do :; done')
    with _stopped_at_exit(hidden):
        status, _, _, took = await _run_wrapped(tmp_path, "hidden", *argv)
    assert status == 0
    assert took >= 3
    assert sorted(path.name for path in tmp_path.iterdir()) == ["hidden"]


async def test_a_wrapper_whose_output_identity_cannot_be_read_ends_and_keeps_it(tmp_path: Path) -> None:
    # The argv puts a directory where the wrapper's identity was: the wrapper's scan cannot read
    # it, so the wrapper ends without removing it, for a later exec's start to report.
    identity = tmp_path / "tai-exec.swapped.out"
    status, _, _, took = await _run_wrapped(tmp_path, "swapped", "sh", "-c", f"rm {identity} && mkdir {identity}")
    assert status == 0
    assert took < 1
    assert sorted(path.name for path in tmp_path.iterdir()) == ["tai-exec.swapped.out"]


async def test_a_kill_after_the_command_exited_kills_the_process_it_left_in_its_group(tmp_path: Path) -> None:
    # The argv exits at once, leaving a process in its group that holds its output: the group
    # keeps its id while the wrapper waits, so the kill reaches that process.
    child = tmp_path / "child"
    argv = ("sh", "-c", f"sh -c 'echo $$ > {child}; exec sleep 30' & exit 5")
    async with _wrapped(tmp_path, "left", *argv) as wrapper:
        with _stopped_at_exit(child):
            await asyncio.wait_for(_until_exists(child), _SCRIPT_TIMEOUT)
            while not child.read_text():
                await asyncio.sleep(0.01)
            leader = int((tmp_path / "tai-exec.left").read_text())
            await asyncio.wait_for(_until_dead(leader), _SCRIPT_TIMEOUT)
            assert await _run_kill(tmp_path, "left") == _found(KILL_SIGNALLED)
            await asyncio.wait_for(_until_dead(int(child.read_text())), _SCRIPT_TIMEOUT)
            # The argv had ended by itself, so the wrapper exits with the argv's own status.
            assert await asyncio.wait_for(wrapper.wait(), _SCRIPT_TIMEOUT) == 5
    assert sorted(path.name for path in tmp_path.iterdir()) == ["child"]


@pytest.mark.parametrize(
    ("line", "result"),
    [
        (b"signalled", KillResult(KillOutcome.SIGNALLED)),
        (b"signalled output-held", KillResult(KillOutcome.SIGNALLED, output_held=True)),
        (b"nothing-running", KillResult(KillOutcome.NOTHING_RUNNING)),
        (b"nothing-running output-held", KillResult(KillOutcome.NOTHING_RUNNING, output_held=True)),
        (b"signalled extra", None),
        (b"0", None),
        (b"", None),
    ],
)
def test_the_kill_finding_reads_the_kill_line(line: bytes, result: KillResult | None) -> None:
    assert kill_finding(line) == result
