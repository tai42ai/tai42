"""The reload/fork race the fork gate exists to close, on the fork a prefork pool makes — a real
billiard ``Process`` and a real ``importlib`` per-module lock, no broker.

A config reload pops this process's manifest modules out of ``sys.modules`` and re-imports them,
holding each module's ``importlib._bootstrap._ModuleLock`` for the span of its body. A child forked
inside that span inherits the held lock with an owner thread that does not exist after the fork,
and blocks forever on the same import. A prefork pool child resolves its task by importing its
module, so it wedges the same way.

Both legs run. ``no_gate`` forks unguarded and asserts the child does NOT exit — the wedge must
still reproduce, or the guarded leg proves nothing. ``guard`` installs the gate's fork guard, which
the worker build installs, and asserts billiard's fork is deferred until the re-import completes,
after which the child imports a settled module and exits clean.
"""

from __future__ import annotations

import importlib
import os
import signal
import sys
import textwrap
import threading
import types
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from billiard.context import ForkProcess
from tai42_kit.fork_gate import ForkGate

pytestmark = pytest.mark.skipif(not hasattr(os, "fork"), reason="the race needs a real fork")

# Budget for something that MUST happen (generous: only a real hang exceeds it).
_ARRIVES = 10.0
# Budget after which a child that has not exited counts as wedged, and an event that has not fired
# counts as deferred.
_SETTLED = 1.0

# The module the parked package reaches back through, so its body can park exactly as long as the
# test wants while holding its own import lock.
_RENDEZVOUS = "_tai_celery_forkrace_rendezvous"


@pytest.fixture
def parked_package(tmp_path: Path) -> Iterator[tuple[str, threading.Event, threading.Event]]:
    """A throwaway importable package whose body parks until released.

    Yields ``(name, importing, release)``: whichever thread imports ``name`` sets ``importing`` from
    inside the module body — holding that module's import lock — and leaves the body only once
    ``release`` is set.
    """
    name = f"_tai_celery_forkrace_{uuid.uuid4().hex}"
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


def _spawn(fn: Any) -> threading.Thread:
    thread = threading.Thread(target=fn, daemon=True)
    thread.start()
    return thread


@pytest.mark.parametrize("mode", ["no_gate", "guard"])
def test_a_billiard_child_forked_during_a_module_reimport_wedges_unless_guarded(
    parked_package: tuple[str, threading.Event, threading.Event], mode: str
) -> None:
    name, importing, release = parked_package
    gate = ForkGate()
    guarded = mode == "guard"
    if guarded:
        gate.guard_forks(timeout=_ARRIVES, tick=lambda: None)
    forked = threading.Event()
    child = ForkProcess(target=importlib.import_module, args=(name,), daemon=True)

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

    def pool_side() -> None:
        """The prefork pool's fork of a new child."""
        child.start()
        forked.set()

    reloader = _spawn(reload_side)
    # ``importing`` is set from inside the module body, so the reload provably holds that module's
    # import lock (and, when guarded, the gate) before the pool forks.
    assert importing.wait(_ARRIVES), "the re-import never reached the module body"
    pool = _spawn(pool_side)

    try:
        if guarded:
            assert not forked.wait(_SETTLED), "the guard did not defer billiard's fork past the re-import"
            release.set()
            reloader.join(timeout=_ARRIVES)
            assert forked.wait(_ARRIVES), "the deferred fork never happened once the re-import finished"
            child.join(_ARRIVES)
            assert child.exitcode == 0, "the guarded child did not exit cleanly — it should import a settled module"
        else:
            assert forked.wait(_ARRIVES), "the unguarded pool never forked"
            child.join(_SETTLED)
            assert child.exitcode is None, (
                "the unguarded child exited — the race no longer reproduces, so the guarded leg "
                "proves nothing; re-derive the reproduction before trusting this suite"
            )
            release.set()
            reloader.join(timeout=_ARRIVES)
        pool.join(timeout=_ARRIVES)
    finally:
        release.set()
        # The guard's fork hooks stay registered for the process; with no guard installed they do nothing.
        gate._fork_hooks.guard = None
        if child.exitcode is None and child.pid is not None:
            os.kill(child.pid, signal.SIGKILL)
            child.join(_ARRIVES)
