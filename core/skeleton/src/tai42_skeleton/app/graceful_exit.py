"""Graceful self-exit primitives for a recycle op.

A recycled worker replies ``applied`` and then exits cleanly so its supervisor
respawns it and the fresh process loads new store env at boot. The exit is a
self-delivered SIGTERM: the process's already-installed graceful handler runs, so
in-flight work drains before the process ends — never a hard kill.

Three callers pick a primitive through :func:`graceful_exit_for`: the recycle
handler arms it on the bus's post-reply slot (fired AFTER the terminal reply); an
env-change door arms it as the writer's own post-reply exit after a converged roll
(``writer_exit_response``), run after the body flushes on the HTTP edge and by
``run_after_reply`` once the call returned on the projected tool edge; and the
lifespan calls it when a perpetual background task dies.
"""

from __future__ import annotations

import os
import signal
from collections.abc import Callable

from tai42_skeleton.app.bus import WorkerKind


def _deliver_self_sigterm() -> None:
    os.kill(os.getpid(), signal.SIGTERM)


def request_serve_graceful_exit() -> None:
    """Trigger this serve worker's uvicorn graceful shutdown.

    Uvicorn's own SIGTERM handler sets ``should_exit``, draining in-flight
    requests within ``timeout_graceful_shutdown`` before the lifespan tears
    down and the process exits cleanly.
    """
    _deliver_self_sigterm()


def request_backend_graceful_exit() -> None:
    """Trigger this backend runtime's clean stop.

    The active SIGTERM handler — the ``run_backend`` main-task cancel, or the
    task backend's own warm-drain handler (celery/rq/arq) — runs, so an
    in-flight job drains before ``app_context`` teardown and the process exits
    cleanly.
    """
    _deliver_self_sigterm()


def graceful_exit_for(kind: WorkerKind) -> Callable[[], None]:
    """Return the graceful self-exit primitive for a worker kind.

    Every caller fires the returned callable after its reply ships: the recycle
    handler on the bus's post-reply slot, an env-change door as the writer's
    post-reply exit; the lifespan calls it directly when a perpetual task dies.
    """
    if kind is WorkerKind.serve:
        return request_serve_graceful_exit
    if kind is WorkerKind.backend:
        return request_backend_graceful_exit
    raise ValueError(f"no graceful-exit primitive for worker kind {kind!r}")
