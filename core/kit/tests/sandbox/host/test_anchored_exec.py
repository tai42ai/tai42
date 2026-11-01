"""The anchored-exec bootstrap: what the command starts with, and every failure before it runs."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import re
import select
import signal
import socket
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from tai42_contract.sandbox import (
    ExecResult,
    SandboxError,
)

from tai42_kit.sandbox import HostProcess, anchored_exec, run_host_process

from .children import (
    PROMPT_SECONDS,
    SPAWNED,
    group_members,
    spawn,
)


async def test_a_command_that_waits_for_every_child_exits_at_once() -> None:
    wait_for_every_child = (
        "import os\nwhile True:\n    try:\n        os.wait()\n    except ChildProcessError:\n        break"
    )
    process = await spawn(sys.executable, "-c", wait_for_every_child)
    result = await run_host_process(process, stdin=None, timeout_seconds=PROMPT_SECONDS)
    assert result == ExecResult(exit_code=0, stdout="", stderr="")


async def test_the_command_sees_its_own_argv_and_only_its_three_pipes() -> None:
    show = "import json, os, sys; print(json.dumps([sys.orig_argv, sorted(int(f) for f in os.listdir('/dev/fd'))]))"
    argv = [sys.executable, "-c", show, "one", "two words"]
    process = await spawn(*argv, stdin=True)
    result = await run_host_process(process, stdin=b"", timeout_seconds=PROMPT_SECONDS)
    seen_argv, fds = json.loads(result.stdout)
    assert seen_argv == argv
    # The listing's own directory fd is the one fd past the three pipes.
    assert fds[:3] == [0, 1, 2]
    assert len(fds) == 4


async def test_a_missing_binary_raises_and_the_command_never_runs() -> None:
    with pytest.raises(FileNotFoundError) as excinfo:
        await spawn("no-such-binary-xyz")
    assert excinfo.value.filename == "no-such-binary-xyz"
    assert subprocess.run(["pgrep", "-f", "no-such-binary-xyz"], capture_output=True, check=False).returncode == 1


# Prepended to the bootstrap, a fault that makes its ``nth`` ``os.fork`` call fail as a fork past the
# process limit does; each forked process counts on from the count it was forked with.
_NTH_FORK_FAILS = """\
import os
_real_fork = os.fork
_forks = []
def _fork():
    _forks.append(None)
    if len(_forks) == {nth}:
        raise BlockingIOError(11, "Resource temporarily unavailable")
    return _real_fork()
os.fork = _fork
"""


# Prepended to the bootstrap, a fault that makes the anchor's own ``/bin/sh`` fail to start.
_ANCHOR_SHELL_FAILS = """\
import os
_real_execv = os.execv
def _execv(path, args):
    if path == "/bin/sh":
        raise PermissionError(13, "Permission denied", path)
    return _real_execv(path, args)
os.execv = _execv
"""


@pytest.mark.parametrize(
    "fault",
    [
        pytest.param(_NTH_FORK_FAILS.format(nth=1), id="the-anchors-parent-cannot-fork"),
        pytest.param(_NTH_FORK_FAILS.format(nth=2), id="the-anchor-cannot-fork"),
        pytest.param(_ANCHOR_SHELL_FAILS, id="the-anchors-shell-cannot-start"),
    ],
)
async def test_a_spawn_whose_group_anchor_fails_to_start_raises_and_never_runs_the_command(
    fault: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(anchored_exec, "ANCHORED_EXEC", fault + anchored_exec.ANCHORED_EXEC)
    marker = tmp_path / "command-ran"
    with pytest.raises(SandboxError, match=r"failed while (forking|starting) the anchor"):
        await spawn("touch", str(marker))
    assert not marker.exists()
    found = subprocess.run(["pgrep", "-f", str(marker)], capture_output=True, check=False)
    assert found.returncode == 1, "a failed spawn left its bootstrap running"


async def test_a_relative_path_entry_is_searched_from_the_exec_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = tmp_path / "bin" / "tool"
    tool.parent.mkdir()
    tool.write_text("#!/bin/sh\necho tool-ran\n")
    tool.chmod(0o755)
    # The kit runs from a directory with no ``bin/tool``: only the exec's cwd has one.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    env = {"PATH": f"bin:{os.environ['PATH']}"}
    process = await HostProcess.spawn(["tool"], cwd=str(tmp_path), env=env, stdin=False)
    SPAWNED.append(process)
    result = await run_host_process(process, stdin=None, timeout_seconds=PROMPT_SECONDS)
    assert result == ExecResult(exit_code=0, stdout="tool-ran\n", stderr="")


def _not_a_program(workspace: Path) -> None:
    (workspace / "target").write_bytes(b"\x00\x01 not a program\n")
    (workspace / "target").chmod(0o755)


def _not_executable(workspace: Path) -> None:
    (workspace / "target").write_text("#!/bin/sh\necho ran\n")
    (workspace / "target").chmod(0o644)


def _a_directory(workspace: Path) -> None:
    (workspace / "target").mkdir()


def _nothing(workspace: Path) -> None:
    del workspace


@pytest.mark.parametrize(
    ("make_target", "code"),
    [
        (_not_a_program, errno.ENOEXEC),
        (_not_executable, errno.EACCES),
        (_a_directory, errno.EACCES),
        (_nothing, errno.ENOENT),
    ],
)
async def test_an_exec_the_host_refuses_raises_the_error_popen_raises(
    make_target: Callable[[Path], None], code: int, tmp_path: Path
) -> None:
    make_target(tmp_path)
    env = {"PATH": os.environ["PATH"]}
    refused = re.escape(os.strerror(code))
    with pytest.raises(OSError, match=refused) as by_popen:
        subprocess.Popen(["./target"], cwd=tmp_path, env=env)
    with pytest.raises(OSError, match=refused) as by_spawn:
        await HostProcess.spawn(["./target"], cwd=str(tmp_path), env=env, stdin=False)
    assert type(by_spawn.value) is type(by_popen.value)
    assert (by_spawn.value.errno, by_spawn.value.strerror, by_spawn.value.filename) == (
        by_popen.value.errno,
        by_popen.value.strerror,
        by_popen.value.filename,
    )


@pytest.mark.parametrize(
    "sig", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGPIPE, signal.SIGXFSZ], ids=lambda sig: sig.name
)
async def test_the_command_starts_with_the_default_disposition_of_the_signals_a_plain_child_gets(
    sig: signal.Signals,
) -> None:
    # A signal ignored on entry to a shell stays ignored there: one restored to its default kills it.
    process = await spawn("sh", "-c", f'kill -s {sig.name.removeprefix("SIG")} "$$"; echo survived')
    result = await run_host_process(process, stdin=None, timeout_seconds=PROMPT_SECONDS)
    assert result == ExecResult(exit_code=-sig, stdout="", stderr="")


@pytest.mark.parametrize(
    "locale_env",
    [{}, {"LANG": "C"}, {"LC_CTYPE": "POSIX"}],
    ids=["no-locale", "LANG=C", "LC_CTYPE=POSIX"],
)
async def test_the_command_gets_exactly_the_env_it_was_spawned_with(locale_env: dict[str, str]) -> None:
    env = {"PATH": os.environ["PATH"], "ACME_VALUE": "a b=c", **locale_env}
    process = await HostProcess.spawn(["env", "-0"], cwd=os.getcwd(), env=env, stdin=False)
    SPAWNED.append(process)
    result = await run_host_process(process, stdin=None, timeout_seconds=PROMPT_SECONDS)
    assert result.exit_code == 0
    seen = dict(entry.split("=", 1) for entry in result.stdout.split("\0") if entry)
    assert seen == env


async def test_the_anchor_holds_no_directory_of_the_exec(tmp_path: Path) -> None:
    process = await HostProcess.spawn(["sleep", "30"], cwd=str(tmp_path), env={"PATH": os.environ["PATH"]}, stdin=False)
    SPAWNED.append(process)
    pid = process._transport.get_pid()
    [anchor] = group_members(pid) - {pid}
    try:
        anchor_cwd = os.readlink(f"/proc/{anchor}/cwd")
    except FileNotFoundError:
        pytest.skip("reading a process's cwd needs /proc")
    assert anchor_cwd == "/"


async def test_the_anchor_holds_its_hold_and_dev_null_and_no_other_fd() -> None:
    process = await spawn("sleep", "30", stdin=True)
    pid = process._transport.get_pid()
    [anchor] = group_members(pid) - {pid}
    fds = {int(fd): os.readlink(f"/proc/{anchor}/fd/{fd}") for fd in os.listdir(f"/proc/{anchor}/fd")}
    assert sorted(fds) == [0, 1, 2]
    assert fds[0].startswith("socket:")
    assert fds[1] == fds[2] == os.devnull


async def test_the_command_starts_with_no_child_of_its_own() -> None:
    show_children = (
        "import os\n"
        "try:\n"
        "    os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)\n"
        "except ChildProcessError:\n"
        "    print('none')\n"
        "else:\n"
        "    print('some')"
    )
    process = await spawn(sys.executable, "-c", show_children)
    result = await run_host_process(process, stdin=None, timeout_seconds=PROMPT_SECONDS)
    assert result == ExecResult(exit_code=0, stdout="none\n", stderr="")


async def test_a_large_env_reaches_the_command_whole() -> None:
    env = {"PATH": os.environ["PATH"], "ACME_LARGE": "x" * 100_000, "ACME_OTHER": "y" * 100_000}
    show = "import os; print(len(os.environb[b'ACME_LARGE']), len(os.environb[b'ACME_OTHER']))"
    process = await HostProcess.spawn([sys.executable, "-c", show], cwd=os.getcwd(), env=env, stdin=False)
    SPAWNED.append(process)
    result = await run_host_process(process, stdin=None, timeout_seconds=PROMPT_SECONDS)
    assert result == ExecResult(exit_code=0, stdout="100000 100000\n", stderr="")


async def test_an_env_value_that_is_not_utf8_reaches_the_command_as_its_raw_bytes() -> None:
    # The host's bytes ``\xff\xfe``, as ``os.environ`` holds them: lone surrogates.
    env = {"PATH": os.environ["PATH"], "ACME_RAW": os.fsdecode(b"\xff\xfe")}
    show = "import os, sys; sys.stdout.write(os.environb[b'ACME_RAW'].hex())"
    process = await HostProcess.spawn([sys.executable, "-c", show], cwd=os.getcwd(), env=env, stdin=False)
    SPAWNED.append(process)
    result = await run_host_process(process, stdin=None, timeout_seconds=PROMPT_SECONDS)
    assert result == ExecResult(exit_code=0, stdout="fffe", stderr="")


def _path_of_dirs(workspace: Path) -> str:
    del workspace
    return os.environ["PATH"]


def _path_of_missing_dirs(workspace: Path) -> str:
    return f"{workspace / 'missing'}:{workspace / 'gone'}"


def _path_through_a_file(workspace: Path) -> str:
    (workspace / "file").write_text("")
    return f"{workspace / 'missing'}:{workspace / 'file'}"


@pytest.mark.parametrize(
    ("make_path", "error"),
    [
        pytest.param(_path_of_dirs, PermissionError, id="directories"),
        pytest.param(_path_of_missing_dirs, FileNotFoundError, id="missing-directories"),
        pytest.param(_path_through_a_file, NotADirectoryError, id="a-file"),
    ],
)
async def test_an_empty_program_name_raises_the_error_popen_raises(
    make_path: Callable[[Path], str], error: type[OSError], tmp_path: Path
) -> None:
    env = {"PATH": make_path(tmp_path)}
    with pytest.raises(error) as by_popen:
        subprocess.Popen([""], cwd=tmp_path, env=env)
    with pytest.raises(error) as by_spawn:
        await HostProcess.spawn([""], cwd=str(tmp_path), env=env, stdin=False)
    assert type(by_spawn.value) is type(by_popen.value)
    assert (by_spawn.value.errno, by_spawn.value.strerror, by_spawn.value.filename) == (
        by_popen.value.errno,
        by_popen.value.strerror,
        by_popen.value.filename,
    )


# Prepended to the bootstrap, a fault that makes the command's exec raise ``error`` instead of
# an ``OSError``.
_EXEC_RAISES = """\
import os
def _execvpe(file, args, env):
    raise {error}
os.execvpe = _execvpe
"""


@pytest.mark.parametrize(
    "error",
    ["KeyboardInterrupt()", "ValueError('refused')", "MemoryError()"],
    ids=["KeyboardInterrupt", "ValueError", "MemoryError"],
)
async def test_an_exec_that_fails_with_any_error_raises_and_never_runs_the_command(
    error: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(anchored_exec, "ANCHORED_EXEC", _EXEC_RAISES.format(error=error) + anchored_exec.ANCHORED_EXEC)
    marker = tmp_path / "command-ran"
    with pytest.raises(SandboxError, match=r"failed while starting the command: " + error.partition("(")[0]):
        await spawn("touch", str(marker))
    assert not marker.exists()


# Prepended to the bootstrap, a SIGINT the bootstrap gets once it restored the command's signal
# dispositions, just before the exec.
_INTERRUPTED_BEFORE_THE_EXEC = """\
import os, signal
_real_execvpe = os.execvpe
def _execvpe(file, args, env):
    os.kill(os.getpid(), signal.SIGINT)
    return _real_execvpe(file, args, env)
os.execvpe = _execvpe
"""


async def test_a_sigint_just_before_the_exec_acts_as_on_the_command_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(anchored_exec, "ANCHORED_EXEC", _INTERRUPTED_BEFORE_THE_EXEC + anchored_exec.ANCHORED_EXEC)
    marker = tmp_path / "command-ran"
    process = await spawn("touch", str(marker))
    result = await run_host_process(process, stdin=None, timeout_seconds=PROMPT_SECONDS)
    assert result == ExecResult(exit_code=-signal.SIGINT, stdout="", stderr="")
    assert not marker.exists()


async def test_an_env_name_the_host_cannot_hold_raises_before_anything_starts() -> None:
    with pytest.raises(ValueError, match="illegal environment variable name"):
        await HostProcess.spawn(["true"], cwd=os.getcwd(), env={"": "x", "PATH": os.environ["PATH"]}, stdin=False)


async def test_env_names_that_are_one_name_on_the_host_raise_before_anything_starts() -> None:
    # ``é`` as text and as the surrogates of its UTF-8 bytes: two keys, one name on the host.
    env = {"PATH": os.environ["PATH"], "\xe9": "first", "\udcc3\udca9": "second"}
    with pytest.raises(ValueError, match="the same name on the host"):
        await HostProcess.spawn(["true"], cwd=os.getcwd(), env=env, stdin=False)


async def test_a_text_and_a_bytes_path_raise_the_error_popen_raises() -> None:
    env = cast("dict[str, str]", {"PATH": "/usr/bin", b"PATH": b"/bin"})
    refused = f"^{re.escape("env cannot contain 'PATH' and b'PATH' keys")}$"
    with pytest.raises(ValueError, match=refused):
        subprocess.Popen(["true"], env=env)
    with pytest.raises(ValueError, match=refused):
        await HostProcess.spawn(["true"], cwd=os.getcwd(), env=env, stdin=False)


@pytest.mark.parametrize(
    "cut",
    [
        pytest.param(lambda whole: b"", id="nothing"),
        pytest.param(lambda whole: whole[: len(whole) // 2], id="inside-an-entry"),
        pytest.param(lambda whole: whole[: whole.index(b"\0") + 1], id="after-an-entry"),
        pytest.param(lambda whole: whole[:-1], id="all-but-the-end"),
    ],
)
def test_a_bootstrap_whose_env_pipe_closes_before_the_env_ends_never_starts_the_command(
    tmp_path: Path, cut: Callable[[bytes], bytes]
) -> None:
    marker = tmp_path / "command-ran"
    whole = anchored_exec.encode_env({"PATH": os.environ["PATH"], "ACME": "1"})
    hold, theirs = socket.socketpair()
    report, report_end = os.pipe()
    env_end, env_feed = os.pipe()
    with hold:
        try:
            bootstrap = subprocess.Popen(
                anchored_exec.bootstrap_argv(
                    ["touch", str(marker)], hold_fd=theirs.fileno(), report_fd=report_end, env_fd=env_end
                ),
                env={},
                pass_fds=(theirs.fileno(), report_end, env_end),
                start_new_session=True,
            )
        finally:
            theirs.close()
            os.close(report_end)
            os.close(env_end)
        os.write(env_feed, cut(whole))
        os.close(env_feed)
        with os.fdopen(report, "rb") as reported:
            failure = reported.read()
        assert bootstrap.wait(timeout=PROMPT_SECONDS) == 1
    assert failure == b"setup:reading the command's env:EOFError: the env pipe closed before the env's end"
    assert not marker.exists()


def _is_open(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


async def test_a_cancelled_report_read_closes_its_end_while_the_writer_still_holds_its_own() -> None:
    report, writer = os.pipe()
    try:
        reading = asyncio.ensure_future(anchored_exec.read_to_eof(report))
        await asyncio.sleep(0.05)
        reading.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reading
        assert not _is_open(report)
    finally:
        os.close(writer)


async def test_an_aborted_feed_closes_its_end_at_once_with_its_abort_while_the_reader_still_holds_its_own() -> None:
    reader, env_feed = os.pipe()
    try:
        # More than the pipe holds, with nobody reading: the write stays pending.
        feed = await anchored_exec.PipeFeed.start(env_feed, b"x" * 1_000_000)
        async with asyncio.timeout(PROMPT_SECONDS):
            feed.abort()
            closed = await feed.closed()
        assert isinstance(closed, ConnectionAbortedError)
        assert str(closed) == "the feed was aborted before it wrote everything"
        assert not _is_open(env_feed)
    finally:
        os.close(reader)


async def test_a_feed_aborted_once_it_wrote_everything_closes_as_written() -> None:
    reader, env_feed = os.pipe()
    try:
        # Less than the pipe holds: all of it is written, and the reader has it before the abort.
        feed = await anchored_exec.PipeFeed.start(env_feed, b"x" * 1000)
        async with asyncio.timeout(PROMPT_SECONDS):
            while not select.select([reader], [], [], 0)[0]:
                await asyncio.sleep(0.01)
            assert os.read(reader, 2000) == b"x" * 1000
            feed.abort()
            assert await feed.closed() is None
    finally:
        os.close(reader)


async def test_a_feed_whose_reader_closed_its_end_closes_with_the_write_error() -> None:
    reader, env_feed = os.pipe()
    feed = await anchored_exec.PipeFeed.start(env_feed, b"x" * 1_000_000)
    os.close(reader)
    async with asyncio.timeout(PROMPT_SECONDS):
        assert isinstance(await feed.closed(), BrokenPipeError)
    assert not _is_open(env_feed)


async def test_a_feed_closes_its_end_once_the_reader_took_everything() -> None:
    reader, env_feed = os.pipe()
    data = b"x" * 1_000_000
    feed = await anchored_exec.PipeFeed.start(env_feed, data)
    loop = asyncio.get_running_loop()
    received = await loop.run_in_executor(None, _read_to_eof_blocking, reader)
    async with asyncio.timeout(PROMPT_SECONDS):
        assert await feed.closed() is None
    assert received == data


def _read_to_eof_blocking(fd: int) -> bytes:
    with os.fdopen(fd, "rb") as pipe:
        return pipe.read()
