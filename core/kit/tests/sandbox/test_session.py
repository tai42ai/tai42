"""``ManagedSandboxSession`` base methods — the bookkeeping a provider inherits.

``id`` / ``info()`` / ``touch()`` / ``destroy()`` all route through the owning
:class:`ManagedSandbox` ledger, so a provider reimplements none of them.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from tai42_contract.sandbox import SandboxSessionNotFoundError, SandboxSessionSpec

from tai42_kit.sandbox import permissive_policy

from .fakes import FakeSandbox


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


def _bound() -> FakeSandbox:
    sandbox = FakeSandbox()
    sandbox.bind_policy(permissive_policy())
    return sandbox


async def test_id_is_the_provider_assigned_id() -> None:
    sandbox = _bound()
    session = await sandbox.create_session(_spec())
    assert session.id == "sess-0"


async def test_info_is_built_from_the_ledger_and_round_trips_workspace_path() -> None:
    sandbox = _bound()
    session = await sandbox.create_session(_spec(workspace_key="alpha", labels={"k": "v"}))

    info = await session.info()
    assert info.id == session.id
    assert info.workspace_key == "alpha"
    assert info.workspace_path == session.workspace_path
    assert info.durability == "ephemeral"
    assert info.labels["k"] == "v"
    assert info.expires_at == info.created_at + timedelta(seconds=300)


async def test_touch_extends_expires_at_through_the_ledger() -> None:
    sandbox = _bound()
    session = await sandbox.create_session(_spec())
    before = (await session.info()).expires_at

    sandbox.clock += timedelta(seconds=120)
    await session.touch()

    assert (await session.info()).expires_at == before + timedelta(seconds=120)


async def test_destroy_delegates_through_the_owning_sandbox() -> None:
    sandbox = _bound()
    session = await sandbox.create_session(_spec())

    await session.destroy()

    assert (session.id, True) in sandbox.destroyed
    with pytest.raises(SandboxSessionNotFoundError):
        await sandbox.get_session(session.id)
