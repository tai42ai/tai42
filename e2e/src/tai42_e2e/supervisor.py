"""The respawn-on-exit supervisor of a stack (opt-in ``config.supervised``).

A :class:`RespawnSupervisor` plays the external process manager a deployment runs: it watches
the recyclable processes (serve + backend) and brings an exited one back from its saved spec.
Its lock serialises every ``procs``/``specs`` mutation the supervisor and the stack's
teardown/restart race on.
"""

from __future__ import annotations

import contextlib
import signal
import threading
from typing import TYPE_CHECKING

from tai42_e2e import ports, readiness, spawning
from tai42_e2e.waiting import wait_for

if TYPE_CHECKING:
    from tai42_e2e.procs import ProcessHandle
    from tai42_e2e.stack import TaiStack

# A graceful stop ends with status 0, or with the SIGTERM that started it when the
# server re-raises the signal after its shutdown completes (uvicorn does).
_GRACEFUL_EXIT_CODES = frozenset({0, -signal.SIGTERM})


class RespawnSupervisor:
    """The respawn-on-exit supervisor of one stack."""

    def __init__(self, stack: TaiStack) -> None:
        self._stack = stack
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._watched: set[str] = set()
        # Set once boot has converged; before it the supervisor respawns only a graceful exit.
        self._booted = False

    def start(self) -> None:
        """Start watching once every process is spawned, on a supervised stack. Only the
        recyclable kinds are watched: the serve workers, the backend worker, every
        queue-label worker (``backend-queue-<label>``) and every green-pool worker
        (``backend-<pool>``) — metrics and the backend variant's extra processes are no
        recycle target and join no bus census."""
        stack = self._stack
        if not stack.config.supervised:
            return
        backend_workers = set(readiness.expected_backend_processes(stack))
        with self.lock:
            self._watched = {name for name in stack.procs if name.startswith("serve") or name in backend_workers}
        self._thread = threading.Thread(target=self._loop, name=f"tai-e2e-supervisor-{stack.config.name}", daemon=True)
        self._thread.start()

    def mark_booted(self) -> None:
        """Record that boot has converged: from now on any exit of a watched process is respawned."""
        with self.lock:
            self._booted = True

    def stop(self) -> None:
        """Stop the supervisor and wait for any in-flight respawn to finish, so teardown
        never races a re-launch. Idempotent."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=30.0)
            self._thread = None

    def watches(self, name: str) -> bool:
        """Whether the supervisor watches the process ``name``. The caller holds :attr:`lock`."""
        return name in self._watched

    def respawns(self, handle: ProcessHandle) -> bool:
        """Whether the supervisor brings this exited handle back: a watched process that
        exited — any exit once boot has converged, a graceful exit (a recycle) while the
        fleet boots. The caller holds :attr:`lock`."""
        code = handle.poll()
        if code is None or handle.name not in self._watched:
            return False
        return self._booted or code in _GRACEFUL_EXIT_CODES

    def _loop(self) -> None:
        while not self._stop.wait(0.2):
            with self.lock:
                if self._stop.is_set():
                    return
                for name in list(self._watched):
                    handle = self._stack.procs.get(name)
                    if handle is not None and self.respawns(handle):
                        self._respawn(name)

    def _respawn(self, name: str) -> None:
        """Re-launch a self-exited supervised process from its saved spec — the external
        supervisor a graceful recycle self-exit assumes. The caller holds :attr:`lock`.
        Reaps the exited handle, waits for its port to free, then respawns; the replacement
        rejoins the bus census under its stable slot name at an incremented generation (a new pid)."""
        stack = self._stack
        old = stack.procs.get(name)
        if old is not None:
            with contextlib.suppress(Exception):
                old.terminate()
        for port in stack.ports_for(name):
            with contextlib.suppress(TimeoutError):
                wait_for(lambda p=port: ports.is_free(p), deadline=15.0, message=f"port {port} never freed for respawn")
        spawning.spawn(stack, stack.specs[name])
