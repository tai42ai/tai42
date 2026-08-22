"""The kit sandbox conformance suite, run against a REAL ``LocalSandbox``.

Unlike a container provider (whose conformance needs a live engine and is an
e2e-only leg), the direct/host provider needs no external dependency — a host
subprocess is always available — so this runs as a normal local gate. Every spec
the suite builds pins ``network="egress"`` (the only tier this provider accepts),
and the provider-appropriate reject case is an unenforceable cap.
"""

from __future__ import annotations

from pydantic import SecretStr
from tai42_contract.sandbox import SandboxSessionSpec
from tai42_kit.sandbox import SandboxConformanceConfig, run_sandbox_conformance

import tai42_sandbox_local


def _cap_reject_spec() -> SandboxSessionSpec:
    """A spec this provider must reject: a resource cap it cannot enforce. Pinned to
    ``network="egress"`` so only the cap — never the network tier — is the cause."""
    return SandboxSessionSpec(
        image="host",
        workspace_key="conf-cap",
        durability="ephemeral",
        network="egress",
        cpu=1.0,
        memory_mb=256,
        ttl_seconds=300,
        env={"IGNORED": SecretStr("x")},
    )


async def test_local_sandbox_passes_kit_conformance(sandbox: tai42_sandbox_local.LocalSandbox) -> None:
    config = SandboxConformanceConfig(
        image="host",
        reject_specs=(_cap_reject_spec(),),
        # This provider supports persistent workspaces via its writable root, so the
        # persistent-survives-reap case runs (it is NOT listed as a reject).
        check_persistent_survives=True,
    )
    await run_sandbox_conformance(sandbox, config)
