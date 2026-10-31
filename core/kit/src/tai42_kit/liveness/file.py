"""A process's liveness file: the writer that reports into it and the check that reads it.

The file holds two lines, the writer's pid and a horizon: the seconds within which the writer
promised its next report when it wrote the file. Its modification time is when that promise was
made. The writer replaces the file atomically, and only when the promise already written would
not cover the loop's next pass; the check decides from one open file whether the promise is
overdue by more than a grace.
"""

from __future__ import annotations

import fcntl
import logging
import math
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from tai42_kit.liveness.settings import BackendLivenessSettings

logger = logging.getLogger(__name__)


def _parse(path: Path, raw: bytes) -> tuple[int, float]:
    """The pid and the horizon a liveness file holds; anything else raises ``ValueError`` naming the file."""
    lines = raw.decode("ascii", errors="replace").splitlines()
    try:
        pid_text, horizon_text = lines
        pid, horizon = int(pid_text), float(horizon_text)
    except ValueError:
        pid, horizon = 0, 0.0
    if pid <= 0 or not (math.isfinite(horizon) and horizon > 0):
        raise ValueError(f"liveness file {path} does not hold a pid and a horizon: {raw[:64]!r}")
    return pid, horizon


def _owner(path: Path) -> int | None:
    """The pid the file at ``path`` holds, or ``None`` when there is no file."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    return _parse(path, raw)[0]


def _pid_alive(pid: int) -> bool:
    """Whether a process with ``pid`` exists (one owned by another user counts)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextmanager
def _claim_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on the directory of ``path`` while a process claims or clears a file it does not own.

    The owner check and the write that follows it are then one step for every process taking
    the same lock, so two processes claiming one file at once leave exactly one owner. Closing the
    descriptor releases the lock, also when the process dies holding it.
    """
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


class LivenessFile:
    """A process's liveness file, written from the reports of its loop.

    Each report (:meth:`report`) carries ``next_within``, the seconds within which the loop's next
    pass is due. The file is written only when the promise already in it would expire before that
    pass: a steady loop writes about once per ``interval``, and a loop whose cadence lengthens
    writes at once. The horizon written is ``interval + next_within``, so while the loop keeps its
    promises the file's age never exceeds its own horizon.

    :meth:`report` is thread-safe and cheap between writes (one clock read). Each write first checks
    the file's owner: a file holding the pid of another live process is refused with a
    ``RuntimeError``, because two processes writing one file would hide either one's stall. A file
    left by a process that no longer exists is taken over, and the temp file that process staged a
    write in is cleared with it. Claiming a file this process does not
    own yet (a missing file, or a dead owner's) happens under a lock on the file's directory, so
    of two processes claiming one file at once the second is refused.

    :meth:`remove` is final: once this writer has removed its file, a later report raises
    ``RuntimeError`` and writes nothing, so no probe reads a process that has released its file
    as alive.
    """

    def __init__(self, path: Path, interval: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        """Write to ``path`` with an ``interval``-second margin on every promise, timed by ``clock``."""
        self.path = path
        self.interval = interval
        self._clock = clock
        self._lock = threading.Lock()
        self._deadline: float | None = None
        self._removed = False

    @classmethod
    def from_settings(cls, settings: BackendLivenessSettings) -> LivenessFile:
        """The writer for the configured path and interval."""
        return cls(settings.path, settings.interval)

    def report(self, next_within: float) -> None:
        """Record one pass of the loop, whose next pass is due within ``next_within`` seconds.

        Raises ``ValueError`` when ``next_within`` is not positive or the file holds no pid and
        horizon, ``OSError`` when the file cannot be written, and ``RuntimeError`` when another live
        process owns it or this writer has already removed it.
        """
        if not (math.isfinite(next_within) and next_within > 0):
            raise ValueError(f"next_within must be positive seconds, got {next_within!r}")
        with self._lock:
            if self._removed:
                raise RuntimeError(
                    f"liveness file {self.path} was removed when this process released it; it reports no more"
                )
            now = self._clock()
            if self._deadline is not None and now + next_within <= self._deadline:
                return
            pid = os.getpid()
            horizon = self.interval + next_within
            if _owner(self.path) == pid:
                # No other process claims a live owner's file, so this write needs no claim.
                self._write(pid, horizon)
            else:
                with _claim_lock(self.path):
                    owner = _owner(self.path)
                    if owner is not None and owner != pid and _pid_alive(owner):
                        raise RuntimeError(
                            f"liveness file {self.path} is owned by live process {owner}; "
                            "set TAI_BACKEND_LIVENESS_PATH to a distinct path per backend process"
                        )
                    if owner is not None and owner != pid:
                        # A dead owner killed between its write and its rename left its temp file.
                        self._temp_file(owner).unlink(missing_ok=True)
                    self._write(pid, horizon)
            self._deadline = now + horizon

    def _temp_file(self, pid: int) -> Path:
        """The file the writer with ``pid`` stages each write in before renaming it over :attr:`path`."""
        return self.path.with_name(f"{self.path.name}.{pid}.tmp")

    def _write(self, pid: int, horizon: float) -> None:
        """Replace the file atomically with ``pid`` and ``horizon``."""
        tmp = self._temp_file(pid)
        tmp.write_bytes(f"{pid}\n{horizon:g}\n".encode("ascii"))
        os.replace(tmp, self.path)

    def remove(self) -> None:
        """Remove the file when this process owns it, or when its owner no longer exists; report no more.

        A missing file is already the state removal leaves. A file another live process owns is
        left in place and reported at WARNING: it is that process's signal, not this one's. Every
        later :meth:`report` through this writer raises. The temp file a write is staged in goes
        with the file it belongs to: this process's own always, a dead owner's with that owner's
        file, another live process's never.
        """
        with self._lock:
            self._removed = True
            pid = os.getpid()
            self._temp_file(pid).unlink(missing_ok=True)
            owner = _owner(self.path)
            if owner is None:
                return
            if owner == pid:
                self.path.unlink(missing_ok=True)
                return
            with _claim_lock(self.path):
                owner = _owner(self.path)
                if owner is None:
                    return
                if owner != pid and _pid_alive(owner):
                    logger.warning(
                        "liveness file %s is owned by process %s, not this one; leaving it in place", self.path, owner
                    )
                    return
                self._temp_file(owner).unlink(missing_ok=True)
                self.path.unlink(missing_ok=True)


@dataclass(frozen=True)
class LivenessCheck:
    """One reading of a liveness file."""

    state: Literal["alive", "stale", "missing"]
    path: Path
    pid: int | None
    age: float | None
    horizon: float | None
    grace: float

    @property
    def alive(self) -> bool:
        """Whether the file's promise was not overdue by more than the grace."""
        return self.state == "alive"

    def describe(self) -> str:
        """The one line the probe prints for this reading."""
        if self.state == "missing":
            return (
                f"not alive: no liveness file at {self.path} (the process has not reached its loop yet, or it exited)"
            )
        if self.state == "stale":
            return (
                f"not alive: last report {self.age:.1f}s ago, next pass was promised within {self.horizon:g}s "
                f"(+{self.grace:g}s grace) (pid {self.pid}, {self.path})"
            )
        return (
            f"alive: pid {self.pid} reported {self.age:.1f}s ago, next pass promised within {self.horizon:g}s "
            f"(+{self.grace:g}s grace; {self.path})"
        )


def check_liveness(path: Path, grace: float, *, now: Callable[[], float] = time.time) -> LivenessCheck:
    """Read the liveness file at ``path``: alive while its age is at most its horizon plus ``grace`` seconds.

    The age, the pid and the horizon come from one open file, so a write landing mid-read cannot
    mix two files. Content that is not a pid and a horizon raises ``ValueError``; any other read
    error propagates.
    """
    try:
        with path.open("rb") as handle:
            mtime = os.fstat(handle.fileno()).st_mtime
            raw = handle.read()
    except FileNotFoundError:
        return LivenessCheck(state="missing", path=path, pid=None, age=None, horizon=None, grace=grace)
    pid, horizon = _parse(path, raw)
    age = now() - mtime
    return LivenessCheck(
        state="alive" if age <= horizon + grace else "stale",
        path=path,
        pid=pid,
        age=age,
        horizon=horizon,
        grace=grace,
    )
