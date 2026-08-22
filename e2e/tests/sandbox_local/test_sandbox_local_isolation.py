"""Isolation capability of the direct/host ``sandbox-local`` provider.

The direct/host provider gives NO isolation, so it accepts EXACTLY isolation ``none``
and REJECTS anything stronger LOUDLY — never a silent downgrade of a ``container`` / ``vm``
request to a bare host process (PLAN_9; the ``none|container|vm`` Literal).

The kit create chokepoint bakes the operator ISOLATION FLOOR into every spec's effective
isolation before the provider primitive runs, so the effective tier the provider must honor
is what the operator floor resolves to:

* With the floor at ``none`` (the run/durability posture) a create resolves to isolation
  ``none`` and is ACCEPTED.
* A VARIANT whose operator floor is ``container`` resolves EVERY create to isolation
  ``container`` — above what the host provider can give — so the provider rejects every
  create with a typed ``SandboxSpecRejectedError`` at the kit create seam. A loud capability
  mismatch, never a silent fallback to an unisolated host process.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tai42_e2e.booting import boot_stack
from tai42_e2e.manifests import build_sandbox_local_stack
from tai42_e2e.stack import Infra, StackConfig, StackResources, TaiStack
from tai42_e2e.variants import Variants

pytestmark = pytest.mark.backendless


def _container_floor_stack(res: StackResources, variants: Variants) -> StackConfig:
    """``build_sandbox_local_stack`` with the operator ISOLATION FLOOR raised to
    ``container`` — a floor the host provider cannot satisfy, so every create fails loudly."""
    config = build_sandbox_local_stack(res, variants)
    config.env["TAI_MCP_SANDBOX_ISOLATION"] = "container"
    return config


@pytest.fixture(scope="module")
def container_floor_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    """The ``sandbox-local`` provider under an operator isolation floor of ``container`` —
    a capability the direct/host provider cannot honor, so every session create is refused."""
    root: Path = tmp_path_factory.mktemp("sandbox-local-container-floor")
    yield from boot_stack(infra, root, _container_floor_stack)


async def test_isolation_none_is_accepted_by_the_direct_provider(
    sandbox_local_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    # The floor is ``none`` here, so a create resolves to isolation ``none`` and the direct
    # provider accepts it — it can honestly give exactly that (no isolation).
    async with sandbox_local_stack.mcp() as mcp:
        created = (await mcp.call_tool("e2e_sandbox_probe", {"op": "create", "workspace_key": uniq("iso")})).data
        assert created["workspace_path"], f"an isolation-none create must be accepted: {created}"
        await mcp.call_tool("e2e_sandbox_probe", {"op": "destroy", "session_id": created["session_id"]})


async def test_container_isolation_floor_is_rejected_loudly(
    container_floor_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    # The floor is ``container`` here, so EVERY create resolves to isolation ``container`` —
    # above what the host provider can give — and is refused at the kit create seam.
    async with container_floor_stack.mcp() as mcp:
        rejected = await mcp.call_tool(
            "e2e_sandbox_probe", {"op": "create", "workspace_key": uniq("iso")}, raise_on_error=False
        )
    assert rejected.is_error, f"a container isolation floor must be rejected by the host provider: {rejected.data}"
    text = " ".join(getattr(part, "text", "") for part in rejected.content)
    assert "cannot enforce" in text, f"the rejection is not the loud capability mismatch: {text}"
    assert "isolation" in text, f"the rejection does not name the isolation facet: {text}"
