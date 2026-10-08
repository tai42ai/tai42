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

import typing

from tai42_contract.monitoring import Monitoring, MonitoringReader, MonitoringWriter
from tai42_kit.registry import StagedSlot

from tai42_skeleton.monitoring.health_watch import activate_export_health, deactivate_export_health
from tai42_skeleton.monitoring.noop import NoOpMonitoring

# The COMMITTED live backend ``get_monitoring()`` serves. During an epoch build the
# monitoring plugin's re-import stages its fresh backend WITHOUT touching the live one —
# activation (which shuts down the previous backend's writer) is deferred to commit, so a
# failed build never tears down the live monitoring backend, and a build that registers no
# monitoring module keeps the live backend.
_BACKEND: StagedSlot[Monitoring] = StagedSlot(on_replace=lambda old: old.writer.shutdown())
_noop_default: Monitoring | None = None


def _check_backend(backend: Monitoring) -> None:
    """Refuse a backend whose writer or reader lacks a member of its contract protocol, naming the members."""
    for face, protocol, label in (
        (backend.writer, MonitoringWriter, "writer"),
        (backend.reader, MonitoringReader, "reader"),
    ):
        missing = sorted(m for m in typing.get_protocol_members(protocol) if not hasattr(face, m))
        if missing:
            raise TypeError(
                f"monitoring backend {type(backend).__qualname__}: its {label} {type(face).__qualname__} lacks "
                f"{', '.join(missing)} of the {protocol.__name__} protocol"
            )


def _activate_if_promoted(previous: Monitoring | None) -> None:
    """Count the live backend's export health when it is a backend other than ``previous``."""
    current = _BACKEND.current()
    if current is not None and current is not previous:
        activate_export_health(current.writer)


def init_monitoring(backend: Monitoring) -> None:
    """Register the monitoring backend, installed by a monitoring plugin.

    Installed via ``@tai42_app.monitoring.register_monitoring``. During an epoch
    build (staging) the backend is STAGED, not activated: the live
    backend keeps serving and is shut down only at commit, so a failed build leaves it
    running. At boot (no staging) it is activated immediately — shutting down any
    previously-installed backend's writer first so its background flush thread / vendor
    client is not leaked. A backend whose writer or reader lacks a protocol member is
    refused first (``TypeError``), staged or not. An activated recording writer has its
    export health counted from then on.
    """
    _check_backend(backend)
    previous = _BACKEND.current()
    _BACKEND.set(backend)
    _activate_if_promoted(previous)


def begin_staging() -> None:
    """Open monitoring staging: subsequent ``init_monitoring`` calls stage rather than activate.

    The live backend keeps serving.
    """
    _BACKEND.begin()


def commit_staging() -> None:
    """Activate the staged backend if the build registered one, else leave the live backend in place.

    Activation shuts down the previous live backend's writer; a build that named
    no monitoring module keeps it. The activated backend's export health is counted from
    then on. Idempotent when no build staged.
    """
    previous = _BACKEND.current()
    _BACKEND.commit()
    _activate_if_promoted(previous)


def abort_staging() -> None:
    """Drop the staged backend on a failed build — the live backend was never touched."""
    _BACKEND.abort()


def register_monitoring(builder=None):
    """Decorator installing the process monitoring backend — the ``app.monitoring`` facet body.

    Selected by the manifest ``monitoring_module``. A monitoring plugin decorates a
    zero-arg callable
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
    """Clear any registered backend so ``get_monitoring()`` falls back to the no-op default.

    For test isolation: a test that installs its own recording backend resets
    here (typically via an autouse fixture) so it cannot leak into the next test.
    Not a production path — a real backend is registered once via the monitoring
    plugin.
    """
    _BACKEND.reset()
    deactivate_export_health()


def get_monitoring_staged() -> Monitoring:
    """The STAGED backend if a build registered one, else the committed backend — the build's own view.

    Serve-time reads use :func:`get_monitoring` (committed only).
    """
    staged = _BACKEND.staged_or_current()
    return staged if staged is not None else get_monitoring()


def get_monitoring() -> Monitoring:
    """Return the registered backend, or a shared no-op default if none is set.

    Monitoring being absent is a valid 'disabled' state, not a failure: with nothing
    registered the process-wide ``NoOpMonitoring`` (writes do nothing, reads return empty)
    is served. A real backend registered via ``init_monitoring`` replaces it. Callers never
    have to initialize monitoring.
    """
    global _noop_default
    backend = _BACKEND.current()
    if backend is not None:
        return backend
    if _noop_default is None:
        _noop_default = NoOpMonitoring()
    return _noop_default
