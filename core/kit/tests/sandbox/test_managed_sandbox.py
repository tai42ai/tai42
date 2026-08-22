"""``ManagedSandbox`` ledger, TTL, reap, and orphan-recovery bookkeeping.

Everything here is the SHIPPED base's own logic, exercised through the fake's
primitives: the ledger and its workspace index, the ``remove_workspace``
distinction reap and destroy carry, idempotent teardown, and the loud orphan
audit.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from tai42_contract.sandbox import (
    SandboxError,
    SandboxSessionNotFoundError,
    SandboxSessionSpec,
    SandboxSpecRejectedError,
)

from tai42_kit.sandbox import (
    LABEL_DURABILITY,
    LABEL_SANDBOX,
    LABEL_WORKSPACE,
    ManagedSandbox,
    permissive_policy,
)

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


async def test_create_registers_in_the_ledger_and_workspace_index() -> None:
    sandbox = _bound()
    session = await sandbox.create_session(_spec(workspace_key="alpha"))

    assert await sandbox.get_session(session.id) is session
    assert sandbox._by_workspace["alpha"] == {session.id}


async def test_standard_labels_are_stamped_on_the_effective_spec() -> None:
    sandbox = _bound()
    session = await sandbox.create_session(_spec(workspace_key="alpha", labels={"team": "x"}))

    # The provider-facing effective spec carries the reserved markers...
    labels = sandbox.created_specs[-1].labels
    assert labels[LABEL_SANDBOX] == "1"
    assert labels[LABEL_WORKSPACE] == "alpha"
    assert labels[LABEL_DURABILITY] == "ephemeral"
    assert labels["team"] == "x"

    # ...but info().labels round-trips ONLY the consumer's labels, never the markers.
    assert sandbox.session_info(session.id).labels == {"team": "x"}


async def test_a_consumer_label_in_the_reserved_namespace_is_rejected_loudly() -> None:
    sandbox = _bound()
    with pytest.raises(SandboxSpecRejectedError):
        await sandbox.create_session(_spec(workspace_key="alpha", labels={LABEL_SANDBOX: "spoof"}))

    # Rejected at the chokepoint, BEFORE the provider primitive runs.
    assert sandbox.created_specs == []


async def test_get_session_raises_not_found_when_absent() -> None:
    sandbox = _bound()
    with pytest.raises(SandboxSessionNotFoundError):
        await sandbox.get_session("nope")


async def test_list_sessions_reports_every_live_session() -> None:
    sandbox = _bound()
    first = await sandbox.create_session(_spec(workspace_key="a"))
    second = await sandbox.create_session(_spec(workspace_key="b"))

    ids = {info.id for info in await sandbox.list_sessions()}
    assert ids == {first.id, second.id}


async def test_reap_destroys_only_expired_and_returns_their_ids() -> None:
    sandbox = _bound()
    live = await sandbox.create_session(_spec(workspace_key="live", ttl_seconds=1000))
    stale = await sandbox.create_session(_spec(workspace_key="stale", ttl_seconds=10))

    sandbox.clock += timedelta(seconds=100)
    reaped = await sandbox.reap()

    assert reaped == [stale.id]
    assert (stale.id, False) in sandbox.destroyed  # reap preserves the workspace
    assert await sandbox.get_session(live.id) is live
    with pytest.raises(SandboxSessionNotFoundError):
        await sandbox.get_session(stale.id)


async def test_reap_preserves_a_persistent_workspace() -> None:
    sandbox = _bound()
    first = await sandbox.create_session(_spec(workspace_key="keep", durability="persistent", ttl_seconds=1))
    await first.put_file("data.txt", b"kept")

    sandbox.clock += timedelta(seconds=10)
    await sandbox.reap()

    second = await sandbox.create_session(_spec(workspace_key="keep", durability="persistent"))
    assert await second.get_file("data.txt") == b"kept"


async def test_destroy_session_removes_the_workspace_and_is_idempotent() -> None:
    sandbox = _bound()
    session = await sandbox.create_session(_spec(workspace_key="gone", durability="persistent"))
    await session.put_file("data.txt", b"bytes")

    await sandbox.destroy_session(session.id)
    assert (session.id, True) in sandbox.destroyed
    assert "gone" not in sandbox._by_workspace

    # Idempotent on an already-gone session: no raise, no second teardown.
    await sandbox.destroy_session(session.id)
    assert sandbox.destroyed.count((session.id, True)) == 1

    replacement = await sandbox.create_session(_spec(workspace_key="gone", durability="persistent"))
    with pytest.raises(SandboxError):
        await replacement.get_file("data.txt")


async def test_recover_orphans_logs_loudly_and_returns_them(caplog: pytest.LogCaptureFixture) -> None:
    sandbox = FakeSandbox(orphans=["orphan-a", "orphan-b"])
    with caplog.at_level(logging.WARNING, logger="tai42_kit.sandbox.base"):
        recovered = await sandbox.recover_orphans()

    assert recovered == ["orphan-a", "orphan-b"]
    assert "orphan-a" in caplog.text
    assert "orphan-b" in caplog.text


async def test_recover_orphans_is_silent_when_none_remain(caplog: pytest.LogCaptureFixture) -> None:
    sandbox = FakeSandbox()
    with caplog.at_level(logging.WARNING, logger="tai42_kit.sandbox.base"):
        assert await sandbox.recover_orphans() == []
    assert caplog.records == []


def test_forgetting_an_unknown_session_is_a_no_op() -> None:
    # Defensive: the internal forget never assumes the id is still present.
    sandbox = FakeSandbox()
    sandbox._forget("never-existed")
    assert sandbox._ledger == {}


async def test_the_default_clock_tracks_wall_time() -> None:
    # The fake overrides the clock; the shipped base reads real UTC time. A create
    # with the real clock stamps expires_at at ttl seconds past a near-now instant.
    class _WallClockSandbox(FakeSandbox):
        def _now(self) -> datetime:
            return ManagedSandbox._now(self)

    sandbox = _WallClockSandbox()
    sandbox.bind_policy(permissive_policy())
    before = datetime.now(UTC)
    session = await sandbox.create_session(_spec(ttl_seconds=300))
    info = await session.info()
    assert before <= info.created_at <= datetime.now(UTC)
    assert info.expires_at == info.created_at + timedelta(seconds=300)
