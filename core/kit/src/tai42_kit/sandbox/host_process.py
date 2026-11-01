"""A sandbox exec run as a host child process: the process, its group and its kill.

For a provider whose execs are host processes. The child is spawned with
``start_new_session=True``, so its pid is its process-group id and one ``killpg``
reaches every descendant that stayed in the group. A group whose leader has exited keeps
its id only while a member lives, so the kit keeps a member of its own in every exec's
group: a small ``sh`` anchor, running before the command starts (a command whose anchor
cannot start never runs), that holds none of the exec's pipes, ignores every catchable
signal and waits on a socket the kit holds. The bootstrap that starts both is
:mod:`~tai42_kit.sandbox.anchored_exec`. While the kit holds that socket the group is the
exec's and a kill may signal it; the kit lets go when it kills the group or when a one-shot
run has completed, and the anchor ends with the hold. A descendant that left the group (a
``setsid`` daemon) can keep the child's stdout/stderr open long after the child is gone, so
:meth:`HostProcess.kill` never waits for those pipes to reach EOF: once the child itself
has exited it closes the pipe transports, and the output ends at once. The exec doors over
a host process are in :mod:`~tai42_kit.sandbox.host_exec`.
"""

from __future__ import annotations

import array
import asyncio
import contextlib
import fcntl
import logging
import os
import select
import signal
import socket
import termios
from collections.abc import Iterator, Sequence
from typing import Any, cast

from tai42_contract.sandbox import SandboxError, SandboxStreamChunk

from tai42_kit.sandbox.anchored_exec import PipeFeed, bootstrap_argv, encode_env, read_to_eof, spawn_failure

logger = logging.getLogger(__name__)

_STDIN_FD = 0
_STDOUT_FD = 1
_STDERR_FD = 2

# The output buffer's back-pressure bounds, as asyncio's ``StreamReader`` sets them: past the
# high-water mark the output pipes stop being read, so a child that writes faster than its output
# is read blocks on its own pipe; once the reader brings the buffer down to the low-water mark,
# reading resumes.
_OUTPUT_LOW_WATER = 64 * 1024
_OUTPUT_HIGH_WATER = 2 * _OUTPUT_LOW_WATER

# How long a kill that signalled the exec's group waits, at most, for the processes it signalled
# to have ended before it closes the pipes and tells whether a process still holds the output,
# and how often it looks: a SIGKILLed process closes its pipes as it ends, an instant after the
# signal, and a process outside the group is not waited for.
_GROUP_END_WAIT_SECONDS = 2.0
_GROUP_END_POLL_SECONDS = 0.01

# Why a write into the child's stdin failed: the pipe is closed, as once the child has closed its
# stdin or exited.
_STDIN_PIPE_CLOSED = "the exec's stdin pipe is closed"


def _pipe_end(pipe: asyncio.BaseTransport) -> Any:
    """The kit's end of the pipe a transport of the child's stdio wraps.

    The event loop backs the child's stdio with pipes (asyncio's own loop) or Unix socket pairs
    (uvloop); the transport names its end under its ``pipe`` or its ``socket`` extra.
    """
    return pipe.get_extra_info("pipe") or pipe.get_extra_info("socket")


def _has_a_reader(pipe: asyncio.WriteTransport) -> bool:
    """Whether some process still holds the read end of the pipe ``pipe`` writes to.

    The write end polls as an error, or a hang-up, once no read end is open.
    """
    poller = select.poll()
    poller.register(_pipe_end(pipe).fileno(), select.POLLOUT)
    return not any(events & (select.POLLERR | select.POLLHUP) for _fd, events in poller.poll(0))


def _has_a_writer(pipe: asyncio.ReadTransport) -> bool:
    """Whether some process still holds the write end of the pipe ``pipe`` reads.

    The read end polls as a hang-up once no write end is open, whether or not bytes are left
    unread in it.
    """
    poller = select.poll()
    poller.register(_pipe_end(pipe).fileno(), select.POLLIN)
    return not any(events & select.POLLHUP for _fd, events in poller.poll(0))


def _unread_bytes(pipe: asyncio.ReadTransport) -> int:
    """How many bytes are left unread in the pipe ``pipe`` reads."""
    count = array.array("i", [0])
    fcntl.ioctl(_pipe_end(pipe).fileno(), termios.FIONREAD, count)
    return count[0]


def _stdin_transport(transport: asyncio.SubprocessTransport) -> asyncio.WriteTransport | None:
    """The write transport of the child's stdin, or ``None`` when it was spawned without one.

    Read by its presence, not its class: uvloop's pipe transports implement asyncio's transport
    interface without deriving from its classes.
    """
    return cast("asyncio.WriteTransport | None", transport.get_pipe_transport(_STDIN_FD))


class _PipeProtocol(asyncio.SubprocessProtocol):
    """Buffers the child's stdout/stderr chunks, then ``None`` once both pipes and the child are done.

    The buffer is bounded: past :data:`_OUTPUT_HIGH_WATER` unread bytes the output pipes are paused
    until :meth:`take` brings it down to :data:`_OUTPUT_LOW_WATER`.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._items: asyncio.Queue[SandboxStreamChunk | None] = asyncio.Queue()
        self._buffered = 0
        self._reading_paused = False
        self.exited: asyncio.Future[None] = loop.create_future()
        self.stdout_len = 0
        self.stderr_len = 0
        self.stdin_lost: Exception | None = None
        self.stdin_gone: asyncio.Future[None] = loop.create_future()
        self._loop = loop
        self._transport: asyncio.SubprocessTransport | None = None
        self._open_outputs = {_STDOUT_FD, _STDERR_FD}
        self._ended = False
        # Set once the kit closed the pipes at the child's exit, after a kill.
        self._closed_at_exit = False
        # Whether some process still held an output pipe as the kit closed the pipes at the
        # child's exit: output it wrote after the close is not counted.
        self.output_held_at_close = False
        self._write_resumed: asyncio.Future[None] | None = None
        self._close_at_exit = False
        self._killed = False
        self._stdin_cut: SandboxError | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        # ``loop.subprocess_exec`` connects this protocol to its subprocess transport.
        self._transport = cast("asyncio.SubprocessTransport", transport)

    def pipe_data_received(self, fd: int, data: bytes) -> None:
        if fd == _STDOUT_FD:
            self.stdout_len += len(data)
            self._items.put_nowait(SandboxStreamChunk(stream="stdout", data=data))
        else:
            self.stderr_len += len(data)
            self._items.put_nowait(SandboxStreamChunk(stream="stderr", data=data))
        self._buffered += len(data)
        if not self._reading_paused and self._buffered > _OUTPUT_HIGH_WATER:
            self._reading_paused = True
            self._set_output_reading(paused=True)

    async def take(self) -> SandboxStreamChunk | None:
        """The next buffered chunk, or ``None`` at the end; resumes paused reading once the buffer drains.

        Raises at the end, instead of returning ``None``, when the child's exit cut its stdin input.
        """
        item = await self._items.get()
        if item is None and self._stdin_cut is not None:
            raise self._stdin_cut
        if item is not None:
            self._buffered -= len(item.data)
            if self._reading_paused and self._buffered <= _OUTPUT_LOW_WATER:
                self._reading_paused = False
                self._set_output_reading(paused=False)
        return item

    def _set_output_reading(self, *, paused: bool) -> None:
        if self._transport is None:  # pragma: no cover - defensive; data arrives only once connected
            return
        for fd in (_STDOUT_FD, _STDERR_FD):
            pipe = cast("asyncio.ReadTransport | None", self._transport.get_pipe_transport(fd))
            # A pipe that is closing ignores both calls.
            if pipe is not None:
                if paused:
                    pipe.pause_reading()
                else:
                    pipe.resume_reading()

    def pipe_connection_lost(self, fd: int, exc: Exception | None) -> None:
        if fd == _STDIN_FD:
            self.stdin_lost = exc or ConnectionResetError(_STDIN_PIPE_CLOSED)
            if not self.stdin_gone.done():
                self.stdin_gone.set_result(None)
            self._wake_writer()
            return
        self._open_outputs.discard(fd)
        self._maybe_end()

    def process_exited(self) -> None:
        if not self.exited.done():
            self.exited.set_result(None)
        self._abort_stdin()
        if self._close_at_exit:
            self._close_counting_unread()
        self._maybe_end()

    def _abort_stdin(self) -> None:
        """Close the exited child's stdin now, its unwritten bytes dropped, so no write waits on it.

        A descendant may hold the stdin pipe without reading it; a graceful close would wait on
        it to take what is buffered. A pipe already closing with nothing left to write closes on
        its own. Bytes dropped while a process still holds the pipe's read end, at an exit the
        kit's kill did not cause, are input the exec never got: the output's end raises it.
        """
        if self._transport is None:  # pragma: no cover - defensive; the exit is reported only once connected
            return
        stdin = _stdin_transport(self._transport)
        if stdin is None:
            return
        unwritten = stdin.get_write_buffer_size()
        if unwritten and not self._killed and _has_a_reader(stdin):
            self._stdin_cut = SandboxError(
                f"sandbox exec stdin was cut: the exec exited with code {self._transport.get_returncode()} "
                f"while a process it started still held its stdin, with {unwritten} bytes of its input unwritten"
            )
        if not stdin.is_closing() or unwritten:
            stdin.abort()

    def note_kill(self) -> None:
        """Record that the kit is killing the child: input its exit leaves unwritten was not cut by the exec."""
        self._killed = True

    def close_at_exit(self) -> None:
        """Close the transport, and with it every pipe, once the child has exited: at once if it has."""
        self._close_at_exit = True
        if self.exited.done():
            self._close_counting_unread()

    def _close_counting_unread(self) -> None:
        """Close the transport, and with it every pipe, counting what the output pipes held unread as output given."""
        if self._closed_at_exit or self._transport is None:
            return
        self.output_held_at_close = self.output_held()
        for fd, pipe in self._open_output_pipes():
            unread = _unread_bytes(pipe)
            if fd == _STDOUT_FD:
                self.stdout_len += unread
            else:
                self.stderr_len += unread
        self._closed_at_exit = True
        self._transport.close()

    def _open_output_pipes(self) -> Iterator[tuple[int, asyncio.ReadTransport]]:
        """Each output pipe not at its end, by its fd, while the kit has not closed it."""
        if self._transport is None or self._closed_at_exit:
            return
        for fd in sorted(self._open_outputs):
            pipe = cast("asyncio.ReadTransport | None", self._transport.get_pipe_transport(fd))
            if pipe is not None and not pipe.is_closing():
                yield fd, pipe

    def output_held(self) -> bool:
        """Whether some process still holds the write end of an output pipe not at its end."""
        return any(_has_a_writer(pipe) for _fd, pipe in self._open_output_pipes())

    def pause_writing(self) -> None:
        if self._write_resumed is None:
            self._write_resumed = self._loop.create_future()

    def resume_writing(self) -> None:
        self._wake_writer()

    async def drain(self) -> None:
        """Wait until the stdin pipe takes more bytes; raises once that pipe is lost."""
        if self._write_resumed is not None:
            await asyncio.shield(self._write_resumed)
        if self.stdin_lost is not None:
            # The pipe's own error, often a bare ``BrokenPipeError`` with no text, stays the cause.
            raise SandboxError(f"sandbox exec stdin write failed: {_STDIN_PIPE_CLOSED}") from self.stdin_lost

    def _wake_writer(self) -> None:
        waiter, self._write_resumed = self._write_resumed, None
        if waiter is not None and not waiter.done():
            waiter.set_result(None)

    def _maybe_end(self) -> None:
        if self._ended or self._open_outputs or not self.exited.done():
            return
        self._ended = True
        self._items.put_nowait(None)
        if self._transport is not None:
            self._transport.close()


class HostProcess:
    """One spawned host child: stdin writes, its output chunks, and a kill that never waits on pipes."""

    def __init__(self, transport: asyncio.SubprocessTransport, protocol: _PipeProtocol, hold: socket.socket) -> None:
        """Bind a started child's subprocess ``transport``, its pipe ``protocol`` and the ``hold`` on its group."""
        self._transport = transport
        self._protocol = protocol
        # The kit's end of the socket its group anchor waits on; ``None`` once the group is released.
        self._hold: socket.socket | None = hold
        # Set once this side closed the child's stdin, so a pipe lost after that is not the child's doing.
        self._stdin_closed_here = False

    @classmethod
    async def spawn(cls, argv: Sequence[str], *, cwd: str, env: dict[str, str], stdin: bool) -> HostProcess:
        """Start ``argv`` in its own session with the kit's group anchor beside it.

        stdout and stderr are always piped, stdin when asked. The command runs with exactly
        ``env``, nothing added.

        The spawn returns once the command was exec'd, the anchor already running in its
        group. An empty ``argv``, or an ``env`` the host cannot hold whole (as
        :func:`~tai42_kit.sandbox.anchored_exec.encode_env` lists), raises :class:`ValueError`
        before anything starts. A command the host cannot exec raises the
        :class:`OSError` ``Popen`` would raise for it (``argv[0]`` resolved as ``Popen``
        resolves it: a name with a directory part against ``cwd``, a bare name along the
        ``PATH`` in ``env``, each entry from ``cwd``); an anchor that could not be started
        raises :class:`SandboxError`. Either way the command never runs. A child started
        without stdin reads EOF from ``/dev/null``, never the host's own stdin. The command
        sees its own argv and the exec's three pipes, nothing of the anchor.
        """
        if not argv:
            raise ValueError("a host exec needs a program: argv is empty")
        encoded_env = encode_env(env)
        loop = asyncio.get_running_loop()
        # Every fd made here is closed when a later step fails, the bootstrap's ends also once the
        # bootstrap started: the kit's ends live on in the spawn.
        with contextlib.ExitStack() as kit_ends:
            with contextlib.ExitStack() as bootstrap_ends:
                ours, theirs = socket.socketpair()
                kit_ends.callback(ours.close)
                bootstrap_ends.callback(theirs.close)
                report, report_end = os.pipe()
                kit_ends.callback(os.close, report)
                bootstrap_ends.callback(os.close, report_end)
                env_end, env_feed = os.pipe()
                kit_ends.callback(os.close, env_feed)
                bootstrap_ends.callback(os.close, env_end)
                ours.setblocking(False)
                transport, protocol = await loop.subprocess_exec(
                    lambda: _PipeProtocol(loop),
                    *bootstrap_argv(argv, hold_fd=theirs.fileno(), report_fd=report_end, env_fd=env_end),
                    cwd=cwd,
                    env={},
                    stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                    pass_fds=(theirs.fileno(), report_end, env_end),
                )
            kit_ends.pop_all()
        process = cls(transport, protocol, ours)
        try:
            feed = await PipeFeed.start(env_feed, encoded_env)
        except BaseException:
            os.close(report)
            await process.kill()
            raise
        try:
            failure = await read_to_eof(report)
            broken = None if failure else await feed.closed()
        except BaseException:
            # The abort closes the env pipe only at the next loop pass, and the kill signals the
            # group before its first await: the bootstrap is stopped before its env pipe ends.
            feed.abort()
            await process.kill()
            raise
        if broken is not None:
            # The bootstrap ended without a report before it took all its env.
            await process.kill()
            raise broken
        if failure:
            # The bootstrap exits on its own once it reported: nothing of the exec is left to stop.
            process.release()
            await process.kill()
            # A bootstrap that failed before it took all its env leaves the feed broken: the
            # report says why, and the broken feed is kept as its cause.
            raise spawn_failure(failure, argv[0]) from await feed.closed()
        return process

    @property
    def returncode(self) -> int | None:
        """The child's exit code, or ``None`` while it runs."""
        return self._transport.get_returncode()

    @property
    def running(self) -> bool:
        """Whether the exec is still running: its child runs, or some process holds its output pipes.

        A child that exited with every write end of its output closed is over, whatever it left
        unread in its pipes; a descendant still holding one keeps the exec running.
        """
        return self.returncode is None or self._protocol.output_held()

    @property
    def output_held_at_close(self) -> bool:
        """Whether a process still held an output pipe as a kill closed the pipes: its later output is uncounted."""
        return self._protocol.output_held_at_close

    @property
    def stdout_len(self) -> int:
        """Cumulative stdout bytes the child gave: those received, and those a kill's close left unread."""
        return self._protocol.stdout_len

    @property
    def stderr_len(self) -> int:
        """Cumulative stderr bytes the child gave: those received, and those a kill's close left unread."""
        return self._protocol.stderr_len

    def exit_code(self) -> int:
        """The exit code of a child whose output has ended."""
        code = self.returncode
        if code is None:  # pragma: no cover - defensive; the output ends only after the exit
            raise SandboxError("sandbox exec exit code read before the process exited")
        return code

    async def next_output(self) -> SandboxStreamChunk | None:
        """The next stdout/stderr chunk, or ``None`` once the child has exited and its pipes are closed.

        Raises :class:`SandboxError` instead of the end when the child exited with stdin input
        unwritten while a process it started still held its stdin, an exit the kit's kill did not
        cause: input the exec never got.
        """
        return await self._protocol.take()

    async def write(self, data: bytes) -> None:
        """Write ``data`` to the child's stdin; raises once stdin is closed or the child has exited."""
        stdin = _stdin_transport(self._transport)
        if stdin is None or self.returncode is not None or stdin.is_closing() or self._protocol.stdin_lost is not None:
            raise SandboxError("sandbox exec stdin is closed or the exec has exited; cannot write")
        stdin.write(data)
        if stdin.is_closing():
            # Only a failed write closes the pipe here; its loss is reported a loop pass later.
            await asyncio.shield(self._protocol.stdin_gone)
        await self._protocol.drain()

    async def feed_stdin(self, data: bytes) -> None:
        """Write ``data`` to the child's stdin, then close it: a one-shot exec's input.

        A child that closed its stdin before taking all of ``data``, or exited while no other
        process held its stdin, chose not to read the rest: the feed stops there and the child's
        output and exit code stand. A child that exited while a process it started still held its
        stdin leaves the rest unwritten: the feed stops there too, and the end of the output
        raises that cut (:meth:`next_output`). Any other failed write raises, as :meth:`write`
        does.
        """
        try:
            await self.write(data)
        except SandboxError:
            if not await self._stdin_closed_by_the_child():
                raise
        self.close_stdin()

    async def _stdin_closed_by_the_child(self) -> bool:
        """Whether the child's stdin, found closed by a write, was closed by the child: it exited or closed its end."""
        if self._stdin_closed_here:
            return False
        stdin = _stdin_transport(self._transport)
        if stdin is not None and stdin.is_closing():
            # A pipe the child closed is reported lost a loop pass after it closes.
            await asyncio.shield(self._protocol.stdin_gone)
        if self.returncode is not None:
            return True
        # The read end closing under a write is a broken pipe; with nothing left to write, a
        # reset (the pipe's loss reported with no error).
        return isinstance(self._protocol.stdin_lost, (BrokenPipeError, ConnectionResetError))

    def close_stdin(self) -> None:
        """Close the child's stdin so it reads EOF."""
        self._stdin_closed_here = True
        stdin = _stdin_transport(self._transport)
        if stdin is not None and not stdin.is_closing():
            stdin.close()

    def kill_group(self) -> bool:
        """SIGKILL every process still in the exec's group, its own exit notwithstanding; whether it signalled it.

        The group is signalled only while the kit's anchor is alive in it, which is what keeps
        the group's id the exec's after the leader is gone; a group the kit has released is
        never signalled again. Should the anchor have ended before the kill, whatever ended it
        (a signal from outside the kit, the host's out-of-memory killer), the kit warns that
        processes the command left behind may still run and kills the leader alone, while it
        still runs. The hold is released afterwards. It returns whether it signalled the group.
        """
        if self._hold is None:
            return False
        self._protocol.note_kill()
        pid = self._transport.get_pid()
        signalled = False
        if self._anchor_alive(self._hold):
            try:
                os.killpg(pid, signal.SIGKILL)
                signalled = True
            except ProcessLookupError:
                # The anchor died between the check and the kill: nothing was signalled.
                self._warn_hold_ended(pid)
        else:
            self._warn_hold_ended(pid)
            if self.returncode is None:
                # The leader alone, by its pid: nothing else is provably the exec's. Never the
                # transport's own kill, whose reap races the child watcher.
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
        self.release()
        return signalled

    def release(self) -> None:
        """Let the exec's group go: the kit will never signal it again, and the anchor ends. Idempotent."""
        if self._hold is not None:
            self._hold.close()
            self._hold = None

    @staticmethod
    def _anchor_alive(hold: socket.socket) -> bool:
        """Whether the anchor still holds its end of ``hold``: it never writes, so EOF means it is gone."""
        try:
            data = hold.recv(1)
        except BlockingIOError:
            return True
        return data != b""

    @staticmethod
    def _warn_hold_ended(pid: int) -> None:
        logger.warning(
            "sandbox exec group hold for pid %d ended before the kill: processes the command left running "
            "in its group may still run",
            pid,
        )

    async def kill(self) -> None:
        """Kill the process group, await the child's own exit, then close every pipe.

        The kill reaches every process still in the group, the child's own exit
        notwithstanding. A kill that signalled the group first waits, for at most
        :data:`_GROUP_END_WAIT_SECONDS`, while the output is still held and a process is still
        in the group (:meth:`_group_ending`), so whether a process still holds the output once
        the pipes are closed tells only of one the kill did not reach. The output then ends
        with what was already read: a descendant that left the group and still holds a pipe no
        longer keeps it open: stdin is aborted at the child's exit, as at every exit, and the
        output pipes are closed then, not by the caller's wait on it, so a caller cancelled
        during either wait still leaves none open. Idempotent: once the group is released, a
        further kill sends nothing.
        """
        signalled = self.kill_group()
        try:
            if signalled:
                await self._group_ending()
        finally:
            self._protocol.close_at_exit()
        await asyncio.shield(self._protocol.exited)

    async def _group_ending(self) -> None:
        """Wait while a process holds the output and a process is still in the signalled group, within a bound.

        Every process in the group was SIGKILLed, so each ends an instant later and closes the
        pipes it held; a process still in the group past the bound, as one no process reaps, is
        waited for no longer. The group's id is probed with signal 0, which signals nothing; a
        group id another user's process has since taken counts as the group ended.
        """
        pgid = self._transport.get_pid()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _GROUP_END_WAIT_SECONDS
        while self._protocol.output_held() and loop.time() < deadline:
            try:
                os.killpg(pgid, 0)
            except (ProcessLookupError, PermissionError):
                return
            await asyncio.sleep(_GROUP_END_POLL_SECONDS)
