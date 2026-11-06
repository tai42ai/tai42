"""The exec group scripts, run by a shell in a process namespace of their own.

Their files sit in a temporary directory. Their ``/proc`` scan must see only the test's own processes,
as inside the exec's container it sees only that container's.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import socket
import time
from collections.abc import Iterator
from contextlib import AbstractAsyncContextManager
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

from .proc_stat import stat_start_time
from .process_namespace import PROCESS_NAMESPACE_MARKS
from .wrapped_exec import wrapped_exec

pytestmark = [*PROCESS_NAMESPACE_MARKS, pytest.mark.usefixtures("ls_command")]

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


def _wrapped(directory: Path, exec_id: str, *argv: str) -> AbstractAsyncContextManager[asyncio.subprocess.Process]:
    """Run ``argv`` under :func:`_wrapper` (:func:`wrapped_exec`)."""
    return wrapped_exec(_wrapper(directory), directory, exec_id, *argv)


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
    """The processes whose process group is ``pgid``, zombies too: until reaped, a kill of the group finds them."""
    members = []
    for stat_path in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat_path.read_text().rpartition(")")[2].split()
        except (FileNotFoundError, ProcessLookupError):
            continue
        if int(fields[2]) == pgid:
            members.append(int(stat_path.parent.name))
    return members


async def _until_group_gone(pgid: int) -> None:
    while _group_members(pgid):
        await asyncio.sleep(0.01)


async def _ended_from_outside(wrapper: asyncio.subprocess.Process, directory: Path, exec_id: str) -> None:
    """SIGKILL ``wrapper``, as a stop from outside the exec would, and wait until its group is gone, anchor and all."""
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


def _slowed(bin_dir: Path, command: str, before: str, after: str = "") -> None:
    """Put in ``bin_dir`` a ``command`` that runs ``before``, then the ``command`` found past ``bin_dir``.

    ``bin_dir`` must lead ``PATH``. ``after`` is the text between that ``command`` and its arguments.
    """
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / command
    stub.write_text(f'#!/bin/sh\nPATH="${{PATH#{bin_dir}:}}"\n{before}\nexec {command} {after}"$@"\n')
    stub.chmod(0o755)


async def _run_kill_with_its_opens_held(
    directory: Path, exec_id: str, opened: Path, *, before: bool = False
) -> tuple[int, bytes, bytes]:
    """Run the kill of exec ``exec_id`` under ``strace``, each open of ``opened`` held 1 s past its result.

    With ``before``, each open is held 1 s before it is made instead. No shell command runs
    between the kill's look for a file, its open of it and what it decides from that open, so it
    is ``strace`` that holds the window. The test is skipped where ``strace`` is not.
    """
    strace = shutil.which("strace")
    if strace is None:
        pytest.skip("strace is not on this host")
    proc = await asyncio.create_subprocess_exec(
        strace,
        *("-f", "-qq", "-o", "/dev/null", "-P", str(opened)),
        *("-e", "trace=openat", "-e", f"inject=openat:delay_{'enter' if before else 'exit'}=1000000"),
        *("sh", "-c", signal_own_group(str(directory)) + report_signalled(), exec_id),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), _SCRIPT_TIMEOUT)
    assert proc.returncode is not None
    return proc.returncode, stdout, stderr


async def test_a_kill_while_the_pid_is_recorded_finds_nothing_running_and_keeps_the_argv_from_starting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The ``mv`` that records the starter's pid takes 1 s, so the kill finds no pidfile; the
    # starter then sees the kill's mark and never starts the argv, holding the exec's output
    # until it exits, as one the kill expects to end. Its ``setsid`` would have it wait 0.5 s
    # more once it leads its group, which the kill knows nothing of.
    started = tmp_path / "started"
    recording = tmp_path / "recording"
    bin_dir = tmp_path / "bin"
    _slowed(bin_dir, "mv", f'[ "$1" = {tmp_path}/tai-exec.recorded.tmp ] && {{ touch {recording}; sleep 1; }}')
    _slowed(bin_dir, "setsid", "", after="sh -c 'sleep 0.5; exec \"$@\"' sh ")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    async with _wrapped(tmp_path, "recorded", "touch", str(started)) as wrapper:
        await asyncio.wait_for(_until_exists(recording), _SCRIPT_TIMEOUT)
        assert await _run_kill(tmp_path, "recorded") == _found(KILL_NOTHING_RUNNING)
        assert await asyncio.wait_for(wrapper.wait(), _SCRIPT_TIMEOUT) == KILLED_EXIT_CODE
    assert not started.exists()


async def test_a_kill_that_reads_the_pid_before_its_group_is_led_finds_nothing_running_and_keeps_the_argv_from_starting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The starter's ``setsid`` waits 0.5 s before it makes the starter lead its group, and the
    # starter then waits 0.5 s more, leading it: the kill reads the pid while the group has no
    # member, and the starter, which then sees the kill's mark, holds the exec's output in that
    # group until it exits, as one the kill expects to end.
    started = tmp_path / "started"
    leading = tmp_path / "leading"
    bin_dir = tmp_path / "bin"
    _slowed(bin_dir, "setsid", f"touch {leading}; sleep 0.5", after="sh -c 'sleep 0.5; exec \"$@\"' sh ")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    async with _wrapped(tmp_path, "leading", "touch", str(started)) as wrapper:
        await asyncio.wait_for(_until_exists(leading), _SCRIPT_TIMEOUT)
        assert await _run_kill(tmp_path, "leading") == _found(KILL_NOTHING_RUNNING)
        assert await asyncio.wait_for(wrapper.wait(), _SCRIPT_TIMEOUT) == KILLED_EXIT_CODE
    assert not started.exists()


async def test_a_kill_that_finds_no_identity_finds_nothing_running_though_it_is_recorded_right_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The wrapper's ``mv`` that records its output identity waits until the kill has left its
    # mark, then 0.3 s more, so the kill finds no pidfile and finds no identity either: it finds
    # nothing running though the ``mv`` records the identity right after. ``strace`` holds each
    # open of the identity 1 s past its result, so a kill that opened it to find it there would
    # see that open fail and the identity there once it returns. The starter's own ``mv`` takes
    # 2 s, so the identity is still there then; the starter then sees the mark and never starts
    # the argv.
    started = tmp_path / "started"
    recording = tmp_path / "recording"
    identity = tmp_path / "tai-exec.appearing.out"
    bin_dir = tmp_path / "bin"
    _slowed(
        bin_dir,
        "mv",
        f'case "$1" in {identity}.tmp) touch {recording}; '
        f"while [ ! -e {tmp_path}/tai-exec.appearing.kill ]; do sleep 0.01; done; sleep 0.3;; "
        f"{tmp_path}/tai-exec.appearing.tmp) sleep 2;; esac",
    )
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    async with _wrapped(tmp_path, "appearing", "touch", str(started)) as wrapper:
        await asyncio.wait_for(_until_exists(recording), _SCRIPT_TIMEOUT)
        result = await _run_kill_with_its_opens_held(tmp_path, "appearing", identity)
        assert result == _found(KILL_NOTHING_RUNNING)
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
    # the kill finds no process left in the recorded group. The argv ends only once the wrapper
    # is stopped, so the wrapper cannot reach its end, which removes the pidfile, before it is
    # ended from outside.
    started = tmp_path / "started"
    go = tmp_path / "go"
    argv = ("sh", "-c", f"touch {started}; while [ ! -e {go} ]; do sleep 0.01; done; exit 0")
    async with _wrapped(tmp_path, "outside", *argv) as wrapper:
        await asyncio.wait_for(_until_exists(started), _SCRIPT_TIMEOUT)
        leader = int((tmp_path / "tai-exec.outside").read_text())
        wrapper.send_signal(signal.SIGSTOP)
        go.touch()
        await asyncio.wait_for(_until_dead(leader), _SCRIPT_TIMEOUT)
        await _ended_from_outside(wrapper, tmp_path, "outside")
    assert await _run_kill(tmp_path, "outside") == _found(KILL_NOTHING_RUNNING)


async def test_a_kill_reads_an_identity_the_wrapper_removes_once_it_is_opened_as_it_was(tmp_path: Path) -> None:
    # The identity names the output of a process the kill does not expect to end, which holds it;
    # it is removed once the kill has opened it, ``strace`` holding that open 1 s past its result,
    # and the kill reads it as it was from what it opened.
    identity = tmp_path / "tai-exec.released.out"
    holder = await asyncio.create_subprocess_exec("sleep", "30", stdout=asyncio.subprocess.PIPE)
    try:
        link = os.readlink(f"/proc/{holder.pid}/fd/1")
        gone = await asyncio.create_subprocess_exec("true")
        await gone.wait()
        identity.write_text(f"{gone.pid}\n0\n{link}\n{link}\n")
        kill = asyncio.ensure_future(_run_kill_with_its_opens_held(tmp_path, "released", identity))
        await asyncio.sleep(0.3)
        identity.unlink()
        assert await kill == _found(KILL_NOTHING_RUNNING_OUTPUT_HELD)
    finally:
        holder.kill()
        await holder.wait()


async def test_a_kill_whose_identity_the_wrapper_removes_as_it_is_opened_finds_nothing_running(
    tmp_path: Path,
) -> None:
    # The identity is there when the kill looks for it, and removed before its open, which
    # ``strace`` holds 1 s before it is made, as the wrapper's end removes it once nothing holds
    # the output: the open finds no identity, and nothing holds the output.
    identity = tmp_path / "tai-exec.released.out"
    identity.write_text("1\n0\npipe:[0]\npipe:[0]\n")
    kill = asyncio.ensure_future(_run_kill_with_its_opens_held(tmp_path, "released", identity, before=True))
    await asyncio.sleep(0.3)
    identity.unlink()
    assert await kill == _found(KILL_NOTHING_RUNNING)


async def test_a_kill_whose_output_identity_cannot_be_read_fails(tmp_path: Path) -> None:
    identity = tmp_path / "tai-exec.unreadable.out"
    identity.mkdir()
    status, stdout, stderr = await _run_kill(tmp_path, "unreadable")
    assert (status, stdout) == (1, b"")
    assert f"cannot read the sandbox exec output identity {identity}".encode() in stderr


async def test_a_kill_whose_pidfile_cannot_be_read_fails(tmp_path: Path) -> None:
    recorded = tmp_path / "tai-exec.unreadable"
    recorded.mkdir()
    status, stdout, stderr = await _run_kill(tmp_path, "unreadable")
    assert (status, stdout) == (1, b"")
    assert stderr.startswith(f"cannot read the sandbox exec pidfile {recorded}: ".encode())
    assert stderr.count(b"\n") == 1
    assert stderr.endswith(b"\n")


async def test_a_kill_whose_mark_cannot_be_written_fails(tmp_path: Path) -> None:
    status, stdout, stderr = await _run_kill(tmp_path / "missing", "nowhere")
    assert (status, stdout) == (1, b"")
    assert stderr


def _escaped(pid_path: Path) -> str:
    """A shell command that starts, in the background, a ``sleep 30`` in a session of its own, holding the output.

    The process writes its own pid to ``pid_path`` once it has left the argv's group, so a kill
    of that group, run once the pid is written, no longer reaches it.
    """
    return f"setsid sh -c 'echo $$ > {pid_path}; exec sleep 30' &"


@contextlib.contextmanager
def _stopped_at_exit(pidfile_path: Path) -> Iterator[None]:
    """Stop the process whose pid ``pidfile_path`` holds once the block ends, should it have started."""
    try:
        yield
    finally:
        if pidfile_path.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pidfile_path.read_text()), signal.SIGKILL)


def test_the_output_identity_is_named_by_the_exec_id_in_the_directory_given() -> None:
    assert output_identity("/run/x") == '/run/x/tai-exec."$0".out'


async def test_the_wrapper_records_its_pid_start_time_and_output_links_before_the_argv_runs(tmp_path: Path) -> None:
    started = tmp_path / "started"
    async with _wrapped(tmp_path, "ident", "sh", "-c", f"touch {started}; sleep 30") as wrapper:
        await asyncio.wait_for(_until_exists(started), _SCRIPT_TIMEOUT)
        lines = (tmp_path / "tai-exec.ident.out").read_text().splitlines()
        assert lines == [
            str(wrapper.pid),
            str(stat_start_time(wrapper.pid)),
            os.readlink(f"/proc/{wrapper.pid}/fd/1"),
            # The wrapper keeps the exec's stderr on fd 4, its own going to /dev/null.
            os.readlink(f"/proc/{wrapper.pid}/fd/4"),
        ]
        assert lines[2].startswith("pipe:[")
        assert await _run_kill(tmp_path, "ident") == _found(KILL_SIGNALLED)


async def _run_unstarted(
    directory: Path, exec_id: str, started: Path, *, session_leader: bool = True
) -> tuple[int | None, bytes]:
    """Run, under :func:`_wrapper` in ``directory``, an argv creating ``started``: its exit status and its stderr.

    The wrapper is a session leader unless ``session_leader`` is false, when it shares the test's session.
    """
    proc = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        _wrapper(directory),
        exec_id,
        "touch",
        str(started),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=session_leader,
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


async def test_a_wrapper_that_leads_no_session_never_starts_the_argv_nor_scans_the_identities_left(
    tmp_path: Path,
) -> None:
    started = tmp_path / "started"
    # An identity an ended wrapper left, its output held by none: the start of a wrapper that
    # goes on scans it and removes it.
    gone = await asyncio.create_subprocess_exec("true", start_new_session=True)
    await gone.wait()
    left = tmp_path / "tai-exec.left.out"
    left.write_text(f"{gone.pid}\n0\npipe:[1]\npipe:[1]\n")
    status, stderr = await _run_unstarted(tmp_path, "unled", started, session_leader=False)
    assert (status, stderr) == (126, b"the sandbox exec's wrapper is not a session leader\n")
    assert sorted(tmp_path.iterdir()) == [left]


async def test_a_wrapper_that_cannot_read_its_own_stat_never_starts_the_argv_and_says_why_on_one_line(
    tmp_path: Path,
) -> None:
    # A subshell runs the wrapper once its shell has exited and been reaped: ``$$`` there still
    # names that shell, whose ``/proc/<pid>/stat`` is gone.
    started = tmp_path / "started"
    status_path = tmp_path / "status"
    proc = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        f'( ( while [ -e /proc/$$ ]; do sleep 0.01; done; eval "$WRAPPER" ); echo "$?" > {status_path} ) & exit 0',
        "unread",
        "touch",
        str(started),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "WRAPPER": _wrapper(tmp_path)},
        start_new_session=True,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), _SCRIPT_TIMEOUT)
    reason = b"the sandbox exec cannot tell whether its wrapper is a session leader: "
    assert (status_path.read_text(), stderr.startswith(reason), stderr.count(b"\n")) == ("126\n", True, 1)
    assert not started.exists()


async def test_a_wrapper_that_cannot_open_its_hold_never_starts_the_argv(tmp_path: Path) -> None:
    started = tmp_path / "started"
    status, stderr = await _run_unstarted(tmp_path / "missing", "unheld", started)
    assert status == 126
    assert b"the sandbox exec cannot open its hold" in stderr
    assert not started.exists()


async def test_a_kill_of_a_group_whose_escaped_process_holds_the_output_reports_it_held(tmp_path: Path) -> None:
    holder = tmp_path / "holder"
    argv = ("sh", "-c", f"{_escaped(holder)} sleep 30")
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
    async with _wrapped(tmp_path, "left", "sh", "-c", _escaped(holder)) as wrapper:
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


async def test_a_kill_finds_nothing_holding_the_output_whose_identity_is_gone_when_it_opens_it(
    tmp_path: Path,
) -> None:
    started = tmp_path / "started"
    async with _wrapped(tmp_path, "removed", "sh", "-c", f"touch {started}; sleep 30"):
        await asyncio.wait_for(_until_exists(started), _SCRIPT_TIMEOUT)
        (tmp_path / "tai-exec.removed.out").unlink()
        result = await _run_kill(tmp_path, "removed")
    assert result == _found(KILL_SIGNALLED)


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


@pytest.mark.parametrize("depth", [1, 2, 3], ids=["a-child", "a-grandchild", "a-great-grandchild"])
async def test_a_kill_expects_every_process_of_the_wrappers_wait_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, depth: int
) -> None:
    # The wrapper's ``ls`` takes 3 s more, longer than the kill waits, so the processes its wait
    # runs a listing in live through the kill, and it cannot list the open files of the passerby,
    # which keeps that wait scanning; the kill's ``ls`` cannot list the open files of a process
    # ``depth`` generations below the wrapper. Those end with the wait, so the kill finds the
    # output held, the wrapper still holding it, and names none of them.
    passerby = tmp_path / "passerby"
    wrapper_bin = tmp_path / "wrapper-bin"
    kill_bin = tmp_path / "kill-bin"
    wrapper_bin.mkdir()
    kill_bin.mkdir()
    wrapper_ls = wrapper_bin / "ls"
    wrapper_ls.write_text(
        "#!/bin/sh\n"
        f'PATH="${{PATH#{wrapper_bin}:}}"; sleep 3\n'
        f'[ "$2" = "/proc/$(cat {passerby})/fd" ] && {{ echo "ls: the passerby" >&2; exit 1; }}\n'
        'exec ls "$@"\n'
    )
    wrapper_ls.chmod(0o755)
    kill_ls = kill_bin / "ls"
    kill_path = os.environ["PATH"]
    monkeypatch.setenv("PATH", f"{wrapper_bin}:{kill_path}")
    tmp = f"{passerby}.tmp"
    argv = (
        "sh",
        "-c",
        f"setsid sh -c 'echo $$ > {tmp} && mv {tmp} {passerby}; exec sleep 30' < /dev/null > /dev/null 2>&1 & sleep 30",
    )
    with _stopped_at_exit(passerby):
        async with _wrapped(tmp_path, "waiting", *argv) as wrapper:
            kill_ls.write_text(
                "#!/bin/sh\n"
                'p="${2%/fd}"; n=0\n'
                f'while [ "$p" != "$2" ] && [ "$n" -lt {depth} ] && read -r s 2>/dev/null < "$p/stat"; do\n'
                '  s="${s##*) }"; s="${s#* }"; p="/proc/${s%% *}"; n=$((n + 1))\n'
                "done\n"
                f'[ "$n" = {depth} ] && [ "$p" = /proc/{wrapper.pid} ] && '
                '{ echo "ls: a process below the wrapper" >&2; exit 1; }\n'
                f'PATH="${{PATH#{kill_bin}:}}"; exec ls "$@"\n'
            )
            kill_ls.chmod(0o755)
            await asyncio.wait_for(_until_exists(passerby), _SCRIPT_TIMEOUT)
            result = await _run_kill(tmp_path, "waiting", path=f"{kill_bin}:{kill_path}")
    assert result == _found(KILL_SIGNALLED_OUTPUT_HELD)


async def test_a_kill_expects_a_process_of_the_wrappers_session_in_a_group_of_its_own_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The wrapper's ``readlink`` starts, once, a process in the wrapper's session that leads a
    # group of its own and holds nothing of the exec's, and the wrapper's ``ls`` cannot list the
    # open files of the passerby, which keeps its wait scanning. The kill's ``ls`` cannot list
    # the open files of the process in a group of its own; it is in the wrapper's session, so the
    # kill expects it to end, finds the output held, the wrapper still holding it, and names none.
    passerby = tmp_path / "passerby"
    grouped = tmp_path / "grouped"
    wrapper_bin = tmp_path / "wrapper-bin"
    kill_bin = tmp_path / "kill-bin"
    wrapper_bin.mkdir()
    kill_bin.mkdir()
    wrapper_ls = wrapper_bin / "ls"
    wrapper_ls.write_text(
        "#!/bin/sh\n"
        f'PATH="${{PATH#{wrapper_bin}:}}"\n'
        f'[ "$2" = "/proc/$(cat {passerby})/fd" ] && {{ echo "ls: the passerby" >&2; exit 1; }}\n'
        'exec ls "$@"\n'
    )
    wrapper_ls.chmod(0o755)
    group_of_its_own = (
        f"import os, time; os.setpgid(0, 0); open({f'{grouped}.tmp'!r}, 'w').write(str(os.getpid())); "
        f"os.rename({f'{grouped}.tmp'!r}, {str(grouped)!r}); time.sleep(30)"
    )
    wrapper_readlink = wrapper_bin / "readlink"
    wrapper_readlink.write_text(
        "#!/bin/sh\n"
        f'PATH="${{PATH#{wrapper_bin}:}}"\n'
        f"[ -e {grouped}.started ] || {{ : > {grouped}.started; "
        f'python3 -c "{group_of_its_own}" < /dev/null > /dev/null 2>&1 8>&- 9>&- & }}\n'
        'exec readlink "$@"\n'
    )
    wrapper_readlink.chmod(0o755)
    kill_ls = kill_bin / "ls"
    kill_path = os.environ["PATH"]
    kill_ls.write_text(
        "#!/bin/sh\n"
        f'[ "$2" = "/proc/$(cat {grouped})/fd" ] && {{ echo "ls: the process in a group of its own" >&2; exit 1; }}\n'
        f'PATH="${{PATH#{kill_bin}:}}"; exec ls "$@"\n'
    )
    kill_ls.chmod(0o755)
    monkeypatch.setenv("PATH", f"{wrapper_bin}:{kill_path}")
    tmp = f"{passerby}.tmp"
    argv = (
        "sh",
        "-c",
        f"setsid sh -c 'echo $$ > {tmp} && mv {tmp} {passerby}; exec sleep 30' < /dev/null > /dev/null 2>&1 & sleep 30",
    )
    with _stopped_at_exit(passerby), _stopped_at_exit(grouped):
        async with _wrapped(tmp_path, "grouped", *argv) as wrapper:
            await asyncio.wait_for(_until_exists(passerby), _SCRIPT_TIMEOUT)
            await asyncio.wait_for(_until_exists(grouped), _SCRIPT_TIMEOUT)
            pid = int(grouped.read_text())
            assert (os.getpgid(pid), os.getsid(pid)) == (pid, wrapper.pid)
            result = await _run_kill(tmp_path, "grouped", path=f"{kill_bin}:{kill_path}")
    assert result == _found(KILL_SIGNALLED_OUTPUT_HELD)


@pytest.mark.parametrize("depth", [1, 2], ids=["a-child", "a-grandchild"])
async def test_a_kill_expects_the_processes_of_the_wait_of_a_wrapper_ended_from_outside_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, depth: int
) -> None:
    # The wrapper's ``ls`` takes 5 s more while the passerby runs, longer than the kill waits,
    # and cannot list the passerby's open files, which keeps its wait scanning. The kill's ``ls``,
    # first asked for the open files of a process of the wait ``depth`` generations below the
    # wrapper, ends the wrapper from outside, which leaves the wait's processes to the init
    # process, and then fails for that process each time. It ends with the wait, so the kill
    # finds the output held by a process it expects to end, and names none.
    passerby = tmp_path / "passerby"
    marked = tmp_path / "marked"
    wrapper_bin = tmp_path / "wrapper-bin"
    kill_bin = tmp_path / "kill-bin"
    wrapper_bin.mkdir()
    kill_bin.mkdir()
    wrapper_ls = wrapper_bin / "ls"
    wrapper_ls.write_text(
        "#!/bin/sh\n"
        f'PATH="${{PATH#{wrapper_bin}:}}"; [ -e "/proc/$(cat {passerby})" ] && sleep 5\n'
        f'[ "$2" = "/proc/$(cat {passerby})/fd" ] && {{ echo "ls: the passerby" >&2; exit 1; }}\n'
        'exec ls "$@"\n'
    )
    wrapper_ls.chmod(0o755)
    kill_ls = kill_bin / "ls"
    kill_path = os.environ["PATH"]
    monkeypatch.setenv("PATH", f"{wrapper_bin}:{kill_path}")
    tmp = f"{passerby}.tmp"
    argv = (
        "sh",
        "-c",
        f"setsid sh -c 'echo $$ > {tmp} && mv {tmp} {passerby}; exec sleep 30' < /dev/null > /dev/null 2>&1 & sleep 30",
    )
    with _stopped_at_exit(passerby):
        async with _wrapped(tmp_path, "orphaned", *argv) as wrapper:
            kill_ls.write_text(
                "#!/bin/sh\n"
                'v="${2#/proc/}"; v="${v%/fd}"\n'
                f'[ -e {marked} ] && [ "$v" = "$(cat {marked})" ] && '
                '{ echo "ls: a process of the wait" >&2; exit 1; }\n'
                'p="/proc/$v"; n=0\n'
                f'[ -e {marked} ] || while [ "$n" -lt {depth} ] && read -r s 2>/dev/null < "$p/stat"; do\n'
                '  s="${s##*) }"; s="${s#* }"; p="/proc/${s%% *}"; n=$((n + 1))\n'
                "done\n"
                f'if [ "$n" = {depth} ] && [ "$p" = /proc/{wrapper.pid} ] && read -r s < "/proc/$v/stat"; then\n'
                '  s="${s##*) }"; s="${s#* * * * }"; [ "${s%% *}" != "$v" ] && {\n'
                f'    echo "$v" > {marked}.tmp && mv {marked}.tmp {marked}; kill -KILL {wrapper.pid}\n'
                f'    while read -r s < /proc/{wrapper.pid}/stat && s="${{s##*) }}" && [ "${{s%% *}}" != Z ]; do\n'
                "      sleep 0.01; done\n"
                '    echo "ls: a process of the wait" >&2; exit 1; }\n'
                "fi\n"
                f'PATH="${{PATH#{kill_bin}:}}"; exec ls "$@"\n'
            )
            kill_ls.chmod(0o755)
            await asyncio.wait_for(_until_exists(passerby), _SCRIPT_TIMEOUT)
            result = await _run_kill(tmp_path, "orphaned", path=f"{kill_bin}:{kill_path}")
    assert (result, marked.exists()) == (_found(KILL_SIGNALLED_OUTPUT_HELD), True)


# A failure's text as ``ls`` writes it, each line in single quotes, and as the kill reports it:
# over two lines, and with a ``\c``, which an ``echo`` of it would end the line at in dash.
_LISTING_FAILURES = {
    "two-lines": ("'ls: first failure' 'ls: second failure'", "ls: first failure; ls: second failure"),
    "backslash-c": (r"'ls: cannot read /tmp/a\cb'", r"ls: cannot read /tmp/a\cb"),
}


@pytest.mark.parametrize("failure", _LISTING_FAILURES.values(), ids=_LISTING_FAILURES.keys())
async def test_a_kill_that_fails_on_a_listing_reports_all_of_its_text_on_one_line(
    tmp_path: Path, failure: tuple[str, str]
) -> None:
    written, reported = failure
    holder = tmp_path / "holder"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # An ``ls`` whose listing of the holder's open files fails with that text.
    ls = bin_dir / "ls"
    ls.write_text(
        "#!/bin/sh\n"
        f'case "$2" in "/proc/$(cat {holder})/fd")\n'
        f"  printf '%s\\n' {written} >&2; exit 2;;\n"
        "esac\n"
        f'PATH="${{PATH#{bin_dir}:}}"; exec ls "$@"\n'
    )
    ls.chmod(0o755)
    argv = ("sh", "-c", f"{_escaped(holder)} sleep 30")
    async with _wrapped(tmp_path, "listed", *argv):
        with _stopped_at_exit(holder):
            await asyncio.wait_for(_until_exists(holder), _SCRIPT_TIMEOUT)
            while not holder.read_text():
                await asyncio.sleep(0.01)
            status, stdout, stderr = await _run_kill(tmp_path, "listed", path=f"{bin_dir}:{os.environ['PATH']}")
    assert (status, stdout) == (1, b"")
    assert stderr == (f"cannot read the open files of process {holder.read_text().strip()}: {reported}\n").encode()


def _passerby(pid_path: Path) -> str:
    """A shell command that leaves a ``sleep 30`` holding none of its output, its pid written to ``pid_path``.

    The process is started from a subshell that exits at once, so the init process reaps it once
    it ends; its pid is written ATOMICALLY (write-then-``mv``).
    """
    tmp = f"{pid_path}.tmp"
    return f"(sh -c 'echo $$ > {tmp} && mv {tmp} {pid_path}; exec sleep 30' < /dev/null > /dev/null 2>&1 &)"


# Python that starts two copies of itself (``fork``), writes its own pid ATOMICALLY
# (write-then-rename) to the path it is given, and then, in each of the three processes, opens 50
# files and closes them over and over, so a listing of their open files meets files closed
# between the listing's read of their ``fd`` directory and its read of their links.
_CHURN = (
    "import os, sys\n"
    "if os.fork() and os.fork():\n"
    "    with open(sys.argv[1] + '.tmp', 'w') as f:\n"
    "        f.write(str(os.getpid()))\n"
    "    os.rename(sys.argv[1] + '.tmp', sys.argv[1])\n"
    "while True:\n"
    "    fds = [os.open('/dev/null', os.O_RDONLY) for _ in range(50)]\n"
    "    for fd in fds:\n"
    "        os.close(fd)\n"
)


async def test_a_kill_reads_processes_that_close_files_as_they_are_listed(tmp_path: Path) -> None:
    # The argv leaves, in a session and a group of their own, three processes that hold none of
    # its output and close files as the kill's scans list them: each scan reads their open files.
    # They are stopped by their group, its id the pid the first one writes.
    churner = tmp_path / "churner"
    code = tmp_path / "churn.py"
    code.write_text(_CHURN)
    argv = ("sh", "-c", f"(setsid python3 {code} {churner} < /dev/null > /dev/null 2>&1 &); sleep 30")
    try:
        async with _wrapped(tmp_path, "churned", *argv) as wrapper:
            await asyncio.wait_for(_until_exists(churner), _SCRIPT_TIMEOUT)
            assert await _run_kill(tmp_path, "churned") == _found(KILL_SIGNALLED)
            assert await asyncio.wait_for(wrapper.wait(), _SCRIPT_TIMEOUT) == KILLED_EXIT_CODE
    finally:
        if churner.exists():
            with contextlib.suppress(ProcessLookupError):
                os.killpg(int(churner.read_text()), signal.SIGKILL)


async def test_a_kill_skips_a_process_that_ends_while_its_open_files_are_listed(tmp_path: Path) -> None:
    passerby = tmp_path / "passerby"
    ended = tmp_path / "ended"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # An ``ls`` that, asked for the passerby's open files, ends it, waits until it is reaped and
    # a next one has started, then lists the ended one's: every scan of the kill meets a process
    # that ended between the scan finding it and the listing of its open files. It writes each
    # one it ended to ``ended``, so the test knows the scan met one.
    ls = bin_dir / "ls"
    ls.write_text(
        "#!/bin/sh\n"
        f'v="$(cat {passerby})"\n'
        'case "$2" in "/proc/$v/fd")\n'
        '  kill -KILL "$v"; while [ -e "/proc/$v" ]; do sleep 0.01; done\n'
        f'  echo "$v" >> {ended}\n'
        f"  {_passerby(passerby)}\n"
        f'  while [ "$(cat {passerby})" = "$v" ]; do sleep 0.01; done;;\n'
        "esac\n"
        f'PATH="${{PATH#{bin_dir}:}}"; exec ls "$@"\n'
    )
    ls.chmod(0o755)
    async with _wrapped(tmp_path, "passed", "sleep", "30") as wrapper:
        with _stopped_at_exit(passerby):
            await asyncio.wait_for(_until_exists(tmp_path / "tai-exec.passed"), _SCRIPT_TIMEOUT)
            started = await asyncio.create_subprocess_exec("sh", "-c", _passerby(passerby))
            await asyncio.wait_for(started.wait(), _SCRIPT_TIMEOUT)
            await asyncio.wait_for(_until_exists(passerby), _SCRIPT_TIMEOUT)
            # The wrapper is stopped, so it still holds the output once the kill's scan has
            # settled; the ended processes the scan met are no holders it cannot read.
            wrapper.send_signal(signal.SIGSTOP)
            result = await _run_kill(tmp_path, "passed", path=f"{bin_dir}:{os.environ['PATH']}")
    assert result == _found(KILL_SIGNALLED_OUTPUT_HELD)
    assert ended.read_text()


def _leaderless(pid_path: Path, thread_body: str) -> str:
    """A ``python3`` command whose main thread ends (``pthread_exit``) while a thread it started runs ``thread_body``.

    It writes its pid to ``pid_path`` once that thread runs: the process then shows as a zombie,
    while the thread lives and holds the command's output.
    """
    code = (
        "import ctypes, os, threading, time; "
        f"threading.Thread(target=lambda: {thread_body}).start(); "
        f"open({str(pid_path)!r}, 'w').write(str(os.getpid())); ctypes.CDLL(None).pthread_exit(None)"
    )
    return f'python3 -c "{code}"'


async def _until_leader_ended(pid: int) -> None:
    """Wait until the leading thread of process ``pid`` is a zombie while another of its threads lives."""
    while True:
        states = [
            (task / "stat").read_text().rpartition(")")[2].split()[0] for task in Path(f"/proc/{pid}/task").iterdir()
        ]
        if _is_dead(pid) and any(state != "Z" for state in states):
            return
        await asyncio.sleep(0.01)


async def test_a_kill_reports_a_process_whose_leading_thread_ended_while_another_holds_the_output(
    tmp_path: Path,
) -> None:
    holder = tmp_path / "holder"
    argv = ("sh", "-c", f"setsid {_leaderless(holder, 'time.sleep(30)')} & sleep 30")
    async with _wrapped(tmp_path, "leaderless", *argv):
        with _stopped_at_exit(holder):
            await asyncio.wait_for(_until_exists(holder), _SCRIPT_TIMEOUT)
            while not holder.read_text():
                await asyncio.sleep(0.01)
            await asyncio.wait_for(_until_leader_ended(int(holder.read_text())), _SCRIPT_TIMEOUT)
            assert await _run_kill(tmp_path, "leaderless") == _found(KILL_SIGNALLED_OUTPUT_HELD)


def _leaderless_until_told(pid_path: Path, threads: int, end_dir: Path) -> str:
    """A ``python3`` command, in a session of its own, whose main thread ends while ``threads`` threads it started run.

    Each thread runs until a file named by its own thread id appears in ``end_dir``, and the
    command writes its pid to ``pid_path`` once they run: the process then shows as a zombie, while
    its threads live and hold the command's output.
    """
    code = (
        "import ctypes, os, threading, time; "
        "e = lambda: [time.sleep(0.01) for _ in iter(lambda: not os.path.exists("
        f"os.path.join({str(end_dir)!r}, str(threading.get_native_id()))), False)]; "
        f"[threading.Thread(target=e).start() for _ in range({threads})]; "
        f"open({str(pid_path)!r}, 'w').write(str(os.getpid())); ctypes.CDLL(None).pthread_exit(None)"
    )
    return f'setsid python3 -c "{code}" &'


def _ending_the_first_thread_listed(bin_dir: Path, pid_path: Path, end_dir: Path, ended: Path) -> None:
    """Write to ``bin_dir`` an ``ls`` that, first asked for a thread's open files, ends that thread, then lists them.

    It tells the thread of the process whose pid ``pid_path`` holds to end (a file in
    ``end_dir``, :func:`_leaderless_until_told`), waits until the thread is gone and writes its id
    to ``ended``: the scan meets a thread that ended between the scan choosing it and the
    listing of its open files.
    """
    ls = bin_dir / "ls"
    ls.write_text(
        "#!/bin/sh\n"
        f'v="$(cat {pid_path})"\n'
        f'case "$2" in "/proc/$v/task/"*/fd) [ -e {ended} ] || {{\n'
        '  t="${2#/proc/$v/task/}"; t="${t%/fd}"\n'
        f'  : > "{end_dir}/$t"; while [ -e "/proc/$v/task/$t" ]; do sleep 0.01; done; echo "$t" > {ended}; }};;\n'
        "esac\n"
        f'PATH="${{PATH#{bin_dir}:}}"; exec ls "$@"\n'
    )
    ls.chmod(0o755)


@pytest.mark.parametrize(
    ("threads", "finding"),
    [(1, KILL_SIGNALLED), (2, KILL_SIGNALLED_OUTPUT_HELD)],
    ids=["its-last-thread-skipped", "the-next-thread-read"],
)
async def test_a_kill_reads_past_a_thread_of_a_leaderless_process_that_ends_while_it_is_listed(
    tmp_path: Path, threads: int, finding: str
) -> None:
    # The thread the scan lists ends first: a process that has no thread left holds nothing,
    # and one with a thread left is read from that thread.
    holder = tmp_path / "holder"
    end_dir = tmp_path / "end"
    end_dir.mkdir()
    ended = tmp_path / "ended"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _ending_the_first_thread_listed(bin_dir, holder, end_dir, ended)
    argv = ("sh", "-c", f"{_leaderless_until_told(holder, threads, end_dir)} sleep 30")
    async with _wrapped(tmp_path, "leaderless", *argv):
        with _stopped_at_exit(holder):
            await asyncio.wait_for(_until_exists(holder), _SCRIPT_TIMEOUT)
            while not holder.read_text():
                await asyncio.sleep(0.01)
            await asyncio.wait_for(_until_leader_ended(int(holder.read_text())), _SCRIPT_TIMEOUT)
            result = await _run_kill(tmp_path, "leaderless", path=f"{bin_dir}:{os.environ['PATH']}")
    assert result == _found(finding)
    assert ended.read_text()


def _churning(pid_path: Path) -> str:
    """A ``python3`` command, in a session of its own and holding none of the output, whose threads keep ending.

    It keeps 50 threads running, each of which lives for 5 ms and starts the one that takes its
    place as it ends, as a pool of short-lived workers does, and writes its pid to ``pid_path``
    once they run: its threads keep ending while a scan lists its open files.
    """
    code = (
        "import os, threading, time; "
        "w = lambda: (time.sleep(0.005), threading.Thread(target=w).start()); "
        "[threading.Thread(target=w).start() for _ in range(50)]; "
        f"open({str(pid_path)!r}, 'w').write(str(os.getpid())); time.sleep(30)"
    )
    return f'setsid python3 -c "{code}" < /dev/null > /dev/null 2>&1 &'


async def test_a_kill_reads_a_process_whose_threads_keep_ending_as_holding_nothing(tmp_path: Path) -> None:
    bystander = tmp_path / "bystander"
    argv = ("sh", "-c", f"{_churning(bystander)} sleep 30")
    async with _wrapped(tmp_path, "churned", *argv):
        with _stopped_at_exit(bystander):
            await asyncio.wait_for(_until_exists(bystander), _SCRIPT_TIMEOUT)
            while not bystander.read_text():
                await asyncio.sleep(0.01)
            assert await _run_kill(tmp_path, "churned") == _found(KILL_SIGNALLED)


async def test_a_kill_reads_no_process_of_the_wrappers_wait_as_a_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The first ``ls`` the wrapper's wait runs takes 0.5 s, as on a loaded host, and the kill
    # comes while it runs: neither it nor any process it started holds the output.
    bystander = tmp_path / "bystander"
    listing = tmp_path / "listing"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ls = bin_dir / "ls"
    ls.write_text(
        "#!/bin/sh\n"
        f"mkdir {tmp_path}/slowed 2>/dev/null && {{ : > {listing}; sleep 0.5; }}\n"
        f'PATH="${{PATH#{bin_dir}:}}"; exec ls "$@"\n'
    )
    ls.chmod(0o755)
    kill_path = os.environ["PATH"]
    monkeypatch.setenv("PATH", f"{bin_dir}:{kill_path}")
    async with _wrapped(tmp_path, "slowed", "sh", "-c", _passerby(bystander)) as wrapper:
        with _stopped_at_exit(bystander):
            await asyncio.wait_for(_until_exists(listing), _SCRIPT_TIMEOUT)
            result = await _run_kill(tmp_path, "slowed", path=kill_path)
            await asyncio.wait_for(wrapper.wait(), _SCRIPT_TIMEOUT)
    assert result == _found(KILL_SIGNALLED)


# A process name that holds a newline, as ``prctl(PR_SET_NAME)`` lets a process take.
_NEWLINE_NAME = "a\nb"


def _named(pid_path: Path, name: str) -> str:
    """A ``python3`` command, holding none of the output, that names itself ``name`` and writes its pid to ``pid_path``.

    It writes its pid only once it bears the name, so a scan run after the pid is written reads that name.
    """
    code = (
        "import ctypes, os, time; "
        f"ctypes.CDLL(None).prctl(15, {name.encode()!r}, 0, 0, 0); "
        f"open({str(pid_path)!r}, 'w').write(str(os.getpid())); time.sleep(30)"
    )
    return f'python3 -c "{code}" < /dev/null > /dev/null 2>&1'


async def test_a_kill_reads_a_process_whose_name_holds_a_newline(tmp_path: Path) -> None:
    named = tmp_path / "named"
    argv = ("sh", "-c", f"setsid {_named(named, _NEWLINE_NAME)} & sleep 30")
    async with _wrapped(tmp_path, "named", *argv):
        with _stopped_at_exit(named):
            await asyncio.wait_for(_until_exists(named), _SCRIPT_TIMEOUT)
            while not named.read_text():
                await asyncio.sleep(0.01)
            assert await _run_kill(tmp_path, "named") == _found(KILL_SIGNALLED)


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


@pytest.mark.parametrize("held", [True, False], ids=["held", "released"])
async def test_a_later_exec_scans_an_identity_of_the_wrappers_pid_start_time_and_output_links(
    tmp_path: Path, held: bool
) -> None:
    # The identity an ended wrapper left: its pid, a start time, and the pipe its stdout and
    # stderr link to, which a process of its own session still holds or none does.
    gone = await asyncio.create_subprocess_exec("true", start_new_session=True)
    await gone.wait()
    holder = await asyncio.create_subprocess_exec(
        "sleep", "30", stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, start_new_session=True
    )
    try:
        link = os.readlink(f"/proc/{holder.pid}/fd/1")
        identity = tmp_path / "tai-exec.left.out"
        identity.write_text(f"{gone.pid}\n{stat_start_time(holder.pid)}\n{link}\n{link}\n")
        if not held:
            holder.kill()
            await holder.wait()
        assert (await _run_wrapped(tmp_path, "later", "true"))[:3] == (0, b"", b"")
        assert identity.exists() is held
    finally:
        with contextlib.suppress(ProcessLookupError):
            holder.kill()
        await holder.wait()


async def test_a_later_exec_removes_an_identity_whose_holder_has_since_ended(tmp_path: Path) -> None:
    holder = tmp_path / "holder"
    async with _wrapped(tmp_path, "left", "sh", "-c", _escaped(holder)) as wrapper:
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


async def test_a_later_exec_fails_on_an_identity_it_cannot_open_with_one_line_giving_the_reason(
    tmp_path: Path,
) -> None:
    started = tmp_path / "started"
    identity = tmp_path / "tai-exec.socket.out"
    # A socket is there yet cannot be opened (ENXIO).
    with socket.socket(socket.AF_UNIX) as sock:
        sock.bind(str(identity))
        status, _, stderr, _ = await _run_wrapped(tmp_path, "after", "touch", str(started))
    assert status == 126
    assert stderr.startswith(f"cannot read the sandbox exec output identity {identity}: ".encode())
    assert b"No such device or address" in stderr
    assert stderr.count(b"\n") == 1
    assert stderr.endswith(b"\n")
    assert not started.exists()


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


async def test_a_wrapper_ends_once_a_process_whose_leading_thread_ended_lets_go_of_its_output(
    tmp_path: Path,
) -> None:
    # The argv exits at once, leaving a process whose leading thread ends while another thread
    # holds the output, writes to it later and then leaves ``done``, before it lets go of it.
    holder = tmp_path / "holder"
    done = tmp_path / "done"
    body = f"(time.sleep(0.3), os.write(1, b'late\\n'), open({str(done)!r}, 'w').close())"
    argv = ("sh", "-c", f"{_leaderless(holder, body)} & while [ ! -s {holder} ]; do sleep 0.01; done; exit 3")
    with _stopped_at_exit(holder):
        async with _wrapped(tmp_path, "leaderless", *argv) as wrapper:
            await asyncio.wait_for(_until_dead(wrapper.pid), _SCRIPT_TIMEOUT)
            assert done.exists()
            stdout, _ = await asyncio.wait_for(wrapper.communicate(), _SCRIPT_TIMEOUT)
    assert (wrapper.returncode, stdout) == (3, b"late\n")
    assert sorted(path.name for path in tmp_path.iterdir()) == ["done", "holder"]


async def test_a_wrapper_ends_with_the_argv_that_left_a_process_whose_threads_keep_ending(tmp_path: Path) -> None:
    bystander = tmp_path / "bystander"
    argv = ("sh", "-c", f"{_churning(bystander)} while [ ! -s {bystander} ]; do sleep 0.01; done; exit 3")
    with _stopped_at_exit(bystander):
        status, _, _, took = await _run_wrapped(tmp_path, "churned", *argv)
    assert status == 3
    assert took < 2


async def test_a_wrapper_reports_the_status_of_an_argv_that_left_a_process_whose_name_holds_a_newline(
    tmp_path: Path,
) -> None:
    named = tmp_path / "named"
    argv = ("sh", "-c", f"{_named(named, _NEWLINE_NAME)} & while [ ! -s {named} ]; do sleep 0.01; done; exit 7")
    with _stopped_at_exit(named):
        status, _, stderr, _ = await _run_wrapped(tmp_path, "named", *argv)
    assert (status, stderr) == (7, b"")


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
