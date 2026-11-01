"""A marketplace operation's fleet recycle roll runs with the fleet key held shared, after its changes.

The operation changes the plugins directory and the install store under the EXCLUSIVE fleet key.
A worker the roll restarts verifies its plugin environments at boot under the SHARED key, so the
roll runs once those changes are made, with the key downgraded to shared on the same session:
the restarted worker's verification takes its shared hold and reads the finished operation, while
a second operation — on another worker or on this one — is still refused until the roll ends.

The real :class:`ConfigService` pipeline and the real fleet locks run over an in-process model of
PostgreSQL's session-level advisory locks; the roll itself is the seam that, mid-roll, stands in
for the restarted worker and for the second operation.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
from pathlib import Path
from typing import Any, cast

import pytest
from tai42_contract.plugins import PluginSpec

import tai42_skeleton.backend.settings  # noqa: F401  — declares BACKEND_MANIFEST_KEY
from tai42_skeleton.app.recycle import RECYCLED, FreshLife, RecycleReport, RecycleRow
from tai42_skeleton.db.lock_keys import MARKETPLACE_LOCK_KEY
from tai42_skeleton.marketplace import locks
from tai42_skeleton.marketplace.environments import boot_verification
from tai42_skeleton.marketplace.environments.targets import Targets
from tai42_skeleton.marketplace.environments.verify import Verification
from tai42_skeleton.marketplace.errors import OperationInProgressError
from tai42_skeleton.marketplace.installer import Installer

from .._fakes.bus import FakeBus
from .._fakes.session_advisory_keys import SessionAdvisoryKeys
from ..config.fake_pipeline import (
    FakeConfigStore,
    _reset_settings_after,  # noqa: F401  — autouse fixture, active on import
    _service,
    _with_bus,
)
from ._specs import make_resolved, make_spec, mcp_server_item
from .test_installer import FakePip, FakeRegistry, FakeStore

_RECYCLE_KEY = "BACKEND_MANIFEST_KEY"


def _relay_spec(version: str = "1.0.0") -> PluginSpec:
    """A descriptor-only plugin whose server reads the recycle-class key through an ``!ENV`` marker."""
    return make_spec(
        name="relay-srv",
        package=None,
        contract=None,
        version=version,
        provides=[{**mcp_server_item(), "mcp": {"command": "srv-mcp", "env": {"KEY": f"!ENV ${{{_RECYCLE_KEY}}}"}}}],
    )


class _RollProbe:
    """What a restarted worker and a second operation met while each roll ran."""

    def __init__(self) -> None:
        self.rolls = 0
        self.writer_self_deferred: list[bool] = []
        self.verified_rows: list[list[tuple[str, str]]] = []
        self.verification_timed_out = False
        self.holders_during_roll: list[dict[str, list[int]]] = []
        self.other_worker_refused: list[bool] = []
        self.same_worker_refused: list[bool] = []


class _World:
    """One writer worker running the real installer and pipeline, a sibling the roll restarts, and the keys."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, keys: SessionAdvisoryKeys, root: Path) -> None:
        monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")
        _with_bus(monkeypatch)
        self.keys = keys
        self.events: list[str] = []
        self.store = FakeStore(self.events)
        self.registry = FakeRegistry(self.events)
        self.config_store = FakeConfigStore(manifest={"mcp": []}, env={_RECYCLE_KEY: "OLD"})
        self.probe = _RollProbe()
        self._monkeypatch = monkeypatch
        self._verification_at_boot(root)
        service, _admin, _bus = _service(
            self.config_store, bus=FakeBus(origin="serve-writer", remotes=["serve-b"]), orchestrate=self._roll
        )
        self.installer = Installer(
            registry=cast("Any", self.registry),
            pip_runner=FakePip(self.events),
            store=cast("Any", self.store),
            config_service=service,
            config_manager=self.config_store,
            plugins_dir=lambda: root,
            owned_routes=list,
            reserved_prefixes=tuple,
        )

    def _verification_at_boot(self, root: Path) -> None:
        """A freshly started process's boot verification, reading the store and recording the rows it saw."""

        def verify(plugins_dir: Path | None, rows: list[Any]) -> Verification:
            self.probe.verified_rows.append([(row.ref, row.version) for row in rows])
            return Verification(plugins_dir=plugins_dir, targets=Targets(), environments=())

        self._monkeypatch.setattr(boot_verification, "component_store_configured", lambda component: True)
        self._monkeypatch.setattr(boot_verification, "MarketplaceInstallStore", lambda: self.store)
        self._monkeypatch.setattr(boot_verification, "verify_environments", verify)
        self._monkeypatch.setattr(boot_verification, "configured_plugins_dir", lambda: root)

    async def _roll(self, bus: Any, **kwargs: Any) -> RecycleReport:
        probe = self.probe
        probe.rolls += 1
        probe.writer_self_deferred.append(kwargs["applier_self_deferred"])
        probe.holders_during_roll.append(self.keys.holders(MARKETPLACE_LOCK_KEY))
        # The worker this roll restarts boots and verifies its plugin environments.
        self._monkeypatch.setattr(boot_verification._VERIFIED, "done", False)
        try:
            await asyncio.wait_for(boot_verification.verify_environments_at_boot(), timeout=2)
        except TimeoutError:
            probe.verification_timed_out = True
        # An operation on another worker is refused by the fleet key.
        refused = False
        try:
            async with locks.fleet_lock():
                pass
        except OperationInProgressError:
            refused = True
        probe.other_worker_refused.append(refused)
        # A second operation on this worker is refused too.
        refused = False
        try:
            await self.installer.uninstall(_relay_spec().ref)
        except OperationInProgressError:
            refused = True
        probe.same_worker_refused.append(refused)
        return RecycleReport(
            rows=[RecycleRow(name="serve-b", kind="serve", generation_before=1, status=RECYCLED)],
            fresh=[FreshLife(name="serve-b", kind="serve", generation=2)],
        )

    async def install(self, value: str) -> dict[str, Any]:
        spec = _relay_spec()
        self.registry.resolved = make_resolved(spec)
        return await self.installer.install(spec.ref, env={_RECYCLE_KEY: value})

    def assert_every_roll_held_the_key_shared_and_refused_a_second_operation(self) -> None:
        probe = self.probe
        assert probe.verification_timed_out is False
        for holders in probe.holders_during_roll:
            assert holders["exclusive"] == []
            assert len(holders["shared"]) == 1
        assert probe.other_worker_refused == [True] * probe.rolls
        assert probe.same_worker_refused == [True] * probe.rolls

    def assert_the_key_is_free(self) -> None:
        assert self.keys.holders(MARKETPLACE_LOCK_KEY) == {"exclusive": [], "shared": []}
        assert locks.operation_lock.locked() is False


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch) -> SessionAdvisoryKeys:
    advisory = SessionAdvisoryKeys()
    monkeypatch.setattr(locks, "pinned_connection", advisory.pinned_connection)
    monkeypatch.setattr(locks, "component_store_settings", lambda component: None)
    return advisory


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, keys: SessionAdvisoryKeys, tmp_path: Path) -> _World:
    return _World(monkeypatch, keys, tmp_path)


async def test_a_restarted_worker_verifies_during_the_roll_and_a_second_operation_is_refused_until_it_ends(
    world: _World,
) -> None:
    receipt = await world.install("NEW")

    assert world.probe.rolls == 1
    # The restarted worker read the finished install: the row was written before the roll.
    assert world.probe.verified_rows == [[(_relay_spec().ref, "1.0.0")]]
    # The operation's own session held the key shared, and only shared, while it rolled.
    world.assert_every_roll_held_the_key_shared_and_refused_a_second_operation()
    # The receipt carries the roll, and the writer's own recycle is armed after its reply.
    assert world.probe.writer_self_deferred == [True]
    assert [row.name for row in receipt["reload"].recycle.rows] == ["serve-b"]
    assert receipt["reload"].self_exit_armed is True
    assert receipt["reload"].awaiting_respawn == []
    world.assert_the_key_is_free()


async def test_an_update_rolls_after_its_row_is_upserted_with_the_key_held_shared(world: _World) -> None:
    await world.install("NEW")
    newer = _relay_spec("1.1.0")
    world.registry.resolved = make_resolved(newer)

    receipt = await world.installer.update(newer.ref, env={_RECYCLE_KEY: "NEWER"})

    assert world.config_store.env[_RECYCLE_KEY] == "NEWER"
    assert world.probe.rolls == 2
    # The worker the update's roll restarts reads the upgraded row.
    assert world.probe.verified_rows[-1] == [(newer.ref, "1.1.0")]
    world.assert_every_roll_held_the_key_shared_and_refused_a_second_operation()
    assert world.probe.writer_self_deferred == [True, True]
    assert receipt["reload"].self_exit_armed is True
    world.assert_the_key_is_free()


async def test_a_failed_install_rolls_its_landed_and_reverted_change_once_with_no_writer_exit(world: _World) -> None:
    world.store.record_error = RuntimeError("attribution write failed")

    with pytest.raises(RuntimeError, match="attribution write failed"):
        await world.install("NEW")

    # The forward change and the unwind's revert both landed and moved the key; one roll covers them.
    assert world.config_store.env[_RECYCLE_KEY] == "OLD"
    assert world.probe.rolls == 1
    assert world.probe.writer_self_deferred == [False]
    # The restarted worker reads the unwound store: no row.
    assert world.probe.verified_rows == [[]]
    world.assert_every_roll_held_the_key_shared_and_refused_a_second_operation()
    world.assert_the_key_is_free()
