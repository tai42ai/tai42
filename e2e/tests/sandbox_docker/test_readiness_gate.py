"""The egress-firewall readiness gate against the live rootless-dind engine.

The live half of the readiness gate: with the probe ENABLED, a create against the
firewalled engine SUCCEEDS — the throwaway probe container's lifecycle, its resolver
discovery, and its two dials all run end-to-end against the real DOCKER-USER chain,
and the create is allowed through. The provider derives both probe targets (the deny
target from its configured engine control address, the allow target from the probe
container's own ``/etc/resolv.conf``), so no target coordinate is supplied here.

The NEGATIVE half (firewall absent ⇒ create refused loudly) is proven at the UNIT
layer (the plugin's ``tests/test_readiness.py``) with a fake engine, because this leg
cannot uninstall the DOCKER-USER chain without privilege it does not hold.

Boundary: the harness addresses the engine over a loopback forward
(``SANDBOX_DOCKER_TEST_HOST``), so the DERIVED deny target resolves to loopback rather
than the in-cluster ``sandbox-ctrl`` control address a real deployment dials — this
leg proves the gate MECHANISM runs green against a live engine, while the sibling
``test_rfc1918_control_plane_blocked`` proves the actual FORWARD-chain drop.

Docker-gated (the parent conftest keeps the module out of collection unless
``TAI_E2E_SANDBOX_DOCKER=1``) and skipped LOUDLY when ``SANDBOX_DOCKER_TEST_HOST``
names no engine.
"""

from __future__ import annotations

from ._support import (
    egress_spec,
    open_sandbox,
    requires_engine,
)

pytestmark = requires_engine


async def test_create_succeeds_with_readiness_probe_enabled() -> None:
    """With the readiness probe enabled, a create against the live firewalled engine
    succeeds: the probe passed against the real DOCKER-USER chain and the engine is
    marked verified."""
    async with open_sandbox(readiness=True) as sandbox:
        session = await sandbox.create_session(egress_spec(workspace_key="readiness-live"))
        assert session.id, "the readiness probe passed but no session was created"
        assert sandbox._engine_verified is True, "the create succeeded but the engine was not marked verified"
