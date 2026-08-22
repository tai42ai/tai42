"""The kit session-create policy chokepoint.

Every consumer flows through ``create_session``, which enforces the bound
:class:`SandboxPolicy` BEFORE the provider primitive runs. Each violation is a
LOUD :class:`SandboxSpecRejectedError` (never a silent clamp or downgrade); an
unbound policy is a loud programming error; and the resolved effective isolation
reaches the provider as a concrete level.
"""

from __future__ import annotations

import pytest
from tai42_contract.sandbox import (
    SandboxIsolation,
    SandboxNetwork,
    SandboxPolicy,
    SandboxSessionSpec,
    SandboxSpecRejectedError,
)

from .fakes import FakeSandbox


def _policy(
    *,
    egress: SandboxNetwork = "egress",
    isolation: SandboxIsolation = "none",
    scrub_transcript: bool = False,
    durable: bool = True,
) -> SandboxPolicy:
    return SandboxPolicy(egress=egress, isolation=isolation, scrub_transcript=scrub_transcript, durable=durable)


def _spec(**overrides: object) -> SandboxSessionSpec:
    base: dict[str, object] = {
        "image": "fake:image",
        "workspace_key": "ws",
        "durability": "ephemeral",
        "network": "egress",
        "ttl_seconds": 300,
    }
    base.update(overrides)
    return SandboxSessionSpec(**base)  # pyright: ignore[reportArgumentType]


async def test_no_policy_bound_is_a_loud_programming_error() -> None:
    sandbox = FakeSandbox()
    with pytest.raises(RuntimeError, match="no sandbox policy is bound"):
        await sandbox.create_session(_spec())


async def test_network_looser_than_the_ceiling_is_rejected() -> None:
    sandbox = FakeSandbox()
    sandbox.bind_policy(_policy(egress="internal"))
    with pytest.raises(SandboxSpecRejectedError, match="looser than the egress ceiling"):
        await sandbox.create_session(_spec(network="egress"))


async def test_network_at_or_tighter_than_the_ceiling_is_accepted() -> None:
    sandbox = FakeSandbox()
    sandbox.bind_policy(_policy(egress="egress"))
    await sandbox.create_session(_spec(network="internal"))


async def test_isolation_below_the_floor_is_rejected() -> None:
    sandbox = FakeSandbox()
    sandbox.bind_policy(_policy(isolation="vm"))
    with pytest.raises(SandboxSpecRejectedError, match="below the floor"):
        await sandbox.create_session(_spec(isolation="container"))


async def test_unset_isolation_inherits_the_floor_as_a_concrete_level() -> None:
    sandbox = FakeSandbox()
    sandbox.bind_policy(_policy(isolation="container"))
    await sandbox.create_session(_spec(isolation=None))
    assert sandbox.created_specs[-1].isolation == "container"


async def test_isolation_at_or_above_the_floor_is_requested_verbatim() -> None:
    sandbox = FakeSandbox()
    sandbox.bind_policy(_policy(isolation="none"))
    await sandbox.create_session(_spec(isolation="vm"))
    assert sandbox.created_specs[-1].isolation == "vm"


async def test_persistent_while_durable_off_is_rejected_before_the_primitive() -> None:
    sandbox = FakeSandbox()
    sandbox.bind_policy(_policy(durable=False))
    with pytest.raises(SandboxSpecRejectedError, match="durable workspaces are disabled"):
        await sandbox.create_session(_spec(durability="persistent"))
    assert sandbox.created_specs == []  # rejected before the provider primitive ran


async def test_persistent_while_durable_on_is_accepted() -> None:
    sandbox = FakeSandbox()
    sandbox.bind_policy(_policy(durable=True))
    await sandbox.create_session(_spec(durability="persistent"))


async def test_scrub_transcript_is_not_a_create_time_gate() -> None:
    # It is carried on the policy for the consumer to read, never enforced here.
    for scrub in (True, False):
        sandbox = FakeSandbox()
        sandbox.bind_policy(_policy(scrub_transcript=scrub))
        await sandbox.create_session(_spec())
