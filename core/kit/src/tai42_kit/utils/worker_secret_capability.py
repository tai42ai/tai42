"""The secret-read capability a backend-worker tool run binds for itself.

The ``action=secret`` admin fence gates a host-secret-exposing primitive on
:func:`~tai42_contract.access_control.context.caller_may_read_secrets`, a
contextvar the HTTP seam binds per request and the execution-identity seam
rebinds per fire. A backend worker runs a dequeued task in a process with no HTTP
request and no bound execution identity, so neither seam runs and the contextvar
would read its fail-closed default ``False``.

The capability follows the identity the job runs AS: the backend-submit seam
captures the submitting caller's own :func:`caller_may_read_secrets` and carries
it with the job under :data:`WORKER_SECRET_CAPABILITY_ARG`, and the worker binds
THAT value — so an admin's job clears the secret fence and a non-admin's does not,
the same verdict the caller would get in-process. When no value rode along (an
OFF-mode process, or a job with no propagated capability) the bind falls back to
the gate rule: gate OFF -> ``True`` (synthetic admin, so a dev ``inject_env`` run
works with no HTTP request), gate ON -> ``False`` (fail-closed). The gate state is
read from ``ACCESS_CONTROL_ENABLE`` — the operator env the skeleton gate reads too
— through a registry-excluded settings mirror, so a worker process that never
imports tai42-skeleton reads the same env with the same parsing. Homed in kit
because the execution backends below the skeleton set it (they never import
tai42-skeleton), the same layering reason the detached-run marker lives here.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import ClassVar

from pydantic_settings import SettingsConfigDict
from tai42_contract.access_control import reset_request_secret_capability, set_request_secret_capability

from tai42_kit.settings import TaiBaseSettings, settings_cache

# The reserved job kwarg the backend-submit seam stamps the submitting caller's
# secret-read capability into, and the worker pops before running the tool. Stamped
# server-side AFTER the caller's arguments are read, so a caller can never forge it;
# namespaced under the ``backend_`` dispatch-kwarg convention so it cannot collide
# with a tool parameter.
WORKER_SECRET_CAPABILITY_ARG = "backend_secret_capability"


class _WorkerAccessControlSettings(TaiBaseSettings):
    """The one access-control field a worker needs — whether the gate is enabled —
    read from the SAME ``ACCESS_CONTROL_`` env the skeleton gate reads.

    Registry-excluded: it is a narrow read-only mirror of the skeleton's own
    ``AccessControlSettings.enable``, not a second configurable group. The default
    matches the skeleton gate's (enabled), so a worker fail-closes on a stray
    process that never set the env.
    """

    registry_exclude: ClassVar[bool] = True

    model_config = SettingsConfigDict(env_prefix="ACCESS_CONTROL_")

    enable: bool = True


@settings_cache
def _worker_access_control_settings() -> _WorkerAccessControlSettings:
    return _WorkerAccessControlSettings()


@contextmanager
def bind_worker_secret_capability(capability: bool | None = None) -> Iterator[None]:
    """Bind the secret-read capability for a backend-worker tool run and restore it
    in the ``finally``.

    ``capability`` is the submitting caller's own secret-read capability, propagated
    with the job from the backend-submit seam. Given, it is bound verbatim, so the
    worker run's capability equals the admin status of the identity that SUBMITTED
    the job — an admin's job clears the secret fence, a non-admin's does not. Absent
    (``None`` — an OFF-mode process, or a job with no propagated value) it falls back
    to the gate rule: gate OFF -> ``True`` (every principal is the synthetic admin,
    so a dev ``inject_env`` run works on a worker), gate ON -> ``False`` (fail-closed,
    a detached run never clears the secret fence off a caller it cannot re-authorize).
    """
    resolved = capability if capability is not None else (not _worker_access_control_settings().enable)
    token = set_request_secret_capability(resolved)
    try:
        yield
    finally:
        reset_request_secret_capability(token)
