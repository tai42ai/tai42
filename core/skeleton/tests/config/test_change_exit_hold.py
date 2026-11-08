"""A worker's own change holds its exit from the change's start, and a recycle op deferred onto it is carried out.

The edge that admits a change takes the hold before anything of it runs
(:meth:`~tai42_skeleton.app.writer_exit.WriterExitScope.hold_exit`): here the scope the test
opens stands in for that edge. The config doors — the config service's write exclusion (every
config write: an env write, a profile apply, a manifest change, a backup import) and the fleet
soft restart — require the hold
(:func:`~tai42_skeleton.app.writer_exit.require_writer_exit_held`): a write reaching its door
under an open scope with no hold raises before anything is read or written, and with no scope
open it writes. A ``Recycle`` op from another worker's roll that reaches the worker anywhere
inside the change — here at its broadcast, after the persist and before the roll's decision —
answers ``exit_deferred`` and arms nothing, so the change's reply is never cut by the recycle.
Once the edge answered, the worker leaves exactly once: for its own change's exit when the change
owed it, else for the deferred recycle op, which is never dropped.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from tai42_contract.app import tai42_app

# ``bus_settings`` is imported for its registration side effect: TAI_BUS_* are recycle-class.
import tai42_skeleton.app.bus_settings  # noqa: F401
import tai42_skeleton.app.lifecycle.bus_subscription as bus_subscription_module
from tai42_skeleton.app import instance
from tai42_skeleton.app.bus import FleetResult, LocalApplyResult, WorkerBus, WorkerIdentity, WorkerKind
from tai42_skeleton.app.bus.ops import FleetOp
from tai42_skeleton.app.epoch import Epoch
from tai42_skeleton.app.lifecycle import TaiMCPLifecycleMixin
from tai42_skeleton.app.recycle import RecycleReport
from tai42_skeleton.app.writer_exit import WRITER_EXIT_REASON, writer_exit_scope
from tai42_skeleton.config.deployment_env import DeploymentOwnership

from .._fakes.bus import FakeBus
from .fake_pipeline import (
    FakeConfigStore,
    FakeReloadAdmin,
    _reset_settings_after,  # noqa: F401  — autouse fixture, active on import
    _service,
    _with_bus,
)

tai42_app.bind(instance.app)

_KEY = "TAI_BUS_HEARTBEAT_TTL"
_RECYCLE_OP_REASON = "a fleet recycle op"
_ADMITTED = "the admitted change"


class _Lifecycle(TaiMCPLifecycleMixin):
    """Concrete-enough lifecycle for the recycle op handler: no server, no config manager."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.preset_manager = cast("Any", type("_Presets", (), {"reconcile_bases": AsyncMock()})())

    def _mcp_tools(self, config: Any, tools: Any, preset_names: frozenset[str]) -> None:  # pragma: no cover - unused
        return None


class _RecycledMidChangeBus(FakeBus):
    """A member's bus a foreign ``Recycle`` op reaches while the member's own change broadcasts.

    The first publish delivers the op to this worker's recycle handler before it publishes, and
    records the handler's answer and what this worker had armed and marked at that moment.
    """

    def __init__(self, lifecycle: _Lifecycle) -> None:
        super().__init__(origin="serve-writer", remotes=["serve-b"])
        self._lifecycle = lifecycle
        lifecycle._bus = cast("Any", self)
        self.recycle_answers: list[dict[str, Any]] = []
        self.marked_mid_change: list[int] = []
        self.armed_mid_change: list[int] = []

    async def publish(
        self,
        op: FleetOp,
        targets: list[str] | None,
        local: LocalApplyResult | None,
        *,
        expected_at_start: dict[str, int] | None = None,
    ) -> FleetResult:
        if not self.recycle_answers:
            self.recycle_answers.append(await self._lifecycle._apply_recycle())
            self.marked_mid_change.append(self.recycling_marks)
            self.armed_mid_change.append(len(self.post_reply_armed))
        return await super().publish(op, targets, local, expected_at_start=expected_at_start)


async def _no_roll(_bus: Any, **_kwargs: Any) -> RecycleReport:
    return RecycleReport()


async def _write_env(bus: FakeBus, writer_keys: list[str], _monkeypatch: pytest.MonkeyPatch) -> None:
    admin = FakeReloadAdmin(result={"status": "ok", "env_keys": 1, "awaiting_respawn": writer_keys})
    service, _admin, _bus = _service(FakeConfigStore(env={_KEY: "5"}), admin=admin, bus=bus, orchestrate=_no_roll)
    await service.apply_env_change({_KEY: "7"})


async def _apply_profile(bus: FakeBus, writer_keys: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_skeleton.app.lifecycle import config_reload

    monkeypatch.setattr(config_reload, "awaiting_respawn", lambda: list(writer_keys))
    service, _admin, _bus = _service(FakeConfigStore(env={_KEY: "5"}), bus=bus, orchestrate=_no_roll)

    async def save_previous(_env: dict[str, str]) -> None:
        return None

    async def build_and_swap(_env: dict[str, str], *, ownership: DeploymentOwnership) -> Epoch:
        return Epoch(number=0)

    await service.apply_replace_env({_KEY: "7"}, save_previous=save_previous, build_and_swap=build_and_swap)


async def _soft_restart(bus: FakeBus, writer_keys: list[str], _monkeypatch: pytest.MonkeyPatch) -> None:
    admin = FakeReloadAdmin(result={"status": "ok", "env_keys": 1, "awaiting_respawn": writer_keys})
    service, _admin, _bus = _service(FakeConfigStore(env={_KEY: "5"}), admin=admin, bus=bus, orchestrate=_no_roll)
    await service.reload_fleet(None, change="the fleet soft restart")


_DOORS = {"write_env": _write_env, "apply_profile": _apply_profile, "soft_restart": _soft_restart}


@pytest.fixture(params=sorted(_DOORS))
def door(request: pytest.FixtureRequest) -> Any:
    return _DOORS[request.param]


@pytest.fixture
def lifecycle(monkeypatch: pytest.MonkeyPatch) -> _Lifecycle:
    _with_bus(monkeypatch)
    return _Lifecycle()


async def _carried_out(lifecycle: _Lifecycle) -> None:
    """Wait for the exit a deferred recycle op runs once the worker's changes are answered, if one runs."""
    task = lifecycle._deferred_recycle_task
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout=5)


async def _run_change(door: Any, lifecycle: _Lifecycle, monkeypatch: pytest.MonkeyPatch, writer_keys: list[str]) -> Any:
    bus = _RecycledMidChangeBus(lifecycle)
    # The reply's fan-out summary reads the process bus, so the door runs through this one.
    monkeypatch.setattr(instance.app, "_bus", bus)
    with writer_exit_scope() as exits:
        exits.hold_exit(cast("WorkerBus", bus), _ADMITTED)
        await door(bus, writer_keys, monkeypatch)
        # The edge has not answered yet: the worker still runs, its exit held by the change.
        assert bus.exit_requests == []
        assert bus.exit_deferred is not None
        await exits.settle()
    return bus


async def test_a_recycle_op_reaching_the_writer_mid_change_is_deferred_and_one_exit_follows_the_reply(
    door: Any, lifecycle: _Lifecycle, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = await _run_change(door, lifecycle, monkeypatch, writer_keys=[_KEY])

    (answer,) = bus.recycle_answers
    assert answer["exit_deferred"] is not None
    assert "recycling" in answer
    # Nothing was marked or armed while the change ran: its reply is never cut by the recycle.
    assert bus.marked_mid_change == [0]
    assert bus.armed_mid_change == [0]
    await _carried_out(lifecycle)
    # The change owed the writer's own restart: that one exit carries the recycle out too.
    assert bus.exit_requests == [WRITER_EXIT_REASON]
    assert bus.recycling_marks == 1
    assert bus.exit_deferred is None


async def test_a_recycle_op_deferred_onto_a_change_that_owes_no_exit_is_carried_out_after_the_reply(
    door: Any, lifecycle: _Lifecycle, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = await _run_change(door, lifecycle, monkeypatch, writer_keys=[])

    (answer,) = bus.recycle_answers
    assert answer["exit_deferred"] is not None
    assert bus.marked_mid_change == [0]
    assert bus.armed_mid_change == [0]
    await _carried_out(lifecycle)
    # The writer was clean, so its change armed no exit: the deferred recycle op is not dropped.
    assert bus.exit_requests == [_RECYCLE_OP_REASON]
    assert bus.recycling_marks == 1
    assert bus.exit_deferred is None


async def test_a_deferred_recycle_waits_for_every_change_still_holding_the_exit(
    lifecycle: _Lifecycle, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = _RecycledMidChangeBus(lifecycle)
    monkeypatch.setattr(instance.app, "_bus", bus)
    other = bus.defer_exit("another in-flight change")
    with writer_exit_scope() as exits:
        exits.hold_exit(cast("WorkerBus", bus), _ADMITTED)
        await _write_env(bus, [], monkeypatch)
        await exits.settle()

    await asyncio.sleep(0)
    # Another change still holds the exit: the recycle op waits for its answer too.
    assert bus.exit_requests == []
    bus.release_deferred_exit(other)
    await _carried_out(lifecycle)
    assert bus.exit_requests == [_RECYCLE_OP_REASON]


async def _write_env_whose_edge_fails(bus: FakeBus, monkeypatch: pytest.MonkeyPatch) -> None:
    """An env write in its scope whose edge fails after the change ran."""
    with writer_exit_scope() as exits:
        exits.hold_exit(cast("WorkerBus", bus), _ADMITTED)
        await _write_env(bus, [], monkeypatch)
        raise RuntimeError("the edge failed")


async def test_a_change_whose_edge_fails_still_carries_out_the_deferred_recycle(
    lifecycle: _Lifecycle, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = _RecycledMidChangeBus(lifecycle)
    monkeypatch.setattr(instance.app, "_bus", bus)
    with pytest.raises(RuntimeError, match="the edge failed"):
        await _write_env_whose_edge_fails(bus, monkeypatch)

    await _carried_out(lifecycle)
    assert bus.exit_requests == [_RECYCLE_OP_REASON]
    assert bus.exit_deferred is None


class _ForkChildBus(FakeBus):
    """A forked job child's bus, a non-member acting for ``backend-1``, recording its held exit at each publish."""

    def __init__(self) -> None:
        super().__init__(origin="backend-1/fork-7", remotes=["backend-1"])
        self._identity = WorkerIdentity(
            name="backend-1/fork-7", kind=WorkerKind.backend, pid=7, generation=0, member=False
        )
        self._member = WorkerIdentity(name="backend-1", kind=WorkerKind.backend, pid=1, generation=3)
        self.held_at_publish: list[str | None] = []

    async def publish(
        self,
        op: FleetOp,
        targets: list[str] | None,
        local: LocalApplyResult | None,
        *,
        expected_at_start: dict[str, int] | None = None,
    ) -> FleetResult:
        self.held_at_publish.append(self.exit_deferred)
        return await super().publish(op, targets, local, expected_at_start=expected_at_start)


async def test_a_forked_job_childs_change_holds_no_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    bus = _ForkChildBus()
    monkeypatch.setattr(instance.app, "_bus", bus)
    service, _admin, _bus = _service(FakeConfigStore(env={_KEY: "5"}), bus=bus, orchestrate=_no_roll)
    with writer_exit_scope() as exits:
        await service.apply_env_change({_KEY: "7"})
        await exits.settle()

    # A job child owns no exit of its own, so its change holds none.
    assert bus.held_at_publish == [None]
    assert bus.exit_deferred is None


async def test_the_join_holds_the_exit_from_its_announcement_and_carries_out_a_deferred_recycle(
    lifecycle: _Lifecycle, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = _RecycledMidChangeBus(lifecycle)
    monkeypatch.setattr(bus_subscription_module, "orchestrate_recycle", _no_roll)

    await lifecycle._join({"status": "ok", "awaiting_respawn": []})

    (answer,) = bus.recycle_answers
    assert answer["exit_deferred"] == "the deployment keys of serve-writer"
    assert bus.marked_mid_change == [0]
    assert bus.armed_mid_change == [0]
    await _carried_out(lifecycle)
    assert bus.exit_requests == [_RECYCLE_OP_REASON]
    assert bus.exit_deferred is None


def _writing_service(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, FakeConfigStore, FakeBus]:
    _with_bus(monkeypatch)
    bus = FakeBus(origin="serve-writer", remotes=["serve-b"])
    monkeypatch.setattr(instance.app, "_bus", bus)
    store = FakeConfigStore(env={_KEY: "5"})
    service, _admin, _bus = _service(store, bus=bus, orchestrate=_no_roll)
    return service, store, bus


async def test_a_config_write_reaching_its_door_with_no_hold_on_the_writer_exit_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store, bus = _writing_service(monkeypatch)

    with writer_exit_scope() as exits:
        with pytest.raises(RuntimeError) as raised:
            await service.apply_env_change({_KEY: "7"})
        assert not exits.holds_exit

    assert str(raised.value) == (
        "writer exit: this config change reached a config door with no hold on the writer's exit — "
        "its operation or route declares holds_writer_exit"
    )
    assert store.events == []
    assert store.env == {_KEY: "5"}
    assert bus.publish_calls == []
    assert bus.exit_deferred is None


async def test_a_config_write_under_a_scope_that_holds_the_exit_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    service, store, bus = _writing_service(monkeypatch)

    with writer_exit_scope() as exits:
        exits.hold_exit(cast("WorkerBus", bus), _ADMITTED)
        await service.apply_env_change({_KEY: "7"})
        assert bus.exit_deferred == _ADMITTED
        await exits.settle()

    assert store.env == {_KEY: "7"}
    assert bus.exit_deferred is None


async def test_a_config_write_with_no_scope_open_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    service, store, bus = _writing_service(monkeypatch)

    await service.apply_env_change({_KEY: "7"})

    assert store.env == {_KEY: "7"}
    assert bus.exit_deferred is None
