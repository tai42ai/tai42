"""The kit sandbox conformance suite against the REAL Docker provider over a live engine.

One full lifecycle pass — create, exec + ``spec.env`` credential channel, interactive
byte-stream, put/get, workspace-path resolution, consumer-label round-trip, touch,
persistent-survives-reap, ttl reap + idempotent destroy, loud spec rejection, and
exec-timeout kill — driven by :func:`tai42_kit.sandbox.run_sandbox_conformance` against
:class:`~tai42_sandbox_docker.provider.DockerSandbox` connected to the rootless-dind
engine the harness ``sandbox`` compose profile stands up. An empty return is the
certification that the docker provider gives every consumer the same session contract the
process-based fake does on every other e2e leg.

Docker-gated: the parent conftest keeps this module out of collection unless
``TAI_E2E_SANDBOX_DOCKER=1``, and it skips LOUDLY when ``SANDBOX_DOCKER_TEST_HOST``
names no engine.
"""

from __future__ import annotations

from tai42_contract.sandbox import SandboxSessionSpec
from tai42_kit.sandbox import SandboxConformanceConfig, run_sandbox_conformance

from ._support import TEST_IMAGE, open_sandbox, requires_engine


@requires_engine
async def test_docker_sandbox_conformance() -> None:
    # The provider-appropriate reject spec: a `vm` isolation floor is above the container
    # boundary this provider can give, so it must be refused loudly at create — the
    # conformance spec-reject case made real, not stubbed.
    reject_vm = SandboxSessionSpec(
        image=TEST_IMAGE,
        workspace_key="conf-reject-vm",
        durability="ephemeral",
        network="egress",
        isolation="vm",
        ttl_seconds=300,
    )
    config = SandboxConformanceConfig(image=TEST_IMAGE, reject_specs=(reject_vm,))
    async with open_sandbox() as sandbox:
        await run_sandbox_conformance(sandbox, config)
