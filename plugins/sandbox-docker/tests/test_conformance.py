"""The kit conformance suite against a REAL Docker engine.

Runs ONLY when ``SANDBOX_DOCKER_TEST_HOST`` names a live engine (PLAN_5's e2e leg
wires it); otherwise it skips LOUDLY with the reason. The unit suite stays green with
no docker anywhere, so this leg is additionally gated behind the ``docker`` marker.
"""

from __future__ import annotations

import os

import pytest
from tai42_contract.sandbox import SandboxSessionSpec
from tai42_kit.sandbox import SandboxConformanceConfig, run_sandbox_conformance

from tai42_sandbox_docker.provider import DockerSandbox
from tai42_sandbox_docker.settings import DockerSandboxSettings

pytestmark = pytest.mark.docker

_TEST_HOST = os.environ.get("SANDBOX_DOCKER_TEST_HOST")
_TEST_IMAGE = os.environ.get("SANDBOX_DOCKER_TEST_IMAGE", "busybox:latest")


@pytest.mark.skipif(
    not _TEST_HOST,
    reason=(
        "SANDBOX_DOCKER_TEST_HOST is not set: the live-engine sandbox conformance leg "
        "needs a real Docker engine and runs at the PLAN_5 e2e stage, not the unit gate."
    ),
)
async def test_docker_sandbox_conformance() -> None:
    assert _TEST_HOST is not None  # guarded by the skipif above
    sandbox = DockerSandbox(settings=DockerSandboxSettings(host=_TEST_HOST))
    # The provider-appropriate reject spec: a `vm` isolation floor is above the
    # container boundary this provider can give.
    reject = SandboxSessionSpec(
        image=_TEST_IMAGE,
        workspace_key="conf-reject-vm",
        durability="ephemeral",
        network="egress",
        isolation="vm",
        ttl_seconds=300,
    )
    config = SandboxConformanceConfig(image=_TEST_IMAGE, reject_specs=(reject,))
    await run_sandbox_conformance(sandbox, config)
