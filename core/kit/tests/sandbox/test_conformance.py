"""The conformance suite, run as a test against the in-memory fake.

An empty return is the certification: the shipped :class:`ManagedSandbox` base
gives the fake — which writes only its runtime primitives — the whole session
lifecycle, the ``spec.env`` credential channel, the interactive seam,
workspace-path resolution, TTL reap, persistent-vs-ephemeral survival, and loud
spec rejection. The single-tier variant proves the parameterized persistent-reject
case.
"""

from __future__ import annotations

from tai42_contract.sandbox import SandboxSessionSpec

from tai42_kit.sandbox import SandboxConformanceConfig, run_sandbox_conformance

from .fakes import FakeSandbox


def _capped_spec() -> SandboxSessionSpec:
    return SandboxSessionSpec(
        image="fake:image",
        workspace_key="capped",
        durability="ephemeral",
        network="egress",
        ttl_seconds=300,
        cpu=1.0,
    )


def _persistent_spec() -> SandboxSessionSpec:
    return SandboxSessionSpec(
        image="fake:image",
        workspace_key="persist",
        durability="persistent",
        network="egress",
        ttl_seconds=300,
    )


async def test_the_fake_is_certified() -> None:
    sandbox = FakeSandbox()
    config = SandboxConformanceConfig(image="fake:image", reject_specs=[_capped_spec()])
    await run_sandbox_conformance(sandbox, config)


async def test_a_single_tier_provider_rejects_persistent() -> None:
    # A provider with no durable storage sets check_persistent_survives=False and
    # lists a persistent spec among its reject cases; the suite drives that path.
    sandbox = FakeSandbox(supports_persistent=False)
    config = SandboxConformanceConfig(
        image="fake:image",
        reject_specs=[_capped_spec(), _persistent_spec()],
        check_persistent_survives=False,
    )
    await run_sandbox_conformance(sandbox, config)
