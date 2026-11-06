"""The open-file scan's shell functions, run by a shell in a process namespace of their own.

Their ``/proc`` scan must see only the test's own processes, as inside a container it sees only
that container's.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import signal
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from tai42_kit.sandbox.open_file_scan import OPEN_FILE_SCAN

from .proc_stat import stat_flags, stat_start_time, wait_asleep
from .process_namespace import PROCESS_NAMESPACE_MARKS

pytestmark = [*PROCESS_NAMESPACE_MARKS, pytest.mark.usefixtures("ls_command")]

_SCRIPT_TIMEOUT = 10

# A ``VISIT`` that writes, for each process holding the file ``$want`` links to, its pid, its
# process-group id, its session id and whether its table was read, then goes on.
_REPORT_HOLDERS = 'report() { ofs_holds_link "$want" && echo "$ofs_pid $ofs_pgid $ofs_sid $ofs_table"; return 0; }; '


# Python that opens 50 files and closes them over and over, so a listing of its open files meets
# files closed between the listing's read of its ``fd`` directory and its read of their links.
_CHURN = (
    "while True:\n"
    "    fds = [os.open('/dev/null', os.O_RDONLY) for _ in range(50)]\n"
    "    for fd in fds:\n"
    "        os.close(fd)\n"
)


@contextlib.contextmanager
def _holding(path: Path, *, hidden: bool = False, name: str | None = None, churning: bool = False) -> Iterator[int]:
    """A process in a session of its own that holds ``path`` open on fd 5, its pid; it is stopped once the block ends.

    A ``hidden`` one makes itself non-dumpable, which hides its open files from its own user, one
    given a ``name`` takes it (``prctl(PR_SET_NAME)``) before it is ready, and a ``churning`` one,
    once ready, opens 50 files and closes them over and over. Any other one sleeps once the
    block starts.
    """
    setup = "import ctypes; ctypes.CDLL(None).prctl(4, 0, 0, 0, 0); " if hidden else ""
    if name is not None:
        setup += f"import ctypes; ctypes.CDLL(None).prctl(15, {name.encode()!r}, 0, 0, 0); "
    then = _CHURN if churning else "time.sleep(30)\n"
    code = f"import os, time; {setup}os.dup2(os.open({str(path)!r}, os.O_RDONLY), 5); print(flush=True)\n{then}"
    with subprocess.Popen(["python3", "-c", code], stdout=subprocess.PIPE, start_new_session=True) as proc:
        try:
            assert proc.stdout is not None
            proc.stdout.readline()
            if not churning:
                wait_asleep(proc.pid)
            yield proc.pid
        finally:
            proc.kill()


@contextlib.contextmanager
def _holding_in_a_group_of_its_own(path: Path) -> Iterator[tuple[int, int]]:
    """A process that holds ``path`` open on fd 5 and leads a group of its own: its pid and its session id.

    Its parent leads that session (``start_new_session``) and its group, the child having left
    the group with ``setpgid``. The child sleeps once the block starts; both are stopped once the
    block ends.
    """
    code = (
        "import os, time\n"
        "if os.fork() == 0:\n"
        "    os.setpgid(0, 0)\n"
        f"    os.dup2(os.open({str(path)!r}, os.O_RDONLY), 5)\n"
        "    print(os.getpid(), flush=True)\n"
        "    time.sleep(30)\n"
        "    os._exit(0)\n"
        "time.sleep(30)\n"
    )
    with subprocess.Popen(["python3", "-c", code], stdout=subprocess.PIPE, start_new_session=True) as proc:
        child = None
        try:
            assert proc.stdout is not None
            child = int(proc.stdout.readline())
            wait_asleep(child)
            yield child, proc.pid
        finally:
            if child is not None:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(child, signal.SIGKILL)
            proc.kill()


def _scan(script: str) -> subprocess.CompletedProcess[str]:
    """Run ``script`` after :data:`OPEN_FILE_SCAN` in ``sh``."""
    return subprocess.run(
        ["sh", "-c", OPEN_FILE_SCAN + script],
        capture_output=True,
        text=True,
        timeout=_SCRIPT_TIMEOUT,
        check=False,
    )


def _report_holders(path: Path, since: int, prelude: str = "") -> str:
    """What :data:`_REPORT_HOLDERS` writes for the processes started no earlier than ``since`` that hold ``path``."""
    result = _scan(f"want={shlex.quote(str(path))}; {prelude}{_REPORT_HOLDERS}ofs_scan {since} report")
    assert (result.returncode, result.stderr) == (0, "")
    return result.stdout


def test_the_scan_visits_a_process_holding_the_file_with_its_ids(tmp_path: Path) -> None:
    held = tmp_path / "held"
    held.write_text("")
    with _holding(held) as pid:
        assert _report_holders(held, stat_start_time(pid)) == f"{pid} {pid} {pid} 0\n"


# The fields ``ofs_stat`` sets, in order, as one line.
_STAT_FIELDS = 'echo "$ofs_stat_state $ofs_stat_pgid $ofs_stat_sid $ofs_stat_flags $ofs_stat_start"'


def test_the_stat_gives_a_process_state_group_session_flags_and_start_time(tmp_path: Path) -> None:
    held = tmp_path / "held"
    held.write_text("")
    with _holding(held) as pid:
        result = _scan(f"ofs_stat /proc/{pid}/stat && {_STAT_FIELDS}")
        expected = f"S {pid} {pid} {stat_flags(pid)} {stat_start_time(pid)}\n"
    assert (result.returncode, result.stdout) == (0, expected)


def test_the_stat_gives_the_session_of_a_process_apart_from_its_group(tmp_path: Path) -> None:
    held = tmp_path / "held"
    held.write_text("")
    with _holding_in_a_group_of_its_own(held) as (pid, sid):
        result = _scan(f"ofs_stat /proc/{pid}/stat && {_STAT_FIELDS}")
        expected = f"S {pid} {sid} {stat_flags(pid)} {stat_start_time(pid)}\n"
    assert (result.returncode, result.stdout) == (0, expected)


def test_the_scan_visits_a_process_in_a_group_of_its_own_with_its_group_and_its_session(tmp_path: Path) -> None:
    held = tmp_path / "held"
    held.write_text("")
    with _holding_in_a_group_of_its_own(held) as (pid, sid):
        assert _report_holders(held, stat_start_time(pid)) == f"{pid} {pid} {sid} 0\n"


def test_a_read_after_the_uid_of_a_process_that_ends_reads_its_own_file_under_bash(tmp_path: Path) -> None:
    # bash reads a file in blocks and seeks back over what a read leaves, and keeps that rest for
    # the next read of ANY file when it cannot seek back, as in the status of a process that has
    # just ended: each read of a known file after the uid read must give that file's own line.
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not on this host")
    known = tmp_path / "known"
    known.write_text("known\n")
    script = (
        "n=0; s=0; while [ $n -lt 300 ]; do n=$((n + 1)); true & p=$!; k=0; while [ $k -lt 8 ]; do "
        f"k=$((k + 1)); ofs_read_uid /proc/$p/status 2>/dev/null; read -r line < {shlex.quote(str(known))}; "
        '[ "$line" = known ] || s=$((s + 1)); done; wait; done; echo "$s"'
    )
    result = subprocess.run(
        [bash, "-c", OPEN_FILE_SCAN + script], capture_output=True, text=True, timeout=_SCRIPT_TIMEOUT, check=False
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, "0\n", "")


def test_the_stat_of_a_process_that_is_gone_fails(tmp_path: Path) -> None:
    result = _scan(f"ofs_stat {shlex.quote(str(tmp_path / 'stat'))} 2>/dev/null")
    assert result.returncode == 1


def test_a_scan_run_from_a_subshell_visits_that_subshell(tmp_path: Path) -> None:
    held = tmp_path / "held"
    held.write_text("")
    # The subshell writes its own pid as the parent's pid of a child it starts, since ``$$`` names
    # the script's shell there.
    result = _scan(
        f"want={shlex.quote(str(held))}; {_REPORT_HOLDERS}echo $$; "
        f"( exec 5< {shlex.quote(str(held))}; sh -c 'echo $PPID'; ofs_scan 0 report ); exit"
    )
    shell, subshell, visited = result.stdout.splitlines()
    pid, _, _, table = visited.split()
    assert (result.returncode, pid, table) == (0, subshell, "0")
    assert pid != shell


def test_the_scan_visits_no_process_started_before_the_time_given(tmp_path: Path) -> None:
    held = tmp_path / "held"
    held.write_text("")
    with _holding(held) as pid:
        assert _report_holders(held, stat_start_time(pid) + 1) == ""


def test_the_scan_never_visits_the_scripts_own_shell(tmp_path: Path) -> None:
    held = tmp_path / "held"
    held.write_text("")
    assert _report_holders(held, 0, prelude=f"exec 5< {shlex.quote(str(held))}; ") == ""


def test_the_scan_matches_a_file_removed_since_it_was_opened(tmp_path: Path) -> None:
    held = tmp_path / "held"
    held.write_text("")
    with _holding(held) as pid:
        held.unlink()
        assert _report_holders(held, stat_start_time(pid)) == f"{pid} {pid} {pid} 0\n"


def test_the_scan_visits_a_process_whose_open_files_it_cannot_read_as_unread(tmp_path: Path) -> None:
    held = tmp_path / "held"
    held.write_text("")
    with _holding(held, hidden=True) as pid:
        result = _scan(f'unread() {{ [ "$ofs_pid" = {pid} ] && echo "$ofs_table"; return 0; }}; ofs_scan 0 unread')
    assert (result.returncode, result.stdout) == (0, "2\n")


def test_the_table_of_a_process_that_closes_files_as_they_are_listed_is_read(tmp_path: Path) -> None:
    held = tmp_path / "held"
    held.write_text("")
    with _holding(held, churning=True) as pid:
        result = _scan(
            f"i=0; while [ $i -lt 20 ]; do i=$((i + 1)); ofs_table=0; "
            f'ofs_fd_table /proc/{pid} || ofs_table="$?"; '
            f'ofs_holds_link {shlex.quote(str(held))} && echo "$ofs_table held" || echo "$ofs_table: $ofs_fds"; done'
        )
    assert (result.returncode, result.stdout, result.stderr) == (0, "0 held\n" * 20, "")


# What a stub ``ls`` writes on its stderr for a listing of a thread's ``fd`` directory, ``{fd}``
# standing for that directory, the status it exits with, and the status ``ofs_fd_table`` gives
# for the thread. An entry of ``fd`` that is not there (``No such file or directory``) is an fd
# the thread closed as it was listed, as coreutils' ``ls`` and busybox's report it.
_LISTING_STDERR = {
    "closed-fds-coreutils": (
        [
            "ls: cannot access '{fd}/41': No such file or directory",
            "ls: cannot read symbolic link '{fd}/42': No such file or directory",
        ],
        1,
        0,
    ),
    "closed-fds-busybox": (
        ["ls: {fd}/3: No such file or directory", "ls: {fd}/22: cannot read link: No such file or directory"],
        0,
        0,
    ),
    "a-permission-error": (["ls: cannot read symbolic link '{fd}/5': Permission denied"], 1, 2),
    "a-closed-fd-and-a-permission-error": (
        ["ls: {fd}/3: No such file or directory", "ls: {fd}/5: cannot read link: Permission denied"],
        0,
        2,
    ),
    "the-directory-not-there": (["ls: cannot open directory '{fd}': No such file or directory"], 2, 2),
    "a-failure-with-no-text": ([], 2, 2),
}


@pytest.mark.parametrize("listing", _LISTING_STDERR.values(), ids=_LISTING_STDERR.keys())
def test_a_listing_fails_on_what_ls_reports_but_an_fd_closed_as_it_was_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listing: tuple[list[str], int, int]
) -> None:
    lines, status, table = listing
    held = tmp_path / "held"
    process = tmp_path / "proc" / "7"
    _fake_thread(process, exiting=False, holds=held)
    fd = process / "fd"
    report = f"printf '%s\\n' {' '.join(shlex.quote(line.format(fd=fd)) for line in lines)} >&2" if lines else ":"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ls = bin_dir / "ls"
    ls.write_text(
        "#!/bin/sh\n"
        f'PATH="${{PATH#{bin_dir}:}}"; [ "$2" = {shlex.quote(str(fd))} ] || exec ls "$@"; ls "$@" || exit\n'
        f"{report}; exit {status}\n"
    )
    ls.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    result = _scan(f'ofs_table=0; ofs_fd_table {shlex.quote(str(process))} || ofs_table="$?"; echo "$ofs_table"')
    assert (result.returncode, result.stdout, result.stderr) == (0, f"{table}\n", "")


def test_a_listing_reads_what_ls_reports_in_the_c_locale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The stub ``ls`` reports an fd closed as it was listed in the language its locale names:
    # the scan reads it as such in the C locale, whatever the caller's.
    held = tmp_path / "held"
    process = tmp_path / "proc" / "7"
    _fake_thread(process, exiting=False, holds=held)
    fd = process / "fd"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ls = bin_dir / "ls"
    ls.write_text(
        "#!/bin/sh\n"
        f'PATH="${{PATH#{bin_dir}:}}"; [ "$2" = {shlex.quote(str(fd))} ] || exec ls "$@"; ls "$@" || exit\n'
        f'[ "$LC_ALL" = C ] && reason="No such file or directory" || reason="Datei oder Verzeichnis nicht gefunden"\n'
        f'printf "%s\\n" "ls: {fd}/3: $reason" >&2\n'
    )
    ls.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    result = _scan(f'ofs_table=0; ofs_fd_table {shlex.quote(str(process))} || ofs_table="$?"; echo "$ofs_table"')
    assert (result.returncode, result.stdout, result.stderr) == (0, "0\n", "")


def test_a_visit_that_fails_ends_the_scan_with_its_status(tmp_path: Path) -> None:
    held = tmp_path / "held"
    held.write_text("")
    with _holding(held) as pid:
        script = f'want={shlex.quote(str(held))}; found() {{ ofs_holds_link "$want" && return 7; return 0; }}; '
        held_by_one = _scan(f"{script}ofs_scan {stat_start_time(pid)} found")
        held_by_none = _scan(f"{script}ofs_scan {stat_start_time(pid) + 1} found")
    assert (held_by_one.returncode, held_by_none.returncode) == (7, 0)


@pytest.mark.parametrize(
    "state",
    ["set -e", "set -f", "IFS=:", "IFS=' 0123456789'"],
    ids=["errexit", "noglob", "ifs-colon", "ifs-digits"],
)
def test_the_functions_read_the_same_whatever_the_callers_shell_state(tmp_path: Path, state: str) -> None:
    # A process whose open files the scan reads, and one whose it cannot; the caller's options
    # and ``IFS`` are its own again once the scan is done.
    held = tmp_path / "held"
    held.write_text("")
    with _holding(held) as pid, _holding(held, hidden=True) as hidden:
        result = _scan(
            f'{state}; ifs="$IFS"; '
            f'seen() {{ case "$ofs_pid" in {pid}|{hidden}) echo "$ofs_pid $ofs_pgid $ofs_table";; esac; }}; '
            f"ofs_stat /proc/{pid}/stat; {_STAT_FIELDS}; "
            f'ofs_read_uid /proc/{pid}/status; echo "uid $ofs_uid"; '
            f'ofs_scan {stat_start_time(pid)} seen; echo "scan $? $-"; [ "$IFS" = "$ifs" ] && echo ifs-kept'
        )
        start = stat_start_time(pid)
        flags = stat_flags(pid)
    stat, uid, *visits, scan, ifs = result.stdout.splitlines()
    assert (result.returncode, result.stderr) == (0, "")
    assert (stat, uid) == (f"S {pid} {pid} {flags} {start}", f"uid {os.getuid()}")
    assert sorted(visits) == sorted([f"{pid} {pid} 0", f"{hidden} {hidden} 2"])
    assert scan.startswith("scan 0 ")
    assert ("f" in scan.split(" ")[2]) == (state == "set -f")
    assert ifs == "ifs-kept"


@contextlib.contextmanager
def _holding_in_a_thread(path: Path, told: Path) -> Iterator[int]:
    """A process in a session of its own whose threads share ``path`` open on fd 5, its pid; stopped at the block's end.

    Its leading thread ends (``pthread_exit``) once ``told`` exists, while the thread it started
    lives on, holding the file.
    """
    code = (
        "import ctypes, os, threading, time; "
        f"os.dup2(os.open({str(path)!r}, os.O_RDONLY), 5); threading.Thread(target=time.sleep, args=(30,)).start(); "
        f"print(flush=True); [time.sleep(0.01) for _ in iter(lambda: not os.path.exists({str(told)!r}), False)]; "
        "ctypes.CDLL(None).pthread_exit(None)"
    )
    with subprocess.Popen(["python3", "-c", code], stdout=subprocess.PIPE, start_new_session=True) as proc:
        try:
            assert proc.stdout is not None
            proc.stdout.readline()
            yield proc.pid
        finally:
            proc.kill()


def test_the_table_of_a_process_whose_leading_thread_ends_as_it_is_listed_is_read_from_a_live_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The ``ls`` asked for the leading thread's open files first has that thread end and lists
    # them once it is a zombie, which root reads as an empty table while the other thread still
    # holds the file.
    held = tmp_path / "held"
    held.write_text("")
    told = tmp_path / "told"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    with _holding_in_a_thread(held, told) as pid:
        ls = bin_dir / "ls"
        ls.write_text(
            "#!/bin/sh\n"
            f'[ "$2" = /proc/{pid}/fd ] && {{ : > {told}; '
            f'while read -r s < /proc/{pid}/stat && s="${{s##*) }}" && [ "${{s%% *}}" != Z ]; do sleep 0.01; done; }}\n'
            f'PATH="${{PATH#{bin_dir}:}}"; exec ls "$@"\n'
        )
        ls.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
        result = _scan(f'ofs_fd_table /proc/{pid}; echo "$?"; ofs_holds_link {shlex.quote(str(held))} && echo held')
    assert (result.returncode, result.stdout, told.exists()) == (0, "0\nheld\n", True)


def _fake_thread(directory: Path, *, exiting: bool, holds: Path | None) -> None:
    """Write at ``directory`` a ``stat`` and an ``fd`` as ``/proc`` gives a thread's, exiting or not.

    Its flags (the 9th field) carry ``PF_EXITING`` (0x4) for an exiting thread, and its ``fd``
    holds a link to ``holds`` when given.
    """
    (directory / "fd").mkdir(parents=True)
    if holds is not None:
        (directory / "fd" / "5").symlink_to(holds)
    flags = 0x400104 if exiting else 0x400100
    (directory / "stat").write_text(f"7 (x) S 1 7 7 0 -1 {flags} {' '.join(['0'] * 12)} 100 0 0\n")


@pytest.mark.parametrize(
    ("live", "expected"),
    [(True, "0 held"), (False, "2 empty")],
    ids=["a-live-thread-read", "every-thread-exiting"],
)
def test_the_listing_of_an_exiting_thread_is_not_read_as_the_table(tmp_path: Path, live: bool, expected: str) -> None:
    # The leading thread and the first other one are exiting and list no open file, as one whose
    # files are already closed; the last one, when live, shares the table and lists the file.
    held = tmp_path / "held"
    process = tmp_path / "proc" / "7"
    _fake_thread(process, exiting=True, holds=None)
    _fake_thread(process / "task" / "7", exiting=True, holds=None)
    _fake_thread(process / "task" / "8", exiting=not live, holds=held if live else None)
    result = _scan(
        f'ofs_table=0; ofs_fd_table {shlex.quote(str(process))} || ofs_table="$?"; '
        f'ofs_holds_link {shlex.quote(str(held))} && echo "$ofs_table held" || echo "$ofs_table empty"'
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, f"{expected}\n", "")


# A process name that holds ``) `` twice and a newline, which ``prctl(PR_SET_NAME)`` lets a process
# take: read past its first ``) ``, the fields would come from the name.
_PARENTHESIS_NAME = "a) b\nc) d 1 2"


def test_the_stat_reads_the_fields_past_a_name_that_holds_a_closing_parenthesis(tmp_path: Path) -> None:
    held = tmp_path / "held"
    held.write_text("")
    with _holding(held, name=_PARENTHESIS_NAME) as pid:
        assert Path(f"/proc/{pid}/comm").read_text() == f"{_PARENTHESIS_NAME}\n"
        result = _scan(f"ofs_stat /proc/{pid}/stat && {_STAT_FIELDS}")
        expected = f"S {pid} {pid} {stat_flags(pid)} {stat_start_time(pid)}\n"
    assert (result.returncode, result.stdout) == (0, expected)
