"""A marketplace operation's fleet recycle roll runs with the fleet key held shared, after its changes.

The operation changes the plugins directory and the install store under the EXCLUSIVE fleet key.
A worker the roll restarts verifies its plugin environments at boot under the SHARED key, so the
roll runs once those changes are made, with the key downgraded to shared on the same session:
the restarted worker's verification takes its shared hold and reads the finished operation, while
a second operation — on another worker or on this one — is still refused until the roll ends.

An operation run under a bound (a dispatched job, an armed turn budget) hands its roll to the bus
member it acts for, which carries it out after answering. The member takes the key shared before
it answers, while the operation still holds it, and holds it until the carried roll ends, so the
key is never free while the roll runs and a second operation is refused until it ends.

The real :class:`ConfigService` pipeline and the real fleet locks run over an in-process model of
PostgreSQL's session-level advisory locks; the roll itself is the seam that, mid-roll, stands in
for the restarted worker and for the second operation.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from tai42_contract.plugins import PluginSpec
from tai42_kit.utils.run_bound import bounded_run

import tai42_skeleton.app.lifecycle.carried_roll as carried_roll_module
import tai42_skeleton.backend.settings  # noqa: F401  — declares BACKEND_MANIFEST_KEY
from tai42_skeleton.app import instance
from tai42_skeleton.app.bus import FleetResult, LocalApplyResult
from tai42_skeleton.app.bus.ops import FleetOp
from tai42_skeleton.app.lifecycle import TaiMCPLifecycleMixin
from tai42_skeleton.app.recycle import (
    RECYCLED,
    TIMED_OUT,
    FreshLife,
    RecycleError,
    RecycleReport,
    RecycleRow,
    RecycleStop,
)
from tai42_skeleton.app.writer_exit import WRITER_EXIT_REASON, WriterExitScope
from tai42_skeleton.config import roll_holds
from tai42_skeleton.db.lock_keys import MARKETPLACE_LOCK_KEY
from tai42_skeleton.marketplace import locks
from tai42_skeleton.marketplace.environments import boot_verification
from tai42_skeleton.marketplace.environments.targets import Targets
from tai42_skeleton.marketplace.environments.verify import Verification
from tai42_skeleton.marketplace.errors import OperationInProgressError, UnrunRollError
from tai42_skeleton.marketplace.installer import Installer

from .._fakes.bus import FakeBus
from .._fakes.session_advisory_keys import SessionAdvisoryKeys
from ..config.fake_pipeline import (
    FakeConfigStore,
    FakeReloadAdmin,
    _reset_settings_after,  # noqa: F401  — autouse fixture, active on import
    _service,
    _with_bus,
)
from ._specs import make_resolved, make_spec, mcp_server_item
from .test_installer import FakePip, FakeRegistry, FakeStore

# The door's edge settles the writer's exit its change owes; these tests drive the door below it.
pytestmark = pytest.mark.usefixtures("writer_exit_edge")

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


class _Bindings:
    """The value of the recycle-class key each worker bound, and what each worker's reload reports from it.

    A worker's reload reports the key while the stored value differs from the one it bound; a
    worker the roll restarts binds the stored value.
    """

    def __init__(self, store: FakeConfigStore) -> None:
        self._store = store
        self.bound = {"serve-writer": "OLD", "serve-b": "OLD"}

    def report(self, name: str) -> dict[str, Any]:
        moved = self._store.env.get(_RECYCLE_KEY) != self.bound[name]
        return {"status": "ok", "env_keys": 1, "awaiting_respawn": [_RECYCLE_KEY] if moved else []}

    def restarted(self, name: str) -> None:
        self.bound[name] = self._store.env.get(_RECYCLE_KEY, "")


class _WriterReload(FakeReloadAdmin):
    """The writer's local reload, reporting the key from the value the writer bound."""

    def __init__(self, bindings: _Bindings) -> None:
        super().__init__()
        self._bindings = bindings

    async def reload_config(self) -> dict[str, Any]:
        await super().reload_config()
        return self._bindings.report("serve-writer")


class _ReportingBus(FakeBus):
    """The fleet bus, its sibling's reload reporting the key from the value the sibling bound."""

    def __init__(self, bindings: _Bindings) -> None:
        super().__init__(origin="serve-writer", remotes=["serve-b"])
        self._bindings = bindings

    async def publish(
        self,
        op: FleetOp,
        targets: list[str] | None,
        local: LocalApplyResult | None,
        *,
        expected_at_start: dict[str, int] | None = None,
    ) -> FleetResult:
        self.remote_payloads["serve-b"] = self._bindings.report("serve-b")
        return await super().publish(op, targets, local, expected_at_start=expected_at_start)


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
        # Set, the roll stops at serve-b with this detail instead of recycling it.
        self.roll_stops: str | None = None
        # Set, the roll waits for it before it starts.
        self.roll_gate: asyncio.Event | None = None
        self._monkeypatch = monkeypatch
        self._verification_at_boot(root)
        self.bindings = _Bindings(self.config_store)
        self.bus = _ReportingBus(self.bindings)
        service, _admin, _bus = _service(
            self.config_store, admin=_WriterReload(self.bindings), bus=self.bus, orchestrate=self._roll
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
        if self.roll_gate is not None:
            await self.roll_gate.wait()
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
        decision = kwargs["decision"]
        if not decision.rolls_over("serve-b"):
            # The decision left the sibling running: the roll restarts nobody.
            return RecycleReport()
        # Each row carries the keys its worker reported, as the real orchestrator's rows do.
        keys = list(decision.reported["serve-b"].keys) if "serve-b" in decision.reported else []
        if self.roll_stops is not None:
            partial = RecycleReport(
                rows=[
                    RecycleRow(
                        name="serve-b",
                        kind="serve",
                        generation_before=1,
                        status=TIMED_OUT,
                        detail=self.roll_stops,
                        keys=keys,
                    )
                ],
                stopped=RecycleStop(kind="serve", name="serve-b", detail=self.roll_stops),
            )
            raise RecycleError(self.roll_stops, partial)
        self.bindings.restarted("serve-b")
        return RecycleReport(
            rows=[RecycleRow(name="serve-b", kind="serve", generation_before=1, status=RECYCLED, keys=keys)],
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

    async def assert_a_second_operation_is_refused(self) -> None:
        with pytest.raises(OperationInProgressError, match="another marketplace operation is in progress"):
            async with locks.fleet_lock():
                pass
        with pytest.raises(OperationInProgressError, match="another marketplace operation is in progress"):
            await self.installer.uninstall(_relay_spec().ref)

    def assert_the_key_is_free(self) -> None:
        assert self.keys.holders(MARKETPLACE_LOCK_KEY) == {"exclusive": [], "shared": []}
        assert locks.operation_lock.locked() is False


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch) -> SessionAdvisoryKeys:
    advisory = SessionAdvisoryKeys()
    monkeypatch.setattr(locks, "pinned_connection", advisory.pinned_connection)
    monkeypatch.setattr(locks, "component_store_settings", lambda component: None)
    return advisory


class _Carrier(TaiMCPLifecycleMixin):
    """The bus member a bounded operation hands its roll to: this process's own app, carrying it on this loop.

    The process app's ``carry_roll`` and ``release_carried_roll`` are this carrier's.
    """

    def __init__(self, bus: FakeBus) -> None:
        super().__init__()
        self.preset_manager = cast("Any", type("_Presets", (), {"reconcile_bases": AsyncMock()})())
        self._bus = cast("Any", bus)
        self.bind_serving_loop()

    def _mcp_tools(self, config: Any, tools: Any, preset_names: frozenset[str]) -> None:  # pragma: no cover - unused
        return None

    async def carried(self) -> None:
        await asyncio.wait_for(asyncio.gather(*self._carried_roll_tasks), timeout=5)


@pytest.fixture
async def carrier(monkeypatch: pytest.MonkeyPatch, world: _World) -> _Carrier:
    carrier = _Carrier(world.bus)
    monkeypatch.setattr(instance.build_app(), "carry_roll", carrier.carry_roll)
    monkeypatch.setattr(instance.build_app(), "release_carried_roll", carrier.release_carried_roll)
    monkeypatch.setattr(carried_roll_module, "orchestrate_recycle", world._roll)
    return carrier


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


async def test_a_failed_install_whose_undo_moved_a_sibling_s_key_rolls_once_with_no_writer_exit(
    world: _World,
) -> None:
    world.store.record_error = RuntimeError("attribution write failed")
    # The sibling still runs on a value the undo does not put back.
    world.bindings.bound["serve-b"] = "EARLIER"

    with pytest.raises(RuntimeError, match="attribution write failed"):
        await world.install("NEW")

    # The forward change and the unwind's revert both landed; one roll covers them.
    assert world.config_store.env[_RECYCLE_KEY] == "OLD"
    assert world.probe.rolls == 1
    assert world.probe.writer_self_deferred == [False]
    # The restarted worker reads the unwound store: no row.
    assert world.probe.verified_rows == [[]]
    world.assert_every_roll_held_the_key_shared_and_refused_a_second_operation()
    world.assert_the_key_is_free()


async def test_a_failed_install_whose_undo_put_every_setting_back_rolls_no_recycle(world: _World) -> None:
    world.store.record_error = RuntimeError("attribution write failed")

    with pytest.raises(RuntimeError, match="attribution write failed"):
        await world.install("NEW")

    # Every worker confirmed the unwind's reload with no recycle-class key off its bound value.
    assert world.config_store.env[_RECYCLE_KEY] == "OLD"
    assert world.probe.rolls == 0
    world.assert_the_key_is_free()


async def test_a_bounded_install_hands_its_roll_off_and_the_carrier_holds_the_key_shared_until_the_roll_ends(
    world: _World, carrier: _Carrier, writer_exit_edge: WriterExitScope
) -> None:
    world.roll_gate = asyncio.Event()

    # The door's edge answers the operation and settles the exit hold its change took.
    async with bounded_run(60):
        receipt = await world.install("NEW")
        await writer_exit_edge.settle()

    # The operation answered with its roll handed to its member, which has not rolled yet.
    assert receipt["reload"].roll_deferred_to == "serve-writer"
    assert world.probe.rolls == 0
    # The operation's own hold is gone; the carrier's shared hold keeps a second operation out.
    assert world.keys.holders(MARKETPLACE_LOCK_KEY)["exclusive"] == []
    assert len(world.keys.holders(MARKETPLACE_LOCK_KEY)["shared"]) == 1
    await world.assert_a_second_operation_is_refused()

    world.roll_gate.set()
    await carrier.carried()

    assert world.probe.rolls == 1
    # The restarted worker verified the finished install while the carrier held the key shared.
    assert world.probe.verified_rows == [[(_relay_spec().ref, "1.0.0")]]
    world.assert_every_roll_held_the_key_shared_and_refused_a_second_operation()
    assert world.probe.writer_self_deferred == [True]
    # The converged roll owes the writer's own restart: the carrier departs last.
    assert world.bus.exit_requests == [WRITER_EXIT_REASON]
    world.assert_the_key_is_free()


async def test_a_bounded_install_whose_carrier_cannot_take_the_key_raises_its_roll_unrun(
    world: _World, carrier: _Carrier, monkeypatch: pytest.MonkeyPatch
) -> None:
    @asynccontextmanager
    async def refused() -> AsyncIterator[None]:
        raise ConnectionError("the store refused the session")
        yield

    monkeypatch.setitem(roll_holds._HOLDS, locks.MARKETPLACE_ROLL_HOLD, refused)

    async with bounded_run(60):
        with pytest.raises(UnrunRollError, match="did not complete"):
            await world.install("NEW")

    assert world.probe.rolls == 0
    assert carrier._carried_roll_tasks == set()
    world.assert_the_key_is_free()
