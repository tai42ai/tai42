"""Settings for the Docker sandbox provider (the ``SANDBOX_DOCKER_`` env group).

:class:`DockerSandboxSettings` mixes in
:class:`~tai42_kit.sandbox.SandboxDispatchSettings`, the surface the kit sandbox
base reads for the default session TTL, the reap interval, and the default
per-``exec`` timeout: those names, defaults and reload classes are declared once
there, under this group's own prefix.

``host`` is REQUIRED and has no default — a mis-wired deployment must fail loudly
at first use rather than silently target a local socket. The mTLS client identity
that speaks the engine control API sits under the canonical ``/certs/client``
mount, so the certs are never env-configured and never enter the recycle-pinned
app env; ``host`` is the ONLY ``SANDBOX_DOCKER_*`` var that does. Every spec
credential rides ``spec.env`` as a ``SecretStr`` and is unwrapped ONLY at the
engine call, never here.
"""

from __future__ import annotations

from typing import Literal

from pydantic_settings import SettingsConfigDict
from tai42_kit.sandbox import SandboxDispatchSettings
from tai42_kit.settings import settings_cache


class DockerSandboxSettings(SandboxDispatchSettings):
    """The ``SANDBOX_DOCKER_`` env group backing the Docker sandbox provider."""

    model_config = SettingsConfigDict(
        env_prefix="SANDBOX_DOCKER_",
    )

    # The REMOTE engine endpoint: a ``unix:///var/run/...`` socket path or a
    # ``tcp://host:port``. REQUIRED with no default so a mis-wired deployment fails
    # loudly at first use instead of silently targeting a local socket.
    host: str

    # mTLS for a ``tcp://`` host. ``tls_verify`` off is never the documented shape;
    # an unauthenticated ``tcp://`` endpoint is not supported. The three cert paths
    # sit under the canonical ``/certs/client`` mount PLAN_6 provisions, so they are
    # NOT env-configured and never enter the recycle-pinned app env.
    tls_verify: bool = True
    tls_cert_path: str = "/certs/client/cert.pem"
    tls_key_path: str = "/certs/client/key.pem"
    tls_ca_path: str = "/certs/client/ca.pem"

    # Fallback caps used ONLY when a spec leaves a cap unset; a spec cap always
    # wins. Neither this provider nor a spec silently runs uncapped when the caller
    # asked for a cap — an unenforceable cap is a loud rejection.
    default_cpu: float | None = None
    default_memory_mb: int | None = None

    # ``missing`` pulls an image only when absent (never a silent per-run pull);
    # ``never`` is for an airgapped engine — a missing image then raises a typed
    # SandboxError rather than reaching out.
    pull_policy: Literal["missing", "never"] = "missing"


@settings_cache
def docker_sandbox_settings() -> DockerSandboxSettings:
    return DockerSandboxSettings()  # pyright: ignore[reportCallIssue]
