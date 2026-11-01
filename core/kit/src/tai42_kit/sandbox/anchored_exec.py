"""The anchored-exec bootstrap: the program every host exec starts under, and its channels to the kit.

A host exec does not start its command directly. It starts the kit's own interpreter on
:data:`ANCHORED_EXEC`, which forks the exec's group anchor and only then replaces itself with
the command (:func:`bootstrap_argv`). The bootstrap takes the command's env as data on a pipe
(:func:`encode_env`), which the event loop writes (:class:`PipeFeed`), so the command gets
the env the caller passed byte for byte, whatever the interpreter does to its own env at its
start. It reports a failure before the command
runs on a second pipe, which the kit reads to its end (:func:`read_to_eof`) and turns into
the error the failure stands for (:func:`spawn_failure`).
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Mapping, Sequence

from tai42_contract.sandbox import SandboxError

# The bootstrap every exec starts under: ``python -I -S -c ANCHORED_EXEC <hold fd> <report fd>
# <env fd> <argv...>``, run by the kit's own interpreter with an empty env. It first closes every fd
# but the exec's three pipes and those three, so neither the anchor nor the command holds an fd the
# event loop that spawned it left open in it: uvloop leaves a copy of each of the exec's pipes open
# past the three, and an anchor holding the output pipes would keep the output from ending at the
# command's exit. It closes only the fds ``/proc/self/fd`` lists, so the cost follows the fds open
# and not the open-files limit, which may be high enough that closing every number below it takes
# minutes. The listing holds an fd of its own while it reads the directory, the lowest free number
# as every new fd takes, and it closes that fd before the list returns: the bootstrap learns the
# number by opening and closing the same directory just before and keeps it out of the closing.
# It then reads the command's env off the env fd to its end, failing when the pipe
# closed before the env's own end (so an env cut short never starts the command), ignores every
# catchable signal (an ignored signal stays ignored across fork and exec, so the anchor is immune
# from its first instant), forks the group anchor through a middle process that exits at once (so
# the anchor is no child of the command), waits until the anchor runs, then closes the hold, gives
# the command the signal dispositions it started with and replaces itself with the command,
# ``argv[0]`` looked up along the command's own ``PATH``. The anchor leaves the exec's cwd, swaps
# the exec's pipes for the hold on its stdin and ``/dev/null``, and becomes an ``sh`` blocked
# reading the hold until the kit lets go.
#
# The dispositions given back are the ones an exec leaves: a signal ignored at the start stays
# ignored and every other one is at its default, the interpreter's own ``SIGINT`` handler among
# them, so no Python handler runs between them and the exec and a signal there acts as it would
# on the command. ``SIGPIPE`` and ``SIGXFSZ``, which the interpreter ignores at its own start, go
# back to their defaults, as ``subprocess`` restores them for every child. ``os.exec*`` refuses an
# empty program name, which ``subprocess`` looks up along the ``PATH`` like any other: every path
# that lookup tries names a directory, which no exec runs, so the bootstrap makes the same lookup
# under a stand-in name and fails with the error ``subprocess`` gives.
#
# The report fd carries failures to the kit, as ``subprocess`` carries a child's exec error: it is
# closed on exec, so the kit reading it to EOF with nothing on it means the command was exec'd. A
# failure of any kind before that writes one report and exits without running the command:
# ``exec:<errno>`` when the host refused the command's exec, ``setup:<stage>:<error>`` for every
# other failure (the anchor's start among them). The anchor reports its own start the same way on
# a pipe of the bootstrap's, whose EOF is the anchor's ``sh`` running.
ANCHORED_EXEC = """\
import os, signal, sys
hold, report, env_fd = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
argv = sys.argv[4:]
def failed(stage, exc):
    return f"setup:{stage}:{type(exc).__name__}: {exc}".encode()
stage = "preparing the bootstrap"
try:
    listing = os.open("/proc/self/fd", os.O_RDONLY | os.O_DIRECTORY)
    os.close(listing)
    kept = {0, 1, 2, hold, report, env_fd, listing}
    for fd in map(int, os.listdir("/proc/self/fd")):
        if fd not in kept:
            os.close(fd)
    os.set_inheritable(report, False)
    stage = "reading the command's env"
    encoded = b""
    while chunk := os.read(env_fd, 65536):
        encoded += chunk
    os.close(env_fd)
    entries = encoded.split(b"\\0")
    if entries[-2:] != [b"", b""]:
        raise EOFError("the env pipe closed before the env's end")
    env = dict(entry.split(b"=", 1) for entry in entries[:-2])
    stage = "preparing the bootstrap"
    started = {
        sig: signal.getsignal(sig)
        for sig in signal.valid_signals() - {signal.SIGKILL, signal.SIGSTOP, signal.SIGCHLD}
    }
    for sig in started:
        signal.signal(sig, signal.SIG_IGN)
    anchor_report, anchor_report_end = os.pipe()
    stage = "forking the anchor's parent"
    middle = os.fork()
    if middle == 0:
        stage = "forking the anchor"
        try:
            if os.fork() == 0:
                stage = "starting the anchor"
                os.chdir("/")
                os.dup2(hold, 0)
                os.close(hold)
                null = os.open(os.devnull, os.O_WRONLY)
                os.dup2(null, 1)
                os.dup2(null, 2)
                os.execv("/bin/sh", ["sh", "-c", "read -r _"])
        except BaseException as exc:
            os.write(anchor_report_end, failed(stage, exc))
        os._exit(0)
    os.close(anchor_report_end)
    stage = "waiting for the anchor"
    os.waitpid(middle, 0)
    anchor_failure = b""
    while chunk := os.read(anchor_report, 4096):
        anchor_failure += chunk
    if anchor_failure:
        os.write(report, anchor_failure)
        os._exit(1)
    stage = "preparing the command's start"
    os.close(anchor_report)
    os.close(hold)
    for sig, disposition in started.items():
        signal.signal(sig, signal.SIG_IGN if disposition == signal.SIG_IGN else signal.SIG_DFL)
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    signal.signal(signal.SIGXFSZ, signal.SIG_DFL)
    stage = "starting the command"
    if argv[0]:
        os.execvpe(argv[0], argv, env)
    os.execvpe("", ["-", *argv[1:]], env)
except BaseException as exc:
    if stage == "starting the command" and isinstance(exc, OSError):
        os.write(report, f"exec:{exc.errno}".encode())
    else:
        os.write(report, failed(stage, exc))
    os._exit(1)
"""


def bootstrap_argv(argv: Sequence[str], *, hold_fd: int, report_fd: int, env_fd: int) -> list[str]:
    """The argv that starts ``argv`` under the bootstrap, its three fds passed by number."""
    return [sys.executable, "-I", "-S", "-c", ANCHORED_EXEC, str(hold_fd), str(report_fd), str(env_fd), *argv]


def encode_env(env: Mapping[str, str]) -> bytes:
    """``env`` as the bootstrap reads it: each name and value as the host's bytes (``os.fsencode``).

    Each ``name=value`` entry ends with a NUL and one more NUL ends the env, so the bootstrap
    tells an env whose pipe closed early from a whole one.

    Raises :class:`ValueError`, with nothing started, for an env the host cannot hold whole:
    both a ``"PATH"`` and a ``b"PATH"`` key (``Popen``'s own error), a name that is empty or
    holds ``=`` (``illegal environment variable name``), a name or value holding a NUL
    (``embedded null byte``), and two names that encode to the same bytes, which the host holds
    as one name.
    """
    if "PATH" in env and b"PATH" in env:
        raise ValueError("env cannot contain 'PATH' and b'PATH' keys")
    names: dict[bytes, str] = {}
    entries: list[bytes] = []
    for name, value in env.items():
        name_bytes, value_bytes = os.fsencode(name), os.fsencode(value)
        if not name_bytes or b"=" in name_bytes:
            raise ValueError("illegal environment variable name")
        if b"\0" in name_bytes or b"\0" in value_bytes:
            raise ValueError("embedded null byte")
        if name_bytes in names:
            raise ValueError(
                f"env names {names[name_bytes]!r} and {name!r} are the same name on the host: {name_bytes!r}"
            )
        names[name_bytes] = name
        entries.append(name_bytes + b"=" + value_bytes + b"\0")
    return b"".join(entries) + b"\0"


class PipeFeed(asyncio.BaseProtocol):
    """``data`` written to a pipe's write end by the event loop, the end closed once all of it was written."""

    def __init__(self, closed: asyncio.Future[Exception | None]) -> None:
        """Bind the future the feed's close resolves with: ``None`` when it wrote everything, else why it stopped."""
        self._closed = closed
        self._transport: asyncio.WriteTransport | None = None
        # Why the feed stopped short when :meth:`abort` dropped data it had still to write.
        self._aborted: ConnectionAbortedError | None = None

    @classmethod
    async def start(cls, fd: int, data: bytes) -> PipeFeed:
        """Start writing ``data`` to the pipe write end ``fd``, which the feed owns from here on.

        ``fd`` is closed once all of ``data`` was written, once the reader closed its end, on
        :meth:`abort`, and when the start itself fails or is cancelled.
        """
        loop = asyncio.get_running_loop()
        closed: asyncio.Future[Exception | None] = loop.create_future()
        pipe = os.fdopen(fd, "wb", buffering=0)
        try:
            transport, feed = await loop.connect_write_pipe(lambda: cls(closed), pipe)
        except BaseException:
            pipe.close()
            raise
        feed._transport = transport
        transport.write(data)
        transport.close()
        return feed

    def connection_lost(self, exc: Exception | None) -> None:
        """Record how the write ended: ``None`` with everything written, else the write's or the abort's error."""
        if not self._closed.done():
            self._closed.set_result(exc or self._aborted)

    async def closed(self) -> Exception | None:
        """Wait until the write end is closed: ``None`` when it wrote everything, else why it stopped."""
        return await asyncio.shield(self._closed)

    def abort(self) -> None:
        """Drop what is left unwritten and close the write end at the next loop pass; a closed feed stays closed.

        A feed that still had data to write closes with a :class:`ConnectionAbortedError`; one
        that had written everything closes with ``None``, as it would have.
        """
        if self._transport is None or self._closed.done():
            return
        if self._transport.get_write_buffer_size():
            self._aborted = ConnectionAbortedError("the feed was aborted before it wrote everything")
        self._transport.abort()


def spawn_failure(report: bytes, argv0: str) -> OSError | SandboxError:
    """The error a bootstrap's failure ``report`` stands for.

    A failed exec gives the :class:`OSError` ``Popen`` raises for it; a failed step before the
    exec gives a :class:`SandboxError` naming the step.
    """
    kind, _, detail = report.decode("utf-8", "replace").partition(":")
    if kind == "exec":
        code = int(detail)
        return OSError(code, os.strerror(code), argv0)
    stage, _, error = detail.partition(":")
    return SandboxError(f"sandbox exec could not start {argv0!r}: its bootstrap failed while {stage}: {error}")


async def read_to_eof(fd: int) -> bytes:
    """Everything written to the pipe read end ``fd`` until its last writer closes it; ``fd`` is closed."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    pipe = os.fdopen(fd, "rb", buffering=0)
    try:
        transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), pipe)
    except BaseException:
        pipe.close()
        raise
    try:
        return await reader.read()
    finally:
        transport.close()
