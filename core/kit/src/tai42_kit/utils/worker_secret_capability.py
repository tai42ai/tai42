"""The secret-read capability a backend-worker tool run binds for itself, and the gate state it derives from.

The ``action=secret`` admin fence gates a host-secret-exposing primitive on
:func:`~tai42_contract.access_control.context.caller_may_read_secrets`, a
contextvar the HTTP seam binds per request and the execution-identity seam
rebinds per fire. A backend worker runs a dequeued job in a process with no HTTP
request and no bound execution identity, so neither seam runs and the contextvar
would read its fail-closed default ``False``.

Every job carries its capability under :data:`WORKER_SECRET_CAPABILITY_ARG`, decided
in the submitting process, and the worker binds exactly that value. A task job
carries the submitting caller's own :func:`caller_may_read_secrets` (an admin's job
clears the secret fence, a non-admin's does not — the verdict the caller gets
in-process). A callback job runs a follow-up no caller re-authorizes, so it carries
the access-control GATE STATE instead: gate OFF -> ``True`` (every principal is the
synthetic admin), gate ON -> ``False`` (fail-closed). The gate's owner declares its
state to the kit through :func:`set_access_control_gate_state` in every process that
enqueues; a read before any declaration raises. The state is per serving generation: a
declaration made while a rebuild stages (:func:`begin_staging`) takes effect only when the
rebuild commits, and a failed rebuild's declaration is dropped. Homed in kit because the execution
backends below the skeleton set it (they never import tai42-skeleton), the same
layering reason the detached-run marker lives here.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from tai42_contract.access_control import reset_request_secret_capability, set_request_secret_capability

from tai42_kit.registry import StagedSlot

# The reserved job kwarg the backend-submit seam stamps the job's secret-read capability
# into, and the worker pops before running the tool. Stamped server-side AFTER the caller's
# arguments are read, so a caller can never forge it; namespaced under the ``backend_``
# dispatch-kwarg convention so it cannot collide with a tool parameter.
WORKER_SECRET_CAPABILITY_ARG = "backend_secret_capability"  # noqa: S105 constant identifier, not a secret value

_GATE_STATE: StagedSlot[bool] = StagedSlot()


def set_access_control_gate_state(enabled: bool) -> None:
    """Declare whether the access-control gate is enabled; the gate's owner calls it.

    Staged while a rebuild stages (promoted at :func:`commit_staging`), else effective at once.
    """
    _GATE_STATE.set(enabled)


def access_control_gate_state() -> bool:
    """Whether the access-control gate is enabled in the serving generation; raises when never declared."""
    enabled = _GATE_STATE.current()
    if enabled is None:
        raise RuntimeError("the access-control gate state was never declared to the kit in this process")
    return enabled


def begin_staging() -> None:
    """Open staging: a declaration now waits for :func:`commit_staging`; the serving state is untouched."""
    _GATE_STATE.begin()


def commit_staging() -> None:
    """Promote a declaration made while staging; with none, the serving state stays."""
    _GATE_STATE.commit()


def abort_staging() -> None:
    """Drop a declaration made while staging; the serving state is untouched."""
    _GATE_STATE.abort()


@contextmanager
def bind_worker_secret_capability(capability: bool) -> Iterator[None]:
    """Bind ``capability`` — the value the job carried — for a backend-worker run and restore it in the ``finally``."""
    token = set_request_secret_capability(capability)
    try:
        yield
    finally:
        reset_request_secret_capability(token)
