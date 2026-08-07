"""Graceful self-exit primitives for a recycle op.

A recycled worker replies ``applied`` and then exits cleanly so its supervisor
respawns it and the fresh process loads new store env at boot. The exit is a
self-delivered SIGTERM: the process's already-installed graceful handler runs, so
in-flight work drains before the process ends — never a hard kill. The recycle
handler arms one of these on the bus's post-reply slot (fired AFTER the terminal
reply); the serve primitive is ALSO the applier's own deferred self-exit, wired by
the profile-apply response as a Starlette ``BackgroundTask``.
"""

from __future__ import annotations

import os
import signal
from collections.abc import Callable

from tai42_skeleton.app.bus import OriginKind


def _deliver_self_sigterm() -> None:
    os.kill(os.getpid(), signal.SIGTERM)


def request_serve_graceful_exit() -> None:
    """Trigger this serve worker's uvicorn graceful shutdown. Uvicorn's own SIGTERM
    handler sets ``should_exit``, draining in-flight requests within
    ``timeout_graceful_shutdown`` before the lifespan tears down and the process
    exits cleanly."""
    _deliver_self_sigterm()


def request_backend_graceful_exit() -> None:
    """Trigger this backend runtime's clean stop. The active SIGTERM handler — the
    ``run_backend`` main-task cancel, or the task backend's own warm-drain handler
    (celery/rq/arq) — runs, so an in-flight job drains before ``app_context``
    teardown and the process exits cleanly."""
    _deliver_self_sigterm()


def graceful_exit_for(kind: OriginKind) -> Callable[[], None]:
    """The graceful self-exit primitive for an origin kind. The recycle handler arms
    the returned callable on the bus's post-reply slot, so it fires only after the
    terminal reply ships."""
    if kind is OriginKind.serve:
        return request_serve_graceful_exit
    if kind is OriginKind.backend:
        return request_backend_graceful_exit
    raise ValueError(f"no graceful-exit primitive for origin kind {kind!r}")
