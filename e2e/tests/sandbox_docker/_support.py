"""Shared coordinates and the live-engine harness for the docker-gated sandbox leg.

This module is imported ONLY when the ``sandbox_docker`` collection gate is on
(``TAI_E2E_SANDBOX_DOCKER=1`` feeds ``HarnessSettings.sandbox_docker``, which the
parent ``tests/conftest.py`` passes to ``gated_collect_ignore``). With the gate off
the whole directory stays out of collection, so the real
``tai42_sandbox_docker`` provider is never imported when docker is absent — the
suite cannot break a bare ``pytest`` run.

The provider registers itself against ``tai42_app`` as an import side effect, so a
recording stub app is bound HERE first — the same bind-before-import the plugin's
own unit conftest does — before the provider module loads. Nothing else in the
pytest process touches ``tai42_app`` (the SUT app runs in the harness's spawned
subprocesses, never in-process), so the stub bind is inert for every other suite.

A live rootless-dind engine is addressed by ``SANDBOX_DOCKER_TEST_HOST`` (the tcp
endpoint the harness ``sandbox`` compose profile exposes on loopback). When that is
unset every test skips LOUDLY with the reason — the engine env is a hard
prerequisite, never silently stubbed.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from tai42_contract.sandbox import SandboxError, SandboxSessionSpec
from tai42_kit.sandbox import ManagedSandbox, ManagedSandboxSession, permissive_policy


class _StubSandboxes:
    """Captures the provider's import-time ``register_sandbox`` so the real
    registration lands somewhere inert instead of raising on an unbound app."""

    def register_sandbox(self, cls: type) -> type:
        return cls


class _StubApp:
    def __init__(self) -> None:
        self.sandboxes = _StubSandboxes()


from tai42_contract.app import tai42_app  # noqa: E402  (bind must precede the provider import)

tai42_app.bind(_StubApp())

from tai42_sandbox_docker.provider import DockerSandbox  # noqa: E402  (registers against the bound stub)
from tai42_sandbox_docker.settings import DockerSandboxSettings  # noqa: E402

# -- engine coordinates ----------------------------------------------------------

TEST_HOST = os.environ.get("SANDBOX_DOCKER_TEST_HOST")
TEST_IMAGE = os.environ.get("SANDBOX_DOCKER_TEST_IMAGE", "busybox:latest")

SKIP_REASON = (
    "SANDBOX_DOCKER_TEST_HOST is unset: the docker-gated sandbox leg needs a live "
    "rootless-dind engine (the harness `sandbox` compose profile) and runs on CI / an "
    "engine host only, behind TAI_E2E_SANDBOX_DOCKER=1."
)

# Even behind the collection gate, a set flag without a reachable engine skips loudly
# rather than trying to dial a socket that is not there.
requires_engine = pytest.mark.skipif(not TEST_HOST, reason=SKIP_REASON)


def _shared_cert_settings() -> dict[str, str]:
    """Point the host-run SUT at the mTLS client certs the ``sandbox-engine`` shares
    through the compose bind mount (``./.sandbox-certs`` -> ``/certs/client``).

    The provider's canonical default is the IN-CONTAINER ``/certs/client`` mount,
    which does not exist on the host that runs this leg — so the harness resolves the
    bind-mount host dir here (honouring ``TAI_E2E_SANDBOX_CERT_DIR`` exactly as the
    compose file does, else ``e2e/.sandbox-certs``). This is why the CI step sets only
    ``SANDBOX_DOCKER_TEST_HOST`` and no cert env. An explicit ``SANDBOX_DOCKER_TLS_*``
    still wins (we only fill a path the environment did not set)."""
    configured = os.environ.get("TAI_E2E_SANDBOX_CERT_DIR", "")
    cert_dir = Path(configured).expanduser() if configured else Path(__file__).resolve().parents[2] / ".sandbox-certs"
    cert_dir = cert_dir.resolve()
    overrides: dict[str, str] = {}
    for key, filename in (("tls_cert_path", "cert.pem"), ("tls_key_path", "key.pem"), ("tls_ca_path", "ca.pem")):
        if f"SANDBOX_DOCKER_{key.upper()}" not in os.environ:
            overrides[key] = str(cert_dir / filename)
    return overrides


def build_sandbox() -> DockerSandbox:
    """A ``DockerSandbox`` pointed at the live engine. ``host`` comes from
    ``SANDBOX_DOCKER_TEST_HOST`` (a ``tcp://`` endpoint — the provider normalizes it
    to mTLS ``https://`` itself); the mTLS cert paths resolve to the engine's shared
    bind-mount dir (see :func:`_shared_cert_settings`); every other ``SANDBOX_DOCKER_*``
    knob resolves from the environment, exactly as in a deployment."""
    assert TEST_HOST is not None  # guarded by requires_engine at the call sites
    return DockerSandbox(settings=DockerSandboxSettings(host=TEST_HOST, **_shared_cert_settings()))


@contextlib.asynccontextmanager
async def open_sandbox() -> AsyncIterator[DockerSandbox]:
    """Yield a policy-bound live ``DockerSandbox`` and, on exit, destroy every
    session still on its ledger and close the engine client.

    The bound policy is the most permissive one (egress open, no isolation floor,
    persistent allowed) so only a PROVIDER limitation — never the policy chokepoint
    — shapes what a session gets; the negatives below prove the ENGINE topology, not
    the operator policy. The client close keeps the suite clean under the harness's
    ``filterwarnings=error`` (an unclosed aiohttp session is a hard error)."""
    sandbox = build_sandbox()
    sandbox.bind_policy(permissive_policy())
    try:
        yield sandbox
    finally:
        for info in await sandbox.list_sessions():
            with contextlib.suppress(SandboxError):
                await sandbox.destroy_session(info.id)
        # The provider exposes no public close; the aiodocker client it lazily built
        # is the one resource to release, and closing it twice is a documented no-op.
        client = sandbox._client
        if client is not None:
            await client.close()


def egress_spec(*, workspace_key: str) -> SandboxSessionSpec:
    """An ephemeral session on the egress tier — the tier whose real network
    topology the isolation negatives interrogate."""
    return SandboxSessionSpec(
        image=TEST_IMAGE,
        workspace_key=workspace_key,
        durability="ephemeral",
        network="egress",
        ttl_seconds=300,
    )


# -- in-session network probes ---------------------------------------------------

_PROBE_WAIT = 4


async def tcp_dials(session: ManagedSandboxSession, host: str, port: int, *, payload: bytes = b"") -> bool:
    """Whether a busybox ``nc`` TCP dial from INSIDE the session established a
    connection to ``host:port``. Exit 0 means the socket opened; a refused peer or a
    firewall-DROPPED destination exits nonzero once ``-w`` elapses. ``payload`` is
    written after connect (used to probe an application-layer response)."""
    result = await session.exec(
        ["nc", "-w", str(_PROBE_WAIT), host, str(port)],
        stdin=payload,
        timeout_seconds=_PROBE_WAIT + 20,
    )
    return result.exit_code == 0


async def http_over_tcp(session: ManagedSandboxSession, host: str, port: int, path: str) -> str:
    """The raw response text of a plaintext HTTP request to ``host:port`` over
    ``nc``. Used to prove an endpoint is NOT a usable Docker control API — a
    TLS-only daemon never answers a plaintext request with a version payload."""
    request = f"GET {path} HTTP/1.0\r\nHost: sandbox\r\n\r\n".encode()
    result = await session.exec(
        ["nc", "-w", str(_PROBE_WAIT), host, str(port)],
        stdin=request,
        timeout_seconds=_PROBE_WAIT + 20,
    )
    return result.stdout


async def resolves_dns(session: ManagedSandboxSession, name: str) -> bool:
    """Whether the session can resolve ``name`` — DNS egress works."""
    result = await session.exec(["nslookup", name], stdin=b"", timeout_seconds=25)
    return result.exit_code == 0


async def default_gateway(session: ManagedSandboxSession) -> str:
    """The session's default-route gateway address — the inner-bridge gateway the
    dind engine's control API is reachable at from an inner container."""
    result = await session.exec(["ip", "route", "show", "default"], stdin=b"", timeout_seconds=25)
    for line in result.stdout.splitlines():
        parts = line.split()
        if parts[:2] == ["default", "via"] and len(parts) >= 3:
            return parts[2]
    raise AssertionError(f"no default gateway in `ip route` output: {result.stdout!r}")


async def sh(session: ManagedSandboxSession, script: str) -> str:
    """Run a ``sh -c`` snippet in the session and return its stdout (stripped)."""
    result = await session.exec(["sh", "-c", script], stdin=b"", timeout_seconds=25)
    return result.stdout.strip()


__all__ = [
    "TEST_HOST",
    "TEST_IMAGE",
    "DockerSandbox",
    "ManagedSandbox",
    "ManagedSandboxSession",
    "default_gateway",
    "egress_spec",
    "http_over_tcp",
    "open_sandbox",
    "requires_engine",
    "resolves_dns",
    "sh",
    "tcp_dials",
]
