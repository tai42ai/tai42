"""Settings for the Docker sandbox provider (the ``SANDBOX_DOCKER_`` env group).

:class:`DockerSandboxSettings` mixes in
:class:`~tai42_kit.sandbox.SandboxDispatchSettings`, the surface the kit sandbox
base reads for the default session TTL, the reap interval, and the default
per-``exec`` timeout: those names, defaults and reload classes are declared once
there, under this group's own prefix.

``host`` is REQUIRED and has no default — a mis-wired deployment must fail loudly
at first use rather than silently target a local socket. The mTLS client identity
that speaks the engine control API sits under the canonical ``/certs/client``
mount, so the certs are never env-configured and never enter the app env — the one
channel a client identity must never leak through. The plain knobs (``host``, the
readiness-probe switches, the resource-cap fallbacks, the pull policy) are ordinary
env. Every spec credential rides ``spec.env`` as a ``SecretStr`` and is unwrapped
ONLY at the engine call, never here.
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
    # sit under the canonical ``/certs/client`` mount the deployment provisions, so they are
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

    # Egress-firewall readiness probe (opt-in). When on, the provider PROVES on the
    # session-create path that the engine's inner-bridge egress firewall is in force
    # before it returns a session, and REFUSES the create loudly when it cannot — an
    # actual egress probe from a throwaway container on the egress tier. The two probe
    # targets are DERIVED, never configured, so no target knob can be mis-set: the DENY
    # target is the configured engine control address (which a session must NOT reach
    # across the firewall), the ALLOW target the probe container's own DNS resolver
    # (which must stay reachable). Off by default so a ``unix://`` dev engine or a
    # non-firewalled deployment needs no probe coordinates and sees no behaviour change.
    readiness_probe_enabled: bool = False

    # The throwaway probe container's image: a small image carrying ``nc`` (busybox
    # family). Ensured through the same ``pull_policy`` as a session image, so under
    # ``never`` an absent probe image raises rather than reaching out.
    readiness_probe_image: str = "busybox:latest"

    # Bound on the WHOLE probe — image ensure, container create/start, resolver
    # discovery, and both dials. On expiry the create is refused loudly: the engine is
    # not provably ready in time, never a session on an unproven engine.
    readiness_probe_timeout_seconds: float = 30.0


@settings_cache
def docker_sandbox_settings() -> DockerSandboxSettings:
    return DockerSandboxSettings()  # pyright: ignore[reportCallIssue]
