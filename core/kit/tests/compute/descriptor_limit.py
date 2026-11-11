"""This test process held at its file descriptor limit for the length of a block."""

from __future__ import annotations

import contextlib
import errno
import os
import resource
from collections.abc import Iterator

# Room the lowered limit leaves above the descriptors open now; the null device fills it.
_ROOM = 8


@contextlib.contextmanager
def at_descriptor_limit(*, free: int = 0) -> Iterator[None]:
    """Lower ``RLIMIT_NOFILE`` and open the null device until no more than ``free`` descriptors are free.

    On the way out every descriptor opened here is closed and the limit is restored.
    """
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    fillers: list[int] = []
    resource.setrlimit(resource.RLIMIT_NOFILE, (len(os.listdir("/proc/self/fd")) + _ROOM, hard))
    try:
        while True:
            try:
                fillers.append(os.open(os.devnull, os.O_RDONLY | os.O_CLOEXEC))
            except OSError as exc:
                if exc.errno != errno.EMFILE:
                    raise
                break
        for _ in range(min(free, len(fillers))):
            _close_now(fillers.pop())
        yield
    finally:
        for fd in fillers:
            _close_now(fd)
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def _close_now(fd: int) -> None:
    # A process prelude may put off ``os.close`` of a descriptor that is not a regular file until
    # its event loop runs; nothing watches these, so they are closed at once.
    os.closerange(fd, fd + 1)
