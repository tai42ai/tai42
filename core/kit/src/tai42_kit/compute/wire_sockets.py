"""The compute wire's socket ends a process holds, and which of them a child it forks keeps.

A peer of the wire sees the other side end by that side's socket end closing, so a copy of an end
held by a forked child that has no use for it would keep a dead peer open. Every site that makes,
receives, hands on or closes an end of the wire does it through :data:`wire_sockets`, whose fork
hooks close, in the forked child, every end the process holds except:

- the end the process's children send their jobs on (:meth:`WireSockets.keep_in_children`): a
  sharing process's child end of its rendezvous, the owner's end of its published pool host, or
  the end the process itself reaches its pool over;
- in a child forked by the thread that made a pair for a process it spawns, that pair's end
  handed to the spawned process (a process start that forks through the interpreter's fork hooks).

A fork waits while an end is made, received, adopted or closed (the lock), so the record matches
the process's descriptors at every fork. The lock is held only around those calls, none of which
blocks or switches. It is re-entrant on its holder: a close made inside one of those calls on the
same thread (the garbage collector finalizing a job that held an end, as a call allocates), or by
a fork hook while the fork holds the lock, takes it again and updates the record at once. No
section iterates the record while such a close can change it.

An end the record makes or receives is closed on an exec, so a program the process starts holds
none of them unless it is handed one.

A forked child lets go of an end by pointing its descriptor at the null device, which needs no
descriptor free in the child. An end numbered at or above a soft descriptor limit lowered below it
cannot be pointed there: the child closes its socket all the same, and its fork hook raises the
kernel's errors together; where a process prelude puts a watched socket's close off until its event
loop runs, the child holds that end until its loop runs. The record opens that null device once
per process, as it first comes to hold an end, and keeps it: one descriptor in a process that holds
the wire, none in one that never does. A call that cannot open it raises that ``OSError`` and records nothing.
"""

from __future__ import annotations

import array
import contextlib
import os
import socket
import threading
from collections.abc import Iterator


class EndNotReceivedError(OSError):
    """The end passed with a message was not received: the kernel dropped it before it reached this process."""


class WireSockets:
    """The compute wire's socket ends this process holds; a forked child closes those it has no use for."""

    def __init__(self) -> None:
        """Hold no end yet; the process's forks run this record's hooks from now on."""
        self._lock = threading.RLock()
        # Each end, and for an end handed to a spawned process the thread that spawns it.
        self._held: dict[socket.socket, int | None] = {}
        self._kept: socket.socket | None = None
        self._forking_thread: int | None = None
        # The null device a forked child points each end it lets go at; open whenever an end is held.
        self._placeholder: int | None = None
        os.register_at_fork(
            before=self._before_fork,
            after_in_parent=self._after_fork_in_parent,
            after_in_child=self._after_fork_in_child,
        )

    @property
    def kept_in_children(self) -> socket.socket | None:
        """The end a child this process forks keeps and sends its jobs on; ``None`` when there is none."""
        return self._kept

    def socketpair(self, *, second_for_a_spawn: bool = False) -> tuple[socket.socket, socket.socket]:
        """A stream socket pair of the wire.

        With ``second_for_a_spawn``, the second end is handed to a process the calling thread
        spawns: a child that thread forks keeps it.
        """
        with self._lock, self._entering():
            first, second = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            self._held[first] = None
            self._held[second] = threading.get_ident() if second_for_a_spawn else None
        return first, second

    def received(self, end: socket.socket) -> tuple[bytes, list[socket.socket]]:
        """Receive one byte and at most one end passed over the non-blocking ``end``.

        Returns the data and the end received, if any; raises :class:`BlockingIOError` when nothing
        is ready. Empty data with no end is the end of file. The end received is closed on an exec
        from the instant it exists where the platform can say so on the receive, else right after it.
        Raises :class:`EndNotReceivedError` when the kernel dropped the end passed with the data;
        the message is consumed then, and any end that did arrive with it is closed.
        """
        close_on_exec = getattr(socket, "MSG_CMSG_CLOEXEC", 0)
        fds = array.array("i")
        ends: list[socket.socket] = []
        with self._lock, self._entering():
            data, ancillary, flags, _ = end.recvmsg(1, socket.CMSG_LEN(fds.itemsize), close_on_exec)
            for level, kind, passed in ancillary:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    fds.frombytes(passed[: len(passed) - len(passed) % fds.itemsize])
            for fd in fds:
                received = socket.socket(fileno=fd)
                self._held[received] = None
                ends.append(received)
                if not close_on_exec:
                    received.set_inheritable(False)
            if flags & socket.MSG_CTRUNC:
                for received in ends:
                    self.close(received)
                raise EndNotReceivedError(
                    "the end passed to this process was not received: the kernel dropped it, "
                    "as it does when the process has no file descriptor free"
                )
        return data, ends

    def adopted(self, fd: int) -> socket.socket:
        """The socket over the descriptor ``fd`` this process inherited, held from now on."""
        with self._lock, self._entering():
            end = socket.socket(fileno=fd)
            self._held[end] = None
        return end

    def keep_in_children(self, end: socket.socket) -> None:
        """Make the held ``end`` the one a child this process forks keeps and sends its jobs on."""
        with self._lock, self._entering():
            self._held[end] = None
            self._kept = end

    def close(self, end: socket.socket) -> None:
        """Close ``end``; one this process's fork closed already, or never held, is closed all the same."""
        with self._lock:
            self._held.pop(end, None)
            if end is self._kept:
                self._kept = None
            end.close()

    @contextlib.contextmanager
    def _entering(self) -> Iterator[None]:
        """Hold the placeholder for the ends the block records; one opened for a block that raised holding none goes."""
        opened = self._placeholder is None
        if opened:
            self._placeholder = os.open(os.devnull, os.O_RDWR | os.O_CLOEXEC)
        try:
            yield
        except BaseException:
            placeholder = self._placeholder
            if opened and not self._held and placeholder is not None:
                self._placeholder = None
                os.close(placeholder)
            raise

    def _before_fork(self) -> None:
        self._lock.acquire()
        self._forking_thread = threading.get_ident()

    def _after_fork_in_parent(self) -> None:
        self._forking_thread = None
        self._lock.release()

    def _after_fork_in_child(self) -> None:
        # The lock the fork took is this thread's here, so a close the collector makes as an end
        # closes takes it again; each end leaves the record before it closes, an end such a close
        # took out is skipped, and the record keeps the ends the child holds. A process prelude may
        # put off a socket's close while an event loop watches it, until that loop runs; so each
        # end's descriptor is first pointed at the placeholder, which lets go of the end at once,
        # and the socket's own close then frees the descriptor whenever the prelude closes it. An end
        # the kernel will not point there is let go of by that close alone, and the kernel's error
        # propagates. Each end is let go of whatever another end raised; the errors raise together.
        errors: list[Exception] = []
        try:
            placeholder = self._placeholder
            if placeholder is not None:  # open whenever the record holds an end
                for end, spawner in list(self._held.items()):
                    if end is not self._kept and (spawner is None or spawner != self._forking_thread):
                        try:
                            self._let_go_in_child(end, placeholder)
                        except Exception as exc:
                            errors.append(exc)
            if errors:
                # The interpreter prints a fork hook's error without a group's members: the text names them.
                raise ExceptionGroup(
                    "a forked child could not let go of the compute wire's ends cleanly: "
                    + "; ".join(repr(error) for error in errors),
                    errors,
                )
        finally:
            self._forking_thread = None
            self._lock = threading.RLock()

    def _let_go_in_child(self, end: socket.socket, placeholder: int) -> None:
        if end in self._held:
            del self._held[end]
            try:
                os.dup2(placeholder, end.fileno(), inheritable=False)
            finally:
                end.close()


wire_sockets = WireSockets()
