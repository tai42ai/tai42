"""The reload/fork race the fork gate exists to close, reproduced with a real
``os.fork()`` and a real ``importlib`` per-module lock — no Redis, no rq server.

A config reload pops this process's manifest modules out of ``sys.modules`` and
re-imports them, holding each module's ``importlib._bootstrap._ModuleLock`` for the
span of its body. ``importlib`` registers no ``os.register_at_fork`` handler, so a
child forked inside that span inherits the held lock with an owner thread that does
not exist post-fork: the child blocks forever on the same import. That is what
wedges an rq work-horse — it resolves its job function by importing its module, and
the parent then sits in ``monitor_work_horse`` draining nothing.

Both legs run. ``no_gate`` forks unguarded and asserts the child does NOT exit — the
wedge must still reproduce, or the gated leg proves nothing. ``gate`` runs the same
two sides through a real :class:`ForkGate` and asserts the fork is deferred until the
re-import completes, after which the child imports a settled module and exits clean.
"""

from __future__ import annotations

import contextlib
import importlib
import os
import signal
import sys
import textwrap
import threading
import time
import types
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from tai42_kit.fork_gate import ForkGate

pytestmark = pytest.mark.skipif(not hasattr(os, "fork"), reason="the race needs a real os.fork()")

# Budget for something that MUST happen (generous: only a real hang exceeds it).
_ARRIVES = 10.0
# Budget after which a child that has not exited counts as wedged, and an event that
# has not fired counts as deferred.
_SETTLED = 1.0
# Reap poll interval; a child process has no event to set, so its exit is polled.
_REAP_POLL = 0.01

# The module the parked package reaches back through, so its body can park exactly as
# long as the test wants while holding its own import lock.
_RENDEZVOUS = "_tai_forkrace_rendezvous"


@pytest.fixture
def parked_package(tmp_path: Path) -> Iterator[tuple[str, threading.Event, threading.Event]]:
    """A throwaway importable package whose body parks until released.

    Yields ``(name, importing, release)``: whichever thread imports ``name`` sets
    ``importing`` from inside the module body — holding that module's import lock —
    and leaves the body only once ``release`` is set.
    """
    name = f"_tai_forkrace_{uuid.uuid4().hex}"
    importing = threading.Event()
    release = threading.Event()

    rendezvous = types.ModuleType(_RENDEZVOUS)
    rendezvous.slots = {"importing": importing, "release": release}  # pyright: ignore[reportAttributeAccessIssue]
    sys.modules[_RENDEZVOUS] = rendezvous

    (tmp_path / f"{name}.py").write_text(
        textwrap.dedent(
            f"""
            import {_RENDEZVOUS} as _r

            _r.slots["importing"].set()
            _r.slots["release"].wait(30)
            VALUE = "imported"
            """
        )
    )
    sys.path.insert(0, str(tmp_path))
    try:
        yield name, importing, release
    finally:
        release.set()
        sys.path.remove(str(tmp_path))
        sys.modules.pop(name, None)
        sys.modules.pop(_RENDEZVOUS, None)


def _fork_child_importing(name: str) -> int:
    """Fork a child whose first act is to import ``name``, then exit 0.

    Mirrors the work-horse: rq resolves the job function in the child by importing
    its module (``rq.utils.import_attribute``).
    """
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the child never returns to the collector
        try:
            importlib.import_module(name)
        except BaseException:
            os._exit(1)
        os._exit(0)
    return pid


def _exit_status_within(pid: int, budget: float) -> int | None:
    """The child's exit status, or ``None`` if it is still running at ``budget``."""
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        reaped, status = os.waitpid(pid, os.WNOHANG)
        if reaped:
            return status
        time.sleep(_REAP_POLL)
    return None


def _spawn(fn: Callable[[], None]) -> threading.Thread:
    thread = threading.Thread(target=fn, daemon=True)
    thread.start()
    return thread


@pytest.mark.parametrize("guarded", [False, True], ids=["no_gate", "gate"])
def test_a_child_forked_during_a_module_reimport_wedges_unless_gated(
    parked_package: tuple[str, threading.Event, threading.Event], guarded: bool
) -> None:
    name, importing, release = parked_package
    gate = ForkGate()
    forked = threading.Event()
    pid_slot: dict[str, int] = {}

    def reimport() -> None:
        sys.modules.pop(name, None)
        importlib.import_module(name)

    def reload_side() -> None:
        """The reload body, as ``reload_gate.run`` drives it."""
        if guarded:
            with gate.exclusive(timeout=_ARRIVES):
                reimport()
        else:
            reimport()

    def work_side() -> None:
        """The rq work loop's job span around the fork instant, as the prefork worker's
        ``fork_work_horse`` drives it."""
        if guarded:
            with gate.job_span(timeout=_ARRIVES):
                pid_slot["pid"] = _fork_child_importing(name)
                forked.set()
        else:
            pid_slot["pid"] = _fork_child_importing(name)
            forked.set()

    reloader = _spawn(reload_side)
    # ``importing`` is set from inside the module body, so the reload is provably
    # holding that module's import lock (and, when guarded, the gate) before the work
    # loop starts.
    assert importing.wait(_ARRIVES), "the re-import never reached the module body"
    worker = _spawn(work_side)

    try:
        if guarded:
            assert not forked.wait(_SETTLED), "the gate did not defer the fork past the re-import"
            release.set()
            reloader.join(timeout=_ARRIVES)
            assert forked.wait(_ARRIVES), "the deferred fork never happened once the re-import finished"
            worker.join(timeout=_ARRIVES)
            assert _exit_status_within(pid_slot["pid"], _ARRIVES) == 0, (
                "the gated child did not exit cleanly — it should import a settled module"
            )
        else:
            assert forked.wait(_ARRIVES), "the unguarded work loop never forked"
            assert _exit_status_within(pid_slot["pid"], _SETTLED) is None, (
                "the unguarded child exited — the race no longer reproduces, so the gated leg "
                "proves nothing; re-derive the reproduction before trusting this suite"
            )
            release.set()
            reloader.join(timeout=_ARRIVES)
            worker.join(timeout=_ARRIVES)
    finally:
        release.set()
        pid = pid_slot.get("pid")
        if pid is not None:
            with contextlib.suppress(ProcessLookupError, ChildProcessError):
                os.kill(pid, signal.SIGKILL)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(pid, 0)
