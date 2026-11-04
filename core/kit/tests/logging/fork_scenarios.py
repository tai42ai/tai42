"""The fork scenarios ``test_fork_safety`` runs, each in its own interpreter.

Run as ``python fork_scenarios.py <scenario>``: the scenario forks through one door
while another thread is provably inside a stream's ``write`` under its buffer lock, and prints
what it observed as one JSON object on stdout. The test asserts on that object and bounds the
interpreter, so a fork that never completes, in the parent or in the child, is killed at the
bound instead of hanging the suite.

The held lock is made, not raced for: a writer thread logs one line far larger than a pipe holds
to a handler whose stream writes into that pipe, and the fork happens only once ``select``
reports the pipe full while the writer is still running. The writer is then blocked in
``write(2)`` inside the stream's buffered writer, holding its lock, and stays there until the
parent drains the pipe, which it starts doing only once the fork has happened.

``multiprocessing`` flushes ``sys.stdout`` and ``sys.stderr`` in the parent before it forks, so a
``sys.stderr`` held by a writer would block that flush, not the fork: its scenario holds a file
stream, and its child flushes the rebuilt ``sys.stderr`` on its way out.

A held ``sys.stderr`` the hook cannot rebuild (utf-16 over a pipe, where whether the parent already
wrote the byte-order mark cannot be read) keeps its lock in the child; the hook's report of it still
reaches the pipe, and the child, which writes nothing itself, exits. So does a held
``sys.__stderr__`` that ``sys.stderr``, a ``codecs`` writer or a forwarding proxy, writes through:
no handler writes to that buffer, so the hook leaves it, and its report of a handler it cannot
rebuild goes beneath ``sys.__stderr__``'s buffer onto the pipe.

Run as ``python fork_scenarios.py <scenario> [<held>]``: a scenario that takes the held stream
reads it as the name of a ``_Held`` member.
"""

from __future__ import annotations

import codecs
import json
import logging
import multiprocessing
import os
import re
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TextIO

from tai42_kit.logging import fork_safety
from tai42_kit.logging.fork_report_text import _headline
from tai42_kit.logging.fork_safety import install_logging_fork_safety

# The line the writer thread is inside when the door forks: far larger than any pipe's buffer,
# so the writer blocks in ``write(2)`` holding its stream's buffer lock until the pipe is drained.
_PAYLOAD = "W" * (1 << 20)
# The line the child logs through the handler whose stream lock the parent's writer holds.
_CHILD_LINE = "child line"
# The line the finalising child prints to ``sys.stderr``.
_CHILD_PRINT = "child print"
# Budget for the writer to fill the pipe, for the child to exit, and for the writer to finish once
# the pipe drains (generous: each takes milliseconds).
_BUDGET = 10.0
# Poll interval for the conditions with no event to wait on.
_POLL = 0.001
# One report: its headline, the indented line of each failure, and the newline that ends it; a
# fresh encoding of a stream that starts with a byte-order mark puts one before it.
_REPORT = re.compile("\ufeff?" + re.escape(_headline(fork_safety.__name__)) + r"(?:\n  [^\n]*)*\n")

Result = dict[str, object]


class _Held(Enum):
    """The stream whose buffer lock the writer holds at the fork, and how the logger reaches it."""

    FILE_STREAM = "a handler on a file stream over the pipe"
    STDERR = "a handler on sys.stderr, moved onto the pipe"
    STDERR_THROUGH_LAST_RESORT = "logging.lastResort, which writes to sys.stderr, moved onto the pipe"
    STDERR_BUFFER_THROUGH_A_CODECS_WRITER = (
        "logging.lastResort, which writes to a codecs writer bound to sys.stderr over the buffer of"
        " sys.__stderr__, moved onto the pipe"
    )
    STDERR_BUFFER_THROUGH_A_PROXY = (
        "logging.lastResort, which writes to a proxy bound to sys.stderr that forwards to sys.__stderr__,"
        " moved onto the pipe"
    )


# The held streams the logger reaches through ``logging.lastResort``, having no handler of its own.
_THROUGH_LAST_RESORT = (
    _Held.STDERR_THROUGH_LAST_RESORT,
    _Held.STDERR_BUFFER_THROUGH_A_CODECS_WRITER,
    _Held.STDERR_BUFFER_THROUGH_A_PROXY,
)


class _ForwardingProxy:
    """A ``sys.stderr`` that is no stream of its own: each ``write`` and ``flush`` goes to ``target``."""

    def __init__(self, target: TextIO) -> None:
        self._target = target

    def write(self, text: str) -> int:
        return self._target.write(text)

    def flush(self) -> None:
        self._target.flush()


@dataclass
class _Child:
    """A child the door started: its pid, and how to read its exit code without blocking."""

    pid: int
    exit_code: Callable[[], int | None]


def _waitpid_exit_code(pid: int) -> Callable[[], int | None]:
    """A non-blocking reader of ``pid``'s exit code, for a child made with a bare ``os.fork``."""
    reaped: list[int] = []

    def exit_code() -> int | None:
        if not reaped:
            done, status = os.waitpid(pid, os.WNOHANG)
            if not done:
                return None
            reaped.append(os.waitstatus_to_exitcode(status))
        return reaped[0]

    return exit_code


def _held_stream_into(write_fd: int, held: _Held, encoding: str | None) -> TextIO:
    """The stream the writer thread holds: a fresh one over ``write_fd``, or ``sys.stderr`` moved onto it.

    ``sys.stderr`` keeps its own stream unless ``encoding`` is given: it is then reopened over its
    descriptor in that encoding.
    """
    if held is _Held.FILE_STREAM:
        # ``open`` over a descriptor: the text-over-buffered-over-file stream a ``FileHandler`` writes to.
        return os.fdopen(write_fd, "w", encoding="utf-8")
    if held in (_Held.STDERR_BUFFER_THROUGH_A_CODECS_WRITER, _Held.STDERR_BUFFER_THROUGH_A_PROXY):
        dunder = sys.__stderr__
        assert dunder is not None
        os.dup2(write_fd, dunder.fileno())
        os.close(write_fd)
        if held is _Held.STDERR_BUFFER_THROUGH_A_CODECS_WRITER:
            sys.stderr = codecs.getwriter(dunder.encoding)(dunder.buffer)  # pyright: ignore[reportAttributeAccessIssue, reportArgumentType]
        else:
            sys.stderr = _ForwardingProxy(dunder)  # pyright: ignore[reportAttributeAccessIssue]
        return dunder
    os.dup2(write_fd, sys.stderr.fileno())
    os.close(write_fd)
    if encoding is not None:
        sys.stderr = open(sys.stderr.fileno(), "w", encoding=encoding, closefd=False)  # noqa: SIM115
    return sys.stderr


def _start_the_writer(log: logging.Logger, stream: TextIO) -> threading.Thread:
    """Start a thread logging ``_PAYLOAD`` and return once it is blocked inside ``stream``'s write."""
    writer = threading.Thread(target=log.warning, args=(_PAYLOAD,), daemon=True)
    writer.start()
    deadline = time.monotonic() + _BUDGET
    while True:
        _, writable, _ = select.select([], [stream.fileno()], [], 0)
        if not writable and writer.is_alive():
            return writer
        if time.monotonic() > deadline:
            raise RuntimeError("the writer thread never filled the pipe")
        time.sleep(_POLL)


class _Drain:
    """Reads the pipe on its own thread, from the moment the door forks until it is stopped.

    The parent must drain while the door is still running (``Popen`` waits for a child whose
    ``preexec_fn`` writes into the full pipe), yet not before the fork, or the writer would leave
    its write before the fork happens. A parent-side fork hook starts the reading.
    """

    def __init__(self, read_fd: int) -> None:
        self._read_fd = read_fd
        self._data = bytearray()
        self._forked = threading.Event()
        self._stopped = threading.Event()
        os.register_at_fork(after_in_parent=self._forked.set)
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        self._forked.wait()
        while not self._stopped.is_set():
            if select.select([self._read_fd], [], [], _POLL)[0]:
                self._data += os.read(self._read_fd, 1 << 16)
        # Every writer is done: what they wrote is all in the pipe now.
        while select.select([self._read_fd], [], [], 0)[0]:
            self._data += os.read(self._read_fd, 1 << 16)

    def stop(self) -> bytes:
        """Stop reading once the pipe is empty and return every byte read."""
        self._stopped.set()
        self._thread.join()
        return bytes(self._data)


def _exit_code_within_budget(child: _Child) -> int | None:
    """The child's exit code, or ``None`` for a child still alive at the budget, which is killed and reaped."""
    deadline = time.monotonic() + _BUDGET
    while time.monotonic() < deadline:
        exit_code = child.exit_code()
        if exit_code is not None:
            return exit_code
        time.sleep(_POLL)
    os.kill(child.pid, signal.SIGKILL)
    while child.exit_code() is None:
        time.sleep(_POLL)
    return None


def _fork_while_a_writer_holds_the_lock(
    door: Callable[[logging.Logger], _Child], held: _Held, encoding: str | None = None
) -> Result:
    """Fork through ``door`` while a writer holds the buffer lock of the stream the logger writes to.

    The logger does not propagate, so its records reach the held stream through its own handler,
    or, having none, through ``logging.lastResort``. ``encoding``, when given, is the encoding
    ``sys.stderr`` is reopened in.
    """
    install_logging_fork_safety()
    read_fd, write_fd = os.pipe()
    stream = _held_stream_into(write_fd, held, encoding)
    log = logging.getLogger("held")
    log.propagate = False
    if held not in _THROUGH_LAST_RESORT:
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(handler)
    drain = _Drain(read_fd)
    writer = _start_the_writer(log, stream)
    child = door(log)
    exit_code = _exit_code_within_budget(child)
    writer.join(_BUDGET)
    # The child's short writes are atomic, but may land between two writes of the payload.
    data = drain.stop().decode(stream.encoding)
    child_line, child_print = f"{_CHILD_LINE}\n", f"{_CHILD_PRINT}\n"
    reports = _REPORT.findall(data)
    return {
        "child_exit": exit_code,
        "child_lines": data.count(child_line),
        "child_prints": data.count(child_print),
        "reports": [report.removeprefix("\ufeff") for report in reports],
        "writer_finished": not writer.is_alive(),
        "payload_written_once": _REPORT.sub("", data).replace(child_line, "").replace(child_print, "")
        == f"{_PAYLOAD}\n",
    }


def _os_fork_child(in_child: Callable[[], None]) -> _Child:
    """Fork with ``os.fork``; the child runs ``in_child``, which ends the child."""
    pid = os.fork()
    if pid == 0:
        in_child()
    return _Child(pid, _waitpid_exit_code(pid))


def _log_the_child_line_and_exit(log: logging.Logger) -> None:
    log.warning(_CHILD_LINE)
    os._exit(0)


def _log_print_and_finalise(log: logging.Logger) -> None:
    log.warning(_CHILD_LINE)
    print(_CHILD_PRINT, file=sys.stderr)
    # The interpreter's own exit: flushes and finalises every stream, the inherited ones included.
    sys.exit(0)


def a_child_os_fork_makes_logs_and_exits() -> Result:
    """``os.fork``; the child logs through the held handler and leaves with ``os._exit``."""
    return _fork_while_a_writer_holds_the_lock(
        lambda log: _os_fork_child(lambda: _log_the_child_line_and_exit(log)), _Held.FILE_STREAM
    )


def a_child_os_fork_makes_logs_prints_and_finalises() -> Result:
    """``os.fork`` while the held stream is ``sys.stderr``; the child logs, prints and ends with ``sys.exit``."""
    return _fork_while_a_writer_holds_the_lock(
        lambda log: _os_fork_child(lambda: _log_print_and_finalise(log)), _Held.STDERR
    )


def a_subprocess_logs_from_its_preexec_fn() -> Result:
    """``subprocess`` with a ``preexec_fn`` that logs through the held handler; ``Popen`` must return."""

    def door(log: logging.Logger) -> _Child:
        process = subprocess.Popen([sys.executable, "-c", ""], preexec_fn=lambda: log.warning(_CHILD_LINE))
        return _Child(process.pid, process.poll)

    return _fork_while_a_writer_holds_the_lock(door, _Held.FILE_STREAM)


def a_fork_context_process_logs_and_returns() -> Result:
    """A ``multiprocessing`` fork-context ``Process`` whose target logs through the held handler and returns."""

    def door(log: logging.Logger) -> _Child:
        process = multiprocessing.get_context("fork").Process(target=log.warning, args=(_CHILD_LINE,))
        process.start()
        assert process.pid is not None
        return _Child(process.pid, lambda: process.exitcode)

    return _fork_while_a_writer_holds_the_lock(door, _Held.FILE_STREAM)


def a_child_logs_through_last_resort() -> Result:
    """The only writer is ``logging.lastResort`` on ``sys.stderr``; the child logs through it too."""
    return _fork_while_a_writer_holds_the_lock(
        lambda log: _os_fork_child(lambda: _log_the_child_line_and_exit(log)), _Held.STDERR_THROUGH_LAST_RESORT
    )


def a_child_os_fork_exits_while_another_thread_holds_a_stderr_it_cannot_rebuild() -> Result:
    """``os.fork`` while ``lastResort`` holds a utf-16 ``sys.stderr``; the child leaves with ``os._exit`` at once."""
    return _fork_while_a_writer_holds_the_lock(
        lambda log: _os_fork_child(lambda: os._exit(0)), _Held.STDERR_THROUGH_LAST_RESORT, "utf-16"
    )


def a_subprocess_starts_while_another_thread_holds_a_stderr_it_cannot_rebuild() -> Result:
    """``subprocess`` with a ``preexec_fn`` that writes nothing while ``lastResort`` holds a utf-16 ``sys.stderr``."""

    def door(log: logging.Logger) -> _Child:
        process = subprocess.Popen([sys.executable, "-c", ""], preexec_fn=lambda: None)
        return _Child(process.pid, process.poll)

    return _fork_while_a_writer_holds_the_lock(door, _Held.STDERR_THROUGH_LAST_RESORT, "utf-16")


# A handler the hook cannot rebuild, a stream over a socket, so the child's hook has a failure to
# report, and its socket pair: each lives for the whole process, the handler in logging's registry of
# live handlers.
_KEPT_ALIVE: list[object] = []


def _a_handler_over_a_socket() -> None:
    left, right = socket.socketpair()
    _KEPT_ALIVE.extend([left, right, logging.StreamHandler(left.makefile("w", encoding="utf-8"))])


def a_child_os_fork_exits_while_another_thread_holds_the_buffer_beneath_stderr(held: str) -> Result:
    """``os.fork`` while ``lastResort`` holds the buffer ``held``'s ``sys.stderr`` writes into; the child exits."""
    _a_handler_over_a_socket()
    return _fork_while_a_writer_holds_the_lock(lambda log: _os_fork_child(lambda: os._exit(0)), _Held[held])


def a_subprocess_starts_while_another_thread_holds_the_buffer_beneath_stderr(held: str) -> Result:
    """``subprocess`` with a ``preexec_fn`` that writes nothing while ``lastResort`` holds that buffer."""
    _a_handler_over_a_socket()

    def door(log: logging.Logger) -> _Child:
        process = subprocess.Popen([sys.executable, "-c", ""], preexec_fn=lambda: None)
        return _Child(process.pid, process.poll)

    return _fork_while_a_writer_holds_the_lock(door, _Held[held])


_SCENARIOS: dict[str, Callable[..., Result]] = {
    scenario.__name__: scenario
    for scenario in (
        a_child_os_fork_makes_logs_and_exits,
        a_child_os_fork_makes_logs_prints_and_finalises,
        a_subprocess_logs_from_its_preexec_fn,
        a_fork_context_process_logs_and_returns,
        a_child_logs_through_last_resort,
        a_child_os_fork_exits_while_another_thread_holds_a_stderr_it_cannot_rebuild,
        a_subprocess_starts_while_another_thread_holds_a_stderr_it_cannot_rebuild,
        a_child_os_fork_exits_while_another_thread_holds_the_buffer_beneath_stderr,
        a_subprocess_starts_while_another_thread_holds_the_buffer_beneath_stderr,
    )
}


def main() -> None:
    # Every scenario forks while another thread runs, on purpose.
    warnings.filterwarnings("ignore", message=r"This process .* is multi-threaded", category=DeprecationWarning)
    name, *arguments = sys.argv[1:]
    # The writer thread may still be inside logging, so the interpreter's exit-time logging
    # shutdown is skipped: the result or the traceback is written to stdout (``sys.stderr`` may be
    # the pipe), then the process exits. A child that ends with ``sys.exit`` raises ``SystemExit``
    # out of the scenario and leaves through the interpreter's own exit, which is what it exercises.
    try:
        result = _SCENARIOS[name](*arguments)
    except Exception:
        os.write(sys.stdout.fileno(), traceback.format_exc().encode())
        os._exit(1)
    os.write(sys.stdout.fileno(), json.dumps(result).encode())
    os._exit(0)


if __name__ == "__main__":
    main()
