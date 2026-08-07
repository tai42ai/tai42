"""Process-global registry for the monitoring backend.

Access is a free global (``get_monitoring()``), not hung off the app:
monitoring is foundational infra below the app, most emit sites have no app
handle, and the framework's own internals emit (so they cannot import the app
to reach it).

Monitoring is optional: with no plugin registered, ``get_monitoring()`` returns
a shared ``NoOpMonitoring`` default — there is exactly one place a real backend
is installed, the ``@tai42_app.monitoring.register_monitoring`` plugin, mirroring how
backend/template register. Callers never seed the no-op themselves.

The registered backend is plain process memory: a forked worker child inherits
it across ``fork()``, so ``get_monitoring()`` keeps working post-fork —
``init_monitoring()`` is not re-called per child. Only the inner vendor client
dies on fork; the writer's ``shutdown()`` evicts and rebuilds it.
"""

from __future__ import annotations

from tai42_contract.monitoring import Monitoring

from tai42_skeleton.monitoring.noop import NoOpMonitoring

# ``_backend`` is the COMMITTED live backend ``get_monitoring()`` serves. During an
# epoch build the monitoring plugin's re-import records its fresh backend in
# ``_staged_backend`` WITHOUT touching the live one — activation (which shuts down the
# previous backend's writer) is deferred to commit, so a failed build never tears down
# the live monitoring backend. ``_staging`` distinguishes "build registered no
# monitoring module" (keep the live backend) from a staged replacement.
_backend: Monitoring | None = None
_staged_backend: Monitoring | None = None
_staging: bool = False


def init_monitoring(backend: Monitoring) -> None:
    """Register the monitoring backend, installed by a monitoring plugin
    (``@tai42_app.monitoring.register_monitoring``).

    During an epoch build (staging) the backend is STAGED, not activated: the live
    backend keeps serving and is shut down only at commit, so a failed build leaves it
    running. At boot (no staging) it is activated immediately — shutting down any
    previously-installed backend's writer first so its background flush thread / vendor
    client is not leaked. The no-op default's ``shutdown`` is a no-op."""
    global _backend, _staged_backend
    if _staging:
        _staged_backend = backend
        return
    if _backend is not None:
        _backend.writer.shutdown()
    _backend = backend


def begin_staging() -> None:
    """Open monitoring staging: subsequent ``init_monitoring`` calls stage rather than
    activate, leaving the live backend serving."""
    global _staging, _staged_backend
    _staging = True
    _staged_backend = None


def commit_staging() -> None:
    """Activate the staged backend if the build registered one — shutting down the
    previous live backend's writer — else leave the live backend in place (the build
    named no monitoring module). Idempotent when no build staged."""
    global _backend, _staged_backend, _staging
    if _staging and _staged_backend is not None:
        if _backend is not None:
            _backend.writer.shutdown()
        _backend = _staged_backend
    _staged_backend = None
    _staging = False


def abort_staging() -> None:
    """Drop the staged backend on a failed build — the live backend was never touched."""
    global _staged_backend, _staging
    _staged_backend = None
    _staging = False


def register_monitoring(builder=None):
    """Decorator installing the process monitoring backend (manifest
    ``monitoring_module``) — the ``app.monitoring`` facet body.

    A monitoring plugin (e.g. the Langfuse impl) decorates a zero-arg callable
    that returns a ``Monitoring``; it is built and installed via
    ``init_monitoring``, replacing the no-op default. One provider per process,
    last registration wins. The skeleton never names a concrete vendor — the
    plugin is selected purely by the manifest.
    """
    if builder:
        return register_monitoring()(builder)

    def decorator(fn):
        init_monitoring(fn())
        return fn

    return decorator


def reset_monitoring() -> None:
    """Clear any registered backend so ``get_monitoring()`` falls back to the
    no-op default. For test isolation: a test that installs its own recording
    backend resets here (typically via an autouse fixture) so it cannot leak into
    the next test. Not a production path — a real backend is registered once via
    the monitoring plugin."""
    global _backend, _staged_backend, _staging
    _backend = None
    _staged_backend = None
    _staging = False


def get_monitoring_staged() -> Monitoring:
    """The STAGED backend if a build registered one, else the committed backend — the
    build's own view (kind status). During staging a build that names a monitoring
    module records it in ``_staged_backend``; a build naming none keeps the committed
    backend. Serve-time reads use :func:`get_monitoring` (committed only)."""
    if _staged_backend is not None:
        return _staged_backend
    return get_monitoring()


def get_monitoring() -> Monitoring:
    """Return the registered backend, or a shared no-op default if none is set.

    Monitoring being absent is a valid 'disabled' state, not a failure: the first
    call with nothing registered installs a process-wide ``NoOpMonitoring`` (writes
    do nothing, reads return empty) and returns it. A real backend registered via
    ``init_monitoring`` replaces it. Callers never have to initialize monitoring.
    """
    global _backend
    if _backend is None:
        _backend = NoOpMonitoring()
    return _backend
