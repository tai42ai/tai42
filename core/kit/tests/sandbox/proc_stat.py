"""A process's ``/proc/<pid>/stat`` fields, for the tests that check what the scripts read from it."""

from __future__ import annotations

import time
from pathlib import Path


def _stat_fields(pid: int) -> list[str]:
    """The fields of process ``pid``'s ``stat`` from the 3rd on, read past its last ``)``.

    The command name before them may hold any character, a ``)`` too.
    """
    return Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()


def stat_flags(pid: int) -> int:
    """The kernel's flags of process ``pid``: the 9th field of its ``stat``."""
    return int(_stat_fields(pid)[6])


def stat_start_time(pid: int) -> int:
    """The start time of process ``pid``, in clock ticks since boot: the 22nd field of its ``stat``."""
    return int(_stat_fields(pid)[19])


def wait_asleep(pid: int) -> None:
    """Return once process ``pid`` sleeps: the state, the 3rd field of its ``stat``, is ``S``.

    Fails when it does not within 10 s.
    """
    deadline = time.monotonic() + 10
    while (state := _stat_fields(pid)[0]) != "S":
        if time.monotonic() > deadline:
            raise AssertionError(f"process {pid} does not sleep within 10 s: its state is {state}")
        time.sleep(0.01)
