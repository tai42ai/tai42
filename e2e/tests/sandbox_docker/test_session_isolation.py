"""The docker session-isolation negatives against the REAL network topology.

These are the e2e teeth behind the ENFORCED topology the deployment ships (the
rootless-dind engine, the ``sandbox-ctrl`` network split, the egress firewall denying
RFC1918 + cloud metadata, and the mTLS control API). They are the ONLY legs that
exercise the real network topology; every other sandbox leg rides the process-based
fake. From inside a live ``egress`` session (an ``exec`` running a busybox probe) they
assert:

1. the dind control API is UNUSABLE from a session — reachable at the inner-bridge
   gateway on :2376 only across the daemon's INPUT chain, where mTLS (no client cert)
   is the backstop, so a plaintext dial never gets a Docker version payload; and any
   sandbox-ctrl / RFC1918 control-plane or compose-service address is DROPPED by the
   egress firewall on the FORWARD/POSTROUTING path;
2. cloud metadata (``169.254.169.254``) is BLOCKED;
3. public internet egress + DNS SUCCEED (egress default open);
4. the session carries NO engine credential and mounts ONLY its own ``/workspace``, and
   two sessions are workspace-isolated from each other.

Docker-gated (the parent conftest keeps the module out of collection unless
``TAI_E2E_SANDBOX_DOCKER=1``) and skipped LOUDLY when ``SANDBOX_DOCKER_TEST_HOST`` names
no engine. A topology-specific coordinate that the harness does not expose skips its own
leg loudly rather than passing vacuously.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest

from ._support import (
    DockerSandbox,
    ManagedSandboxSession,
    default_gateway,
    egress_spec,
    http_over_tcp,
    open_sandbox,
    requires_engine,
    resolves_dns,
    sh,
    tcp_dials,
)

pytestmark = requires_engine

# The cloud-metadata endpoint is a universal constant (link-local), so its block is
# asserted unconditionally. The public egress target and the RFC1918 control-plane
# address the firewall must DROP are deployment coordinates: the public target has a
# sensible default the harness can override, and the blocked address — a host that is
# genuinely LISTENING so a DROP is distinguishable from a dead route — is supplied by
# the harness or its leg skips loudly.
_METADATA_HOST = "169.254.169.254"
_METADATA_PORT = 80

_EGRESS_HOST = os.environ.get("SANDBOX_DOCKER_EGRESS_HOST", "1.1.1.1")
_EGRESS_PORT = int(os.environ.get("SANDBOX_DOCKER_EGRESS_PORT", "443"))
_EGRESS_DNS = os.environ.get("SANDBOX_DOCKER_EGRESS_DNS", "one.one.one.one")

_CONTROL_API_PORT = int(os.environ.get("SANDBOX_DOCKER_CONTROL_API_PORT", "2376"))
_BLOCKED_ADDR = os.environ.get("SANDBOX_DOCKER_BLOCKED_ADDR")


@pytest.fixture
async def sandbox() -> AsyncIterator[DockerSandbox]:
    async with open_sandbox() as live:
        yield live


@pytest.fixture
async def egress_session(sandbox: DockerSandbox) -> ManagedSandboxSession:
    # The session is torn down by the sandbox fixture's ledger sweep on exit.
    return await sandbox.create_session(egress_spec(workspace_key="iso-egress"))


async def test_public_egress_open(egress_session: ManagedSandboxSession) -> None:
    """The egress default is OPEN: the session resolves a public name and opens a TCP
    connection to a public host — the positive that makes the blocks below meaningful."""
    assert await resolves_dns(egress_session, _EGRESS_DNS), (
        f"the egress session could not resolve {_EGRESS_DNS!r}: DNS egress is not open"
    )
    assert await tcp_dials(egress_session, _EGRESS_HOST, _EGRESS_PORT), (
        f"the egress session could not reach {_EGRESS_HOST}:{_EGRESS_PORT}: public egress is not open"
    )


async def test_metadata_endpoint_blocked(egress_session: ManagedSandboxSession) -> None:
    """The cloud metadata endpoint is BLOCKED by the egress firewall — a session can
    never reach the instance credential service."""
    assert not await tcp_dials(egress_session, _METADATA_HOST, _METADATA_PORT), (
        f"the session reached the cloud metadata endpoint {_METADATA_HOST}:{_METADATA_PORT}: "
        "the egress firewall is not denying it"
    )


async def test_control_api_requires_mtls(egress_session: ManagedSandboxSession) -> None:
    """The dind control API is reachable from a session only at the inner-bridge
    gateway on :2376, across the daemon's INPUT chain (NOT the FORWARD/POSTROUTING
    chains the egress-firewall drops sit on). There the mTLS client identity is the
    ONLY thing between a session and the engine: a plaintext dial, lacking a client
    cert, never gets a Docker version payload back."""
    gateway = await default_gateway(egress_session)
    response = await http_over_tcp(egress_session, gateway, _CONTROL_API_PORT, "/version")
    assert "ApiVersion" not in response, (
        f"a plaintext request to the control API at {gateway}:{_CONTROL_API_PORT} returned a Docker "
        f"version payload — the mTLS backstop is not in force: {response!r}"
    )


@pytest.mark.skipif(
    not _BLOCKED_ADDR,
    reason=(
        "SANDBOX_DOCKER_BLOCKED_ADDR is unset: the RFC1918 control-plane / compose-service "
        "drop leg needs a genuinely-listening private address the topology's egress firewall "
        "must DROP (so the block is distinguishable from a dead route). The harness supplies it; "
        "without it this leg skips rather than asserting vacuously."
    ),
)
async def test_rfc1918_control_plane_blocked(egress_session: ManagedSandboxSession) -> None:
    """A sandbox-ctrl / RFC1918 control-plane or compose-service address (serve,
    backend, datastores) is DROPPED by the egress firewall on the FORWARD/POSTROUTING
    path — a session is an inner dind container OFF the compose network and cannot
    reach any of it, even a host that is actively listening."""
    assert _BLOCKED_ADDR is not None  # guarded by the skipif
    host, _, port = _BLOCKED_ADDR.rpartition(":")
    assert host, f"SANDBOX_DOCKER_BLOCKED_ADDR must be host:port, got {_BLOCKED_ADDR!r}"
    assert port, f"SANDBOX_DOCKER_BLOCKED_ADDR must be host:port, got {_BLOCKED_ADDR!r}"
    assert not await tcp_dials(egress_session, host, int(port)), (
        f"the session reached the private control-plane address {_BLOCKED_ADDR}: the egress "
        "firewall is not dropping the FORWARD/POSTROUTING path off the sandbox network"
    )


async def test_session_has_no_engine_credentials(egress_session: ManagedSandboxSession) -> None:
    """The session mounts ONLY its own ``/workspace`` and holds NO engine credential:
    the mTLS client identity is absent, the engine socket is absent, and the workspace
    is the one writable mount — so even reaching the control API's TCP endpoint yields
    nothing to authenticate with."""
    assert await sh(egress_session, "test -e /certs/client/key.pem && echo present || echo absent") == "absent", (
        "the session filesystem carries the engine's mTLS client identity under /certs/client"
    )
    assert await sh(egress_session, "test -S /var/run/docker.sock && echo present || echo absent") == "absent", (
        "the session has the engine socket mounted at /var/run/docker.sock"
    )
    # The workspace is the session's own writable root.
    await egress_session.put_file("iso-probe.txt", b"workspace-writable")
    assert await egress_session.get_file("iso-probe.txt") == b"workspace-writable"


async def test_cross_session_workspace_isolation(sandbox: DockerSandbox) -> None:
    """Two sessions are workspace-isolated: a file written in one session's
    ``/workspace`` is not visible in another's — each ephemeral session owns a
    distinct volume, never a shared mount."""
    first = await sandbox.create_session(egress_spec(workspace_key="iso-cross-a"))
    second = await sandbox.create_session(egress_spec(workspace_key="iso-cross-b"))

    await first.put_file("secret.txt", b"first-only")
    assert first.workspace_path == second.workspace_path, (
        "both sessions expose the same workspace_path root, so a leak would be a mount, not a name"
    )
    assert await sh(second, f"test -e {second.workspace_path}/secret.txt && echo present || echo absent") == "absent", (
        "a file written in one session's workspace was visible in another's — the sessions share a mount"
    )
