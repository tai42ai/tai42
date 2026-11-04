"""Unit oracles for :class:`~tai42_skeleton.config.service.ConfigService` — the post-persist
reload + broadcast tail and its failure discipline: a local reload that fails after the
persist landed still broadcasts and then re-raises with the fleet report; a raw broadcast
fault becomes a :class:`~tai42_skeleton.operations._broadcast.FleetBroadcastError` carrying
the honest bus-unreachable shape; an unconfirmed origin is a loud ERROR log but a returned
success; expected membership is pinned to op start, not publish time; the
backend-needs-bus and backend-needs-hooks-store invariants reject both directions; and an
env change that leaves the fleet apply window below the retire's drain floor is refused
before it persists.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from tai42_kit.settings import reset_all_settings
from tai42_kit.utils.run_bound import bounded_run

from tai42_skeleton.app import instance
from tai42_skeleton.app.boot_rules import (
    BackendNeedsBusError,
    BackendNeedsHooksStoreError,
    WorkersNeedBusError,
    WorkersNeedHooksStoreError,
    WorkersNeedSubMcpStoreError,
)
from tai42_skeleton.app.bus import FleetResult, OpOutcome, WorkerIdentity, WorkerKind, WorkerResult
from tai42_skeleton.app.discarded_build import DISCARDED_BUILD_STATUS
from tai42_skeleton.app.epoch import Epoch
from tai42_skeleton.app.recycle import RECYCLED, RecycleReport, RecycleRow
from tai42_skeleton.config.deployment_env import DeploymentOwnership
from tai42_skeleton.config.file_manager import FileConfigManager
from tai42_skeleton.config.recycle_policy import RecycleReason
from tai42_skeleton.operations._broadcast import FleetBroadcastError

from .._fakes.bus import FakeBus
from .._fakes.combo_check_app import CHECK_DOCUMENT, booted_check_app, clean_server, factory_calls
from .fake_pipeline import (
    FakeConfigStore,
    FakeReloadAdmin,
    RecordingBus,
    _no_bus,
    _orchestrate_spy,
    _reset_settings_after,  # noqa: F401  — autouse fixture, active on import
    _service,
    _with_bus,
    discarded_build_failure,
)

# The door's edge settles the writer's exit its change owes; these tests drive the door below it.
pytestmark = pytest.mark.usefixtures("writer_exit_edge")

# ---------------------------------------------------------------------------
# Failure discipline
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures(clean_server.__name__)
async def test_a_combo_on_a_base_not_bound_passes_the_gate_and_the_reload_refusal_still_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_bus(monkeypatch)
    async with booted_check_app():
        store = FakeConfigStore(manifest=dict(CHECK_DOCUMENT))
        admin = FakeReloadAdmin(raise_reload=RuntimeError("Tools 'fresh' not found in server."))
        service, _admin, _bus = _service(store, admin=admin, combo_check=instance.app.extensions)
        factory_calls().clear()
        combo = [{"name": "labelled", "config": {"label": "ok"}}]

        def attach(document: dict[str, Any]) -> None:
            document["tools"][0]["extensions"] = {"fresh": [combo]}

        with pytest.raises(FleetBroadcastError, match="'fresh' not found"):
            await service.apply_change(attach)

        assert factory_calls() == []
        assert store.manifest["tools"][0]["extensions"] == {"fresh": [combo]}
        assert admin.calls == 1


async def test_local_reload_failure_after_persist_still_broadcasts_then_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    admin = FakeReloadAdmin(raise_reload=RuntimeError("reload boom"))
    service, _admin, bus = _service(store, admin=admin)

    def add_server(document: dict[str, Any]) -> None:
        document["mcp"] = [{"title": "srv", "config": {"url": "http://x"}}]

    with pytest.raises(FleetBroadcastError) as exc:
        await service.apply_change(add_server)

    # The persist landed; the failed local reload does NOT strand the fleet.
    assert store.persisted == [{"mcp": [{"title": "srv", "config": {"url": "http://x"}}]}]
    assert len(bus.publish_calls) == 1
    _op, _targets, local = bus.publish_calls[0]
    assert local is not None
    assert local.outcome == OpOutcome.failed
    # The fleet report the broadcast produced rides the raised error.
    assert exc.value.report.op == "reload_config"


async def test_a_local_reload_failure_still_restarts_the_drifted_siblings_and_raises_with_the_roll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The writer's own reload failed after the persist: the siblings' reports still decide the
    # roll — serve-b reports a key and is restarted, serve-c confirmed clean and is left running —
    # and the writer, its state unknown, has its own restart follow the roll.
    _with_bus(monkeypatch)
    orch, seen = _orchestrate_spy(
        RecycleReport(rows=[RecycleRow(name="serve-b", kind="serve", generation_before=1, status=RECYCLED)])
    )
    bus = FakeBus(origin="serve-writer", remotes=["serve-b", "serve-c"])
    bus.remote_payloads.update(
        {"serve-b": {"status": "ok", "awaiting_respawn": ["BACKEND_MANIFEST_KEY"]}, "serve-c": {"awaiting_respawn": []}}
    )
    store = FakeConfigStore(manifest={"mcp": []})
    admin = FakeReloadAdmin(raise_reload=RuntimeError("reload boom"))
    service, _admin, _bus = _service(store, admin=admin, bus=bus, orchestrate=orch)

    def add_server(document: dict[str, Any]) -> None:
        document["mcp"] = [{"title": "srv", "config": {"url": "http://x"}}]

    with pytest.raises(FleetBroadcastError, match="reload boom") as exc:
        await service.apply_change(add_server)

    roll = exc.value.roll
    assert roll is not None
    assert roll.decision.reported.keys() == {"serve-b"}
    assert roll.decision.clean == {"serve-c"}
    assert roll.decision.writer_reason == RecycleReason(unknown="its reload reported failed")
    assert seen["applier_self_deferred"] is True
    assert roll.self_exit_armed is True
    assert roll.awaiting_respawn == []


async def test_a_local_reload_refused_at_its_epoch_build_raises_and_leaves_the_writer_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The writer's build was discarded before its swap and measured no drift after the discard: no
    # restart of its own follows; the siblings' reports still decide the roll, and the refusal still
    # raises.
    _with_bus(monkeypatch)
    orch, _seen = _orchestrate_spy(
        RecycleReport(rows=[RecycleRow(name="serve-b", kind="serve", generation_before=1, status=RECYCLED)])
    )
    bus = FakeBus(origin="serve-writer", remotes=["serve-b", "serve-c"])
    bus.remote_payloads.update(
        {"serve-b": {"status": "ok", "awaiting_respawn": ["BACKEND_MANIFEST_KEY"]}, "serve-c": {"awaiting_respawn": []}}
    )
    refusal = await discarded_build_failure(RuntimeError("chain pending: run tai db migrate"))
    store = FakeConfigStore(manifest={"mcp": []})
    service, _admin, _bus = _service(store, admin=FakeReloadAdmin(raise_reload=refusal), bus=bus, orchestrate=orch)

    def add_server(document: dict[str, Any]) -> None:
        document["mcp"] = [{"title": "srv", "config": {"url": "http://x"}}]

    with pytest.raises(FleetBroadcastError, match="chain pending") as exc:
        await service.apply_change(add_server)

    assert exc.value.__cause__ is refusal
    _op, _targets, local = bus.publish_calls[0]
    assert local is not None
    assert local.outcome == OpOutcome.failed
    assert local.payload == {"status": DISCARDED_BUILD_STATUS, "awaiting_respawn": []}
    roll = exc.value.roll
    assert roll is not None
    assert roll.decision.writer_reason is None
    assert roll.decision.reported.keys() == {"serve-b"}
    assert roll.decision.clean == {"serve-c"}
    assert roll.self_exit_armed is False


@pytest.mark.parametrize(
    "bus_fault",
    [
        pytest.param({"reachable": False, "error": "ConnectionError: bus down"}, id="a-returned-unreachable-report"),
        pytest.param({"publish_error": RuntimeError("ResponseError: WRONGTYPE")}, id="a-raised-broadcast"),
    ],
)
async def test_a_local_reload_refused_at_its_epoch_build_is_never_restarted_when_its_broadcast_misses_the_bus(
    monkeypatch: pytest.MonkeyPatch, bus_fault: dict[str, Any]
) -> None:
    # The report the broadcast left holds no row for the writer, yet the writer's own reload stated
    # its refused build: the roll that follows in the request (its orchestration a spy here) judges
    # it from that statement, clean, and arms no exit. The raised report stays the bus-unreachable
    # shape.
    _with_bus(monkeypatch)
    orch, seen = _orchestrate_spy(RecycleReport())
    refusal = await discarded_build_failure(RuntimeError("chain pending: run tai db migrate"))
    store = FakeConfigStore(manifest={"mcp": []})
    service, _admin, _bus = _service(
        store, admin=FakeReloadAdmin(raise_reload=refusal), bus=RecordingBus(**bus_fault), orchestrate=orch
    )

    def add_server(document: dict[str, Any]) -> None:
        document["mcp"] = [{"title": "srv", "config": {"url": "http://x"}}]

    with pytest.raises(FleetBroadcastError) as exc:
        await service.apply_change(add_server)

    assert exc.value.report.reachable is False
    assert exc.value.report.results == []
    roll = exc.value.roll
    assert roll is not None
    assert roll.decision.writer_reason is None
    assert seen["applier_self_deferred"] is False
    assert roll.self_exit_armed is False


async def test_broadcast_raise_after_persist_becomes_fleet_broadcast_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    # A non-transport broadcast fault the bus does NOT fold into a returned
    # bus-unreachable report (e.g. a redis ResponseError, or a malformed presence key
    # the census cannot parse) — it raises RAW out of publish AFTER the persist landed.
    bus = RecordingBus(publish_error=RuntimeError("ResponseError: WRONGTYPE"))
    service, admin, _bus = _service(store, bus=bus)

    def add_server(document: dict[str, Any]) -> None:
        document["mcp"] = [{"title": "srv", "config": {"url": "http://x"}}]

    with pytest.raises(FleetBroadcastError) as exc:
        await service.apply_change(add_server)

    # The raw broadcast error was wrapped as FleetBroadcastError, never propagated raw,
    # and rides as the cause.
    assert isinstance(exc.value.__cause__, RuntimeError)
    # The persist DID land — the committed mutation is in the store — and the local
    # reload ran before the broadcast raised.
    assert store.persisted == [{"mcp": [{"title": "srv", "config": {"url": "http://x"}}]}]
    assert admin.calls == 1
    # The error carries the honest bus-unreachable report (no origin list, only error).
    assert exc.value.report.op == "reload_config"
    assert exc.value.report.reachable is False
    assert exc.value.report.results == []
    assert "ResponseError" in (exc.value.report.error or "")


@pytest.mark.usefixtures(clean_server.__name__)
async def test_a_roll_that_raises_after_a_failed_broadcast_still_raises_the_broadcast_failure_naming_both(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The broadcast fault raises again at the roll's own ask of serve-b: the change landed, so the
    # raised error stays the broadcast failure, and its report names the roll's failure too.
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    bus = RecordingBus(remotes=["serve-b"], publish_error=RuntimeError("ResponseError: WRONGTYPE"))
    service, _admin, _bus = _service(store, bus=bus)

    def add_server(document: dict[str, Any]) -> None:
        document["mcp"] = [{"title": "srv", "config": {"url": "http://x"}}]

    with caplog.at_level(logging.ERROR), pytest.raises(FleetBroadcastError) as exc:
        await service.apply_change(add_server)

    assert store.persisted == [{"mcp": [{"title": "srv", "config": {"url": "http://x"}}]}]
    # The failed roll's outcome rides the error: no roll report, no exit armed, and the
    # unreachable report states no worker as awaiting a respawn.
    roll = exc.value.roll
    assert roll is not None
    assert (roll.recycle, roll.self_exit_armed, roll.awaiting_respawn) == (None, False, [])
    assert exc.value.report.error == (
        "RuntimeError: ResponseError: WRONGTYPE (the recycle roll the change owes also failed: "
        "RuntimeError: ResponseError: WRONGTYPE)"
    )
    assert "the recycle roll the change owes also failed" in caplog.text


@pytest.mark.usefixtures(clean_server.__name__)
async def test_a_roll_hand_off_that_fails_after_the_persist_raises_the_landed_change_as_a_fleet_broadcast_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Under a bound the change hands its roll to its member, whose app is not built here, so the
    # hand-off fails after the persist landed: the door raises the landed change's error, never a
    # bare one, and its report names the roll's failure.
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    bus = RecordingBus(remotes=["serve-b"], remote_payload={"awaiting_respawn": ["TAI_BUS_HEARTBEAT_TTL"]})
    service, _admin, _bus = _service(store, bus=bus)
    monkeypatch.setattr(instance, "_app", None)

    def add_server(document: dict[str, Any]) -> None:
        document["mcp"] = [{"title": "srv", "config": {"url": "http://x"}}]

    with caplog.at_level(logging.ERROR), bounded_run(), pytest.raises(FleetBroadcastError) as exc:
        await service.apply_change(add_server)

    assert store.persisted == [{"mcp": [{"title": "srv", "config": {"url": "http://x"}}]}]
    assert isinstance(exc.value.__cause__, RuntimeError)
    assert exc.value.roll is not None
    assert exc.value.report.reachable is True
    assert exc.value.report.error == (
        "the recycle roll the change owes failed: RuntimeError: fleet recycle: the roll for this config change "
        "could not be handed to 'serve-test', the bus member the change acts for — its app is not built. The roll did "
        "not start; the reload broadcast reached the bus"
    )
    assert "the recycle roll the change owes failed" in caplog.text


@pytest.mark.usefixtures(clean_server.__name__)
async def test_a_failed_roll_hand_off_states_the_workers_still_awaiting_a_respawn_on_every_reader_of_its_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The hand-off is refused, so the roll did not start: serve-b, which reported a moved key,
    # still awaits its respawn, and the error's roll says so — in the error reply's roll fields
    # and in the backup import's section report alike.
    from tai42_skeleton.backup.sections import _landed_change_report
    from tai42_skeleton.operations._broadcast import roll_outcome_body

    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    bus = RecordingBus(remotes=["serve-b"], remote_payload={"awaiting_respawn": ["TAI_BUS_HEARTBEAT_TTL"]})
    service, _admin, _bus = _service(store, bus=bus)
    app = instance._app
    monkeypatch.setattr(instance, "_app", None)

    def add_server(document: dict[str, Any]) -> None:
        document["mcp"] = [{"title": "srv", "config": {"url": "http://x"}}]

    with bounded_run(), pytest.raises(FleetBroadcastError) as exc:
        await service.apply_change(add_server)

    awaiting = [{"name": "serve-b", "keys": ["TAI_BUS_HEARTBEAT_TTL"]}]
    assert roll_outcome_body(exc.value.roll) == {
        "recycle": None,
        "awaiting_respawn": awaiting,
        "roll_deferred": False,
        "roll_deferred_to": None,
    }
    # The backup import reports the section from the same error.
    monkeypatch.setattr(instance, "_app", app)
    monkeypatch.setattr(instance.app, "_bus", bus)
    section = _landed_change_report("manifest", exc.value, updated=1)
    assert [worker.model_dump() for worker in section.awaiting_respawn] == awaiting
    assert section.recycle is None
    assert section.roll_deferred is False


class _ForkedChildBus(RecordingBus):
    """A forked job child's bus, acting for its member ``serve-test``; the carry-roll op gets ``carry_answer``."""

    def __init__(self, carry_answer: FleetResult, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.member_identity = self.identity
        self.identity = WorkerIdentity(
            name="serve-test/fork-9", kind=WorkerKind.backend, pid=9, generation=0, member=False
        )
        self._carry_answer = carry_answer

    async def publish(self, op: Any, targets: Any, local: Any, *, expected_at_start: Any = None) -> FleetResult:
        if op.op == "carry_roll":
            return self._carry_answer
        return await super().publish(op, targets, local, expected_at_start=expected_at_start)


@pytest.mark.parametrize(
    ("carry_answer", "why"),
    [
        (
            FleetResult(
                op="carry_roll",
                results=[
                    WorkerResult(
                        name="serve-test", outcome=OpOutcome.failed, error="RuntimeError: post-apply handler failed"
                    )
                ],
            ),
            "the op failed there: RuntimeError: post-apply handler failed",
        ),
        (
            FleetResult(op="carry_roll", reachable=False, error="ConnectionError: bus down"),
            "the bus is unreachable: ConnectionError: bus down",
        ),
        (
            FleetResult(op="carry_roll", results=[WorkerResult(name="serve-test", outcome=OpOutcome.timed_out)]),
            "no answer came: timed_out",
        ),
        (FleetResult(op="carry_roll"), "it did not answer"),
    ],
    ids=["failed", "unreachable", "timed-out", "no-row"],
)
@pytest.mark.usefixtures(clean_server.__name__)
async def test_a_roll_whose_hand_off_answer_is_unknown_is_stated_deferred_to_its_member(
    monkeypatch: pytest.MonkeyPatch, carry_answer: FleetResult, why: str
) -> None:
    # The roll may have gone out to serve-test, which may have started it: the reply does not judge it,
    # so the error's roll names serve-test as the member it is deferred to and lists no worker
    # awaiting a respawn — in the error reply's roll fields and in the backup import's section
    # report alike — while the error text and the report's error say whether serve-test started
    # it is unknown, never that it failed.
    from tai42_skeleton.backup.sections import _landed_change_report
    from tai42_skeleton.operations._broadcast import roll_outcome_body

    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    bus = _ForkedChildBus(
        carry_answer, remotes=["serve-b"], remote_payload={"awaiting_respawn": ["TAI_BUS_HEARTBEAT_TTL"]}
    )
    service, _admin, _bus = _service(store, bus=bus)

    def add_server(document: dict[str, Any]) -> None:
        document["mcp"] = [{"title": "srv", "config": {"url": "http://x"}}]

    with bounded_run(), pytest.raises(FleetBroadcastError) as exc:
        await service.apply_change(add_server)

    unknown = (
        "fleet recycle: the roll for this config change may have been sent to 'serve-test', the bus member the "
        f"change acts for, but its answer is not known — {why}. Whether 'serve-test' started the roll is unknown; the "
        "reload broadcast reached the bus. Read its log and the fleet census"
    )
    assert str(exc.value) == f"reload_config: change persisted but fleet propagation failed — {unknown}"
    assert exc.value.report.error == (
        f"the recycle roll the change owes may have been sent to its member, and its answer is not known: "
        f"RollHandOffUnknownError: {unknown}"
    )
    assert roll_outcome_body(exc.value.roll) == {
        "recycle": None,
        "awaiting_respawn": [],
        "roll_deferred": True,
        "roll_deferred_to": "serve-test",
    }
    assert exc.value.roll is not None
    assert exc.value.roll.self_exit_armed is False
    monkeypatch.setattr(instance.app, "_bus", bus)
    section = _landed_change_report("manifest", exc.value, updated=1)
    assert section.fanout is not None
    assert section.fanout.error == exc.value.report.error
    assert section.awaiting_respawn == []
    assert section.recycle is None
    assert section.roll_deferred is True
    assert section.roll_deferred_to == "serve-test"


@pytest.mark.usefixtures(clean_server.__name__)
async def test_a_failed_reload_whose_roll_answer_is_unknown_says_so_on_the_backup_section_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A bounded backup import restores the manifest section. The member's local reload fails on
    # a plugin that cannot load, and the roll that reload still owes goes to the member's own
    # app, whose answer names no carried roll. The section report is the door that carries the
    # report's error: its errors keep the reload's failure, its fan-out error says the roll may
    # have been sent and its answer is not known, next to the roll fields deferring it to the member.
    from tai42_skeleton.backup.sections import _import_manifest
    from tai42_skeleton.config.service import ConfigService
    from tai42_skeleton.marketplace.compat import CorePluginBootError

    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    bus = RecordingBus(remotes=["serve-b"], remote_payload={"awaiting_respawn": ["TAI_BUS_HEARTBEAT_TTL"]})
    admin = FakeReloadAdmin(raise_reload=CorePluginBootError("plugin 'acme-tools' cannot load"))
    service, _admin, _bus = _service(store, admin=admin, bus=bus)

    async def answerless_carry_roll(op: Any) -> object:
        return {"status": "ok"}

    def from_app(cls: type[ConfigService]) -> ConfigService:
        return service

    # One app serves both reads: the hand-off's (``built_app``) and the fan-out's (``instance.app``).
    app = instance.app
    monkeypatch.setattr(instance, "_app", app)
    monkeypatch.setattr(app, "_bus", bus)
    monkeypatch.setattr(app, "carry_roll", answerless_carry_roll)
    monkeypatch.setattr(ConfigService, "from_app", classmethod(from_app))

    with bounded_run():
        section = await _import_manifest({"mcp": [{"title": "srv", "config": {"url": "http://x"}}]})

    assert section.errors == [
        "reload_config: change persisted but fleet propagation failed — plugin 'acme-tools' cannot load"
    ]
    assert section.fanout is not None
    assert section.fanout.error == (
        "the recycle roll the change owes may have been sent to its member, and its answer is not known: "
        "RollHandOffUnknownError: fleet recycle: the roll for this config change may have been sent to "
        "'serve-test', the bus member the change acts for, but its answer is not known — its answer names no carried "
        "roll: {'status': 'ok'}. Whether 'serve-test' started the roll is unknown; the reload broadcast reached "
        "the bus. Read its log and the fleet census"
    )
    assert (section.awaiting_respawn, section.recycle) == ([], None)
    assert (section.roll_deferred, section.roll_deferred_to) == (True, "serve-test")


@pytest.mark.usefixtures(clean_server.__name__)
async def test_a_failed_broadcast_whose_roll_answer_is_unknown_names_both_on_its_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The broadcast fault raises, and the roll the unreachable report still owes goes out to
    # serve-test, which never answers: the report keeps the broadcast failure and adds that the
    # roll may have been sent with its answer not known, never that it failed.
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    bus = _ForkedChildBus(
        FleetResult(op="carry_roll"), remotes=["serve-b"], publish_error=RuntimeError("ResponseError: WRONGTYPE")
    )
    service, _admin, _bus = _service(store, bus=bus)

    def add_server(document: dict[str, Any]) -> None:
        document["mcp"] = [{"title": "srv", "config": {"url": "http://x"}}]

    with bounded_run(), pytest.raises(FleetBroadcastError) as exc:
        await service.apply_change(add_server)

    assert exc.value.report.error == (
        "RuntimeError: ResponseError: WRONGTYPE (the recycle roll the change owes may have been sent to its "
        "member, and its answer is not known: RollHandOffUnknownError: fleet recycle: the roll for this config "
        "change may have been sent to 'serve-test', the bus member the change acts for, but its answer is not "
        "known — it "
        "did not answer. "
        "Whether 'serve-test' started the roll is unknown; the reload broadcast did not reach the bus. Read its "
        "log and the fleet census)"
    )
    assert exc.value.roll is not None
    assert exc.value.roll.deferred_to == "serve-test"


async def test_apply_replace_broadcast_raise_after_persist_becomes_fleet_broadcast_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": [{"title": "old", "config": {"url": "http://old"}}]})
    bus = RecordingBus(publish_error=RuntimeError("ResponseError: WRONGTYPE"))
    service, admin, _bus = _service(store, bus=bus)

    document = {"mcp": [{"title": "new", "config": {"url": "http://new"}}]}
    with pytest.raises(FleetBroadcastError) as exc:
        await service.apply_replace(document)

    # apply_replace honors the same post-persist contract: the replace committed, then
    # the raw broadcast error surfaced as FleetBroadcastError with the unreachable report.
    assert store.manifest == document
    assert admin.calls == 1
    assert isinstance(exc.value.__cause__, RuntimeError)
    assert exc.value.report.reachable is False


# ---------------------------------------------------------------------------
# Expected membership is pinned to op start, not to publish time
#
# publish censuses when it is called — after the local reload. A worker whose presence
# fades across a reimporting reload would drop off that census and the report would read
# converged without it, so the pipeline snapshots membership BEFORE the reload.
# ---------------------------------------------------------------------------


async def test_expected_membership_is_censused_before_the_local_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    bus = RecordingBus(remotes=["serve-w1"])
    # The sibling is live when the op begins and its presence fades DURING the reload.
    admin = FakeReloadAdmin(during_reload=lambda: bus.live.discard("serve-w1"))
    service, _admin, _bus = _service(store, admin=admin, bus=bus)

    await service.apply_replace({"mcp": [{"title": "new", "config": {"url": "http://new"}}]})

    # Read first, so the sibling is still owed a confirmation; a snapshot taken after the
    # reload would be empty and the op would converge without it.
    assert bus.live == set()
    assert bus.expected_at_start_calls == [{"serve-w1": 1}]


async def test_op_start_census_failure_is_loud_but_never_aborts_the_reload(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The snapshot rides on top of publish's own census, which degrades a dead bus to an
    # honest unreachable report — so a census blip must not turn a survivable post-persist
    # reload into a raise. Loud, because the op then runs on the membership the snapshot
    # exists to correct.
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    bus = RecordingBus(remotes=["serve-w1"])
    service, admin, _bus = _service(store, bus=bus)
    resolve_ownership = service.deployment_ownership

    async def ownership_then_census_blip(op_name: str) -> Any:
        ownership = await resolve_ownership(op_name)
        bus.census_error = ConnectionError("bus census unreachable")
        return ownership

    monkeypatch.setattr(service, "deployment_ownership", ownership_then_census_blip)

    with caplog.at_level(logging.WARNING, logger="tai42_skeleton.operations._broadcast"):
        result = await service.apply_replace({"mcp": [{"title": "new", "config": {"url": "http://new"}}]})

    assert admin.calls == 1
    assert result.fleet.ok
    assert bus.expected_at_start_calls == [None]
    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and r.name == "tai42_skeleton.operations._broadcast"
    ]
    assert len(warnings) == 1
    assert warnings[0].exc_info is not None
    assert "op-start census" in warnings[0].getMessage()


async def test_broadcast_raise_with_local_reload_failure_is_single_fleet_broadcast_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    admin = FakeReloadAdmin(raise_reload=RuntimeError("reload boom"))
    bus = RecordingBus(publish_error=RuntimeError("ResponseError: WRONGTYPE"))
    service, _admin, _bus = _service(store, admin=admin, bus=bus)

    def add_server(document: dict[str, Any]) -> None:
        document["mcp"] = [{"title": "srv", "config": {"url": "http://x"}}]

    with pytest.raises(FleetBroadcastError) as exc:
        await service.apply_change(add_server)

    # Both the local reload AND the broadcast failed after the persist landed — a SINGLE
    # FleetBroadcastError surfaces, carrying the broadcast error as cause and an
    # unreachable report whose error notes the local reload failure too.
    assert store.persisted == [{"mcp": [{"title": "srv", "config": {"url": "http://x"}}]}]
    assert isinstance(exc.value.__cause__, RuntimeError)
    assert exc.value.report.reachable is False
    assert "ResponseError" in (exc.value.report.error or "")
    assert "local reload also failed" in (exc.value.report.error or "")


async def test_unconfirmed_origin_logs_error_but_returns_success(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    bus = RecordingBus(remotes=["serve-w1"], remote_outcome=OpOutcome.missing)
    orch, seen = _orchestrate_spy()
    service, _admin, _bus = _service(store, bus=bus, orchestrate=orch)

    with caplog.at_level(logging.ERROR, logger="tai42_skeleton.operations._broadcast"):
        result = await service.apply_replace({"mcp": []})

    # Persist + local reload landed, so the call SUCCEEDS; the unconfirmed origin is a
    # loud ERROR log and an explicit non-applied entry in the report.
    assert result.fleet.ok is False
    assert {r.name: r.outcome for r in result.fleet.results}["serve-w1"] == OpOutcome.missing
    assert any(record.levelno == logging.ERROR for record in caplog.records)
    assert any("did not fully converge" in record.message for record in caplog.records)
    # Its state is unknown, so the roll asks it once it is ready.
    assert result.decision is not None
    assert set(result.decision.unconfirmed) == {"serve-w1"}
    assert seen["decision"].clean == set()


async def test_bus_unreachable_returns_success_with_unreachable_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    bus = RecordingBus(remotes=["serve-w1"], reachable=False, error="ConnectionError: bus down")
    orch, seen = _orchestrate_spy()
    service, admin, _bus = _service(store, bus=bus, orchestrate=orch)

    result = await service.apply_replace({"mcp": []})

    # Persist + local reload landed, so the call SUCCEEDS even though the transport was
    # down: the honest bus-unreachable shape (no origin list, only an error) rides through.
    assert admin.calls == 1
    assert result.fleet.reachable is False
    assert result.fleet.error == "ConnectionError: bus down"
    assert result.fleet.results == []
    # No sibling stated its env: every pinned worker is asked once ready. The writer's own applied
    # reload stated no drift, so it is judged clean and keeps running.
    assert result.decision is not None
    assert set(result.decision.unconfirmed) == {"serve-w1"}
    assert result.decision.writer_reason is None
    assert seen["applier_self_deferred"] is False
    assert result.self_exit_armed is False


# ---------------------------------------------------------------------------
# backend-needs-bus invariant — both directions
# ---------------------------------------------------------------------------


def _with_hooks_store(monkeypatch: pytest.MonkeyPatch) -> None:
    # A registered backend also needs the shared hooks registry; the bus-rule tests
    # set it so only the bus decides their outcome.
    monkeypatch.setenv("HOOKS_REDIS_URL", "redis://localhost:6379/0")


async def test_manifest_change_adding_backend_without_bus_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_bus(monkeypatch)
    store = FakeConfigStore(manifest={})
    service, admin, bus = _service(store)

    def add_backend(document: dict[str, Any]) -> None:
        document["backend_module"] = "myapp.backend"

    with pytest.raises(BackendNeedsBusError, match="TAI_BUS_REDIS_URL"):
        await service.apply_change(add_backend)

    assert store.persisted == []
    assert admin.calls == 0
    assert bus.publish_calls == []


async def test_manifest_change_adding_backend_with_bus_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_hooks_store(monkeypatch)
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={})
    service, admin, _bus = _service(store)

    def add_backend(document: dict[str, Any]) -> None:
        document["backend_module"] = "myapp.backend"

    result = await service.apply_change(add_backend)

    assert store.manifest == {"backend_module": "myapp.backend"}
    assert admin.calls == 1
    assert result.document == {"backend_module": "myapp.backend"}


async def test_env_change_materializing_backend_without_bus_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_bus(monkeypatch)
    # The manifest's backend module is an !ENV marker; the env change supplies the
    # value that materializes it — with no bus, the invariant rejects it.
    monkeypatch.delenv("TAI_BACKEND", raising=False)
    store = FakeConfigStore(manifest={"backend_module": "!ENV ${TAI_BACKEND}"})
    service, _admin, bus = _service(store)

    with pytest.raises(BackendNeedsBusError, match="TAI_BUS_REDIS_URL"):
        await service.apply_env_change({"TAI_BACKEND": "myapp.backend"})

    assert store.env_writes == []
    assert bus.publish_calls == []


async def test_env_change_removing_bus_while_backend_present_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_bus(monkeypatch)
    # A static backend is registered and the bus is configured only through the stored
    # env; the change empties the bus var — after it, a backend would run with no bus.
    monkeypatch.delenv("TAI_BUS_REDIS_URL", raising=False)
    store = FakeConfigStore(
        manifest={"backend_module": "myapp.backend"},
        env={"TAI_BUS_REDIS_URL": "redis://localhost:6379/0"},
    )
    service, _admin, bus = _service(store)

    with pytest.raises(BackendNeedsBusError, match="TAI_BUS_REDIS_URL"):
        await service.apply_env_change({"TAI_BUS_REDIS_URL": ""})

    assert store.env_writes == []
    assert bus.publish_calls == []


async def test_env_change_keeping_bus_with_backend_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_hooks_store(monkeypatch)
    _no_bus(monkeypatch)
    monkeypatch.delenv("TAI_BUS_REDIS_URL", raising=False)
    store = FakeConfigStore(
        manifest={"backend_module": "myapp.backend"},
        env={"TAI_BUS_REDIS_URL": "redis://localhost:6379/0"},
    )
    service, admin, _bus = _service(store)

    result = await service.apply_env_change({"SOME_KEY": "v"})

    assert store.env_writes == [{"SOME_KEY": "v"}]
    assert admin.calls == 1
    assert result.document is None


async def test_env_change_with_backend_and_default_namespace_bus_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    # The bus configured ONLY through the shared TAI_DEFAULT_REDIS_URL (no
    # TAI_BUS_REDIS_URL) resolves as ENABLED through BusSettings, so an unrelated env
    # edit on a backend deployment is NOT falsely rejected (a raw TAI_BUS_REDIS_URL
    # read would have missed the default and rejected every edit).
    _no_bus(monkeypatch)
    monkeypatch.delenv("TAI_BUS_REDIS_URL", raising=False)
    monkeypatch.setenv("TAI_DEFAULT_REDIS_URL", "redis://localhost:6379/0")
    reset_all_settings()
    try:
        store = FakeConfigStore(manifest={"backend_module": "myapp.backend"}, env={})
        service, admin, _bus = _service(store)

        result = await service.apply_env_change({"SOME_KEY": "v"})

        assert store.env_writes == [{"SOME_KEY": "v"}]
        assert admin.calls == 1
        assert result.document is None
    finally:
        reset_all_settings()


# ---------------------------------------------------------------------------
# backend-needs-hooks-store invariant — both directions
# ---------------------------------------------------------------------------


def _no_hooks_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOOKS_REDIS_URL", raising=False)
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    reset_all_settings()


async def test_manifest_change_adding_backend_without_hooks_store_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    _no_hooks_store(monkeypatch)
    store = FakeConfigStore(manifest={})
    service, admin, bus = _service(store)

    def add_backend(document: dict[str, Any]) -> None:
        document["backend_module"] = "myapp.backend"

    with pytest.raises(BackendNeedsHooksStoreError, match="HOOKS_REDIS_URL"):
        await service.apply_change(add_backend)

    assert store.persisted == []
    assert admin.calls == 0
    assert bus.publish_calls == []


async def test_env_change_removing_hooks_store_while_backend_present_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A static backend is registered and the hooks registry is shared only through the
    # stored env; the change empties it — after it, the backend process would match its
    # events against an empty per-process registry.
    _with_bus(monkeypatch)
    _no_hooks_store(monkeypatch)
    store = FakeConfigStore(
        manifest={"backend_module": "myapp.backend"},
        env={"HOOKS_REDIS_URL": "redis://localhost:6379/0"},
    )
    service, _admin, bus = _service(store)

    with pytest.raises(BackendNeedsHooksStoreError, match="HOOKS_REDIS_URL"):
        await service.apply_env_change({"HOOKS_REDIS_URL": ""})

    assert store.env_writes == []
    assert bus.publish_calls == []


async def test_profile_replace_dropping_hooks_store_while_backend_present_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_bus(monkeypatch)
    _no_hooks_store(monkeypatch)
    store = FakeConfigStore(
        manifest={"backend_module": "myapp.backend"},
        env={"HOOKS_REDIS_URL": "redis://localhost:6379/0"},
    )
    service, _admin, bus = _service(store)

    saved: list[dict[str, str]] = []

    async def save_previous(env: dict[str, str]) -> None:
        saved.append(env)

    with pytest.raises(BackendNeedsHooksStoreError, match="HOOKS_REDIS_URL"):
        await service.apply_replace_env({}, save_previous=save_previous)

    # Refused before anything is snapshotted or written.
    assert saved == []
    assert store.env_writes == []
    assert bus.publish_calls == []


async def test_env_change_keeping_hooks_store_with_backend_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    _no_hooks_store(monkeypatch)
    store = FakeConfigStore(
        manifest={"backend_module": "myapp.backend"},
        env={"HOOKS_REDIS_URL": "redis://localhost:6379/0"},
    )
    service, admin, _bus = _service(store)

    result = await service.apply_env_change({"SOME_KEY": "v"})

    assert store.env_writes == [{"SOME_KEY": "v"}]
    assert admin.calls == 1
    assert result.document is None


async def test_env_and_manifest_change_removing_hooks_store_while_backend_present_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The combined env-write + manifest-mutate door judges the same post-change env: the
    # env half empties the stored hooks registry while the manifest keeps its backend.
    _with_bus(monkeypatch)
    _no_hooks_store(monkeypatch)
    store = FakeConfigStore(
        manifest={"backend_module": "myapp.backend"},
        env={"HOOKS_REDIS_URL": "redis://localhost:6379/0"},
    )
    service, admin, bus = _service(store)

    with pytest.raises(BackendNeedsHooksStoreError, match="HOOKS_REDIS_URL"):
        await service.apply_env_and_change(_prepare_env_and_mutator({"HOOKS_REDIS_URL": ""}))

    assert store.env_writes == []
    assert store.persisted == []
    assert admin.calls == 0
    assert bus.publish_calls == []


# ---------------------------------------------------------------------------
# workers-need-shared-stores invariant — an env change on a multi-worker server
#
# ``TAI_SERVE_WORKERS`` is the worker count ``tai serve`` stamps for its workers. Each
# store below is shared only through the stored env, and the change empties it, so every
# worker would come up on a per-process in-memory store after the change applies.
# ---------------------------------------------------------------------------

_STORE_RULES = [
    pytest.param("SUB_MCP_REDIS_URL", WorkersNeedSubMcpStoreError, id="sub-mcp"),
    pytest.param("HOOKS_REDIS_URL", WorkersNeedHooksStoreError, id="hooks"),
]


def _multi_worker_server_over_stored_stores(monkeypatch: pytest.MonkeyPatch) -> FakeConfigStore:
    _with_bus(monkeypatch)
    monkeypatch.setenv("TAI_SERVE_WORKERS", "2")
    for name in ("SUB_MCP_REDIS_URL", "HOOKS_REDIS_URL", "TAI_DEFAULT_REDIS_URL"):
        monkeypatch.delenv(name, raising=False)
    reset_all_settings()
    return FakeConfigStore(
        manifest={"mcp": []},
        env={"SUB_MCP_REDIS_URL": "redis://localhost:6379/1", "HOOKS_REDIS_URL": "redis://localhost:6379/2"},
    )


@pytest.mark.parametrize(("setting", "error"), _STORE_RULES)
async def test_env_change_removing_a_store_on_a_multi_worker_server_is_rejected(
    monkeypatch: pytest.MonkeyPatch, setting: str, error: type[Exception]
) -> None:
    store = _multi_worker_server_over_stored_stores(monkeypatch)
    service, admin, bus = _service(store)

    with pytest.raises(error, match=setting) as exc:
        await service.apply_env_change({setting: ""})

    assert "2 workers" in str(exc.value)
    assert store.env_writes == []
    assert admin.calls == 0
    assert bus.publish_calls == []


@pytest.mark.parametrize(("setting", "error"), _STORE_RULES)
async def test_env_and_manifest_change_removing_a_store_on_a_multi_worker_server_is_rejected(
    monkeypatch: pytest.MonkeyPatch, setting: str, error: type[Exception]
) -> None:
    store = _multi_worker_server_over_stored_stores(monkeypatch)
    service, admin, bus = _service(store)

    with pytest.raises(error, match=setting):
        await service.apply_env_and_change(_prepare_env_and_mutator({setting: ""}))

    assert store.env_writes == []
    assert store.persisted == []
    assert admin.calls == 0
    assert bus.publish_calls == []


@pytest.mark.parametrize(("setting", "error"), _STORE_RULES)
async def test_profile_replace_dropping_a_store_on_a_multi_worker_server_is_rejected(
    monkeypatch: pytest.MonkeyPatch, setting: str, error: type[Exception]
) -> None:
    store = _multi_worker_server_over_stored_stores(monkeypatch)
    service, _admin, bus = _service(store)
    kept = {key: value for key, value in store.env.items() if key != setting}

    saved: list[dict[str, str]] = []

    async def save_previous(env: dict[str, str]) -> None:
        saved.append(env)

    with pytest.raises(error, match=setting):
        await service.apply_replace_env(kept, save_previous=save_previous)

    assert saved == []
    assert store.env_writes == []
    assert bus.publish_calls == []


@pytest.mark.parametrize(("setting", "error"), _STORE_RULES)
async def test_manifest_change_on_a_multi_worker_server_over_an_in_memory_store_is_rejected(
    monkeypatch: pytest.MonkeyPatch, setting: str, error: type[Exception]
) -> None:
    # A manifest change leaves the env as it is, so it is judged against the live env.
    _multi_worker_server_over_stored_stores(monkeypatch)
    monkeypatch.setenv("SUB_MCP_REDIS_URL", "redis://localhost:6379/1")
    monkeypatch.setenv("HOOKS_REDIS_URL", "redis://localhost:6379/2")
    monkeypatch.delenv(setting)
    reset_all_settings()
    store = FakeConfigStore(manifest={"mcp": []})
    service, admin, bus = _service(store)

    def add_server(document: dict[str, Any]) -> None:
        document["mcp"] = [{"title": "srv", "config": {"url": "http://x"}}]

    with pytest.raises(error, match=setting):
        await service.apply_change(add_server)

    assert store.persisted == []
    assert admin.calls == 0
    assert bus.publish_calls == []


async def test_env_change_keeping_the_stores_on_a_multi_worker_server_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _multi_worker_server_over_stored_stores(monkeypatch)
    service, admin, _bus = _service(store)

    await service.apply_env_change({"SOME_KEY": "v"})

    assert store.env_writes == [{"SOME_KEY": "v"}]
    assert admin.calls == 1


async def test_env_change_removing_a_store_on_a_single_worker_server_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _multi_worker_server_over_stored_stores(monkeypatch)
    monkeypatch.setenv("TAI_SERVE_WORKERS", "1")
    service, admin, _bus = _service(store)

    await service.apply_env_change({"HOOKS_REDIS_URL": ""})

    assert store.env_writes == [{"HOOKS_REDIS_URL": ""}]
    assert admin.calls == 1


# ---------------------------------------------------------------------------
# workers-need-bus invariant — a change on a multi-worker server
#
# The bus is configured only through the stored env, and the change empties it, so every
# worker would come up on its own local bus after the change applies.
# ---------------------------------------------------------------------------

_WORKERS_NEED_BUS = r"2 workers with no worker bus.*TAI_BUS_REDIS_URL"


def _multi_worker_server_over_a_stored_bus(monkeypatch: pytest.MonkeyPatch) -> FakeConfigStore:
    _no_bus(monkeypatch)
    monkeypatch.delenv("TAI_BUS_REDIS_URL", raising=False)
    monkeypatch.setenv("TAI_SERVE_WORKERS", "2")
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    monkeypatch.setenv("SUB_MCP_REDIS_URL", "redis://localhost:6379/1")
    monkeypatch.setenv("HOOKS_REDIS_URL", "redis://localhost:6379/2")
    reset_all_settings()
    return FakeConfigStore(manifest={"mcp": []}, env={"TAI_BUS_REDIS_URL": "redis://localhost:6379/0"})


async def test_env_change_removing_the_bus_on_a_multi_worker_server_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _multi_worker_server_over_a_stored_bus(monkeypatch)
    service, admin, bus = _service(store)

    with pytest.raises(RuntimeError, match=_WORKERS_NEED_BUS):
        await service.apply_env_change({"TAI_BUS_REDIS_URL": ""})

    assert store.env_writes == []
    assert admin.calls == 0
    assert bus.publish_calls == []


async def test_env_and_manifest_change_removing_the_bus_on_a_multi_worker_server_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _multi_worker_server_over_a_stored_bus(monkeypatch)
    service, admin, bus = _service(store)

    with pytest.raises(RuntimeError, match=_WORKERS_NEED_BUS):
        await service.apply_env_and_change(_prepare_env_and_mutator({"TAI_BUS_REDIS_URL": ""}))

    assert store.env_writes == []
    assert store.persisted == []
    assert admin.calls == 0
    assert bus.publish_calls == []


async def test_profile_replace_dropping_the_bus_on_a_multi_worker_server_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _multi_worker_server_over_a_stored_bus(monkeypatch)
    service, _admin, bus = _service(store)

    saved: list[dict[str, str]] = []

    async def save_previous(env: dict[str, str]) -> None:
        saved.append(env)

    with pytest.raises(RuntimeError, match=_WORKERS_NEED_BUS):
        await service.apply_replace_env({}, save_previous=save_previous)

    assert saved == []
    assert store.env_writes == []
    assert bus.publish_calls == []


async def test_manifest_change_on_a_busless_multi_worker_server_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # A manifest change leaves the env as it is, so it is judged against the live env.
    _multi_worker_server_over_a_stored_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    service, admin, bus = _service(store)

    def add_server(document: dict[str, Any]) -> None:
        document["mcp"] = [{"title": "srv", "config": {"url": "http://x"}}]

    with pytest.raises(RuntimeError, match=_WORKERS_NEED_BUS):
        await service.apply_change(add_server)

    assert store.persisted == []
    assert admin.calls == 0
    assert bus.publish_calls == []


async def test_env_change_keeping_the_bus_on_a_multi_worker_server_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _multi_worker_server_over_a_stored_bus(monkeypatch)
    service, admin, _bus = _service(store)

    await service.apply_env_change({"SOME_KEY": "v"})

    assert store.env_writes == [{"SOME_KEY": "v"}]
    assert admin.calls == 1


# ---------------------------------------------------------------------------
# post-change invariants over a working-directory env file
#
# A change is judged against the env the processes run with after it: the post-change
# stored env plus every source that stays live, the process env and a working-directory
# ``.env`` that is not the stored env file. A server whose working directory is its file-mode
# config dir reads the stored env file as that ``.env``, so the file's current contents are
# what the change replaces; any other ``.env`` keeps being read after the change.
# ---------------------------------------------------------------------------

_STORES_AND_BUS = {
    "TAI_BUS_REDIS_URL": "redis://localhost:6379/0",
    "SUB_MCP_REDIS_URL": "redis://localhost:6379/1",
    "HOOKS_REDIS_URL": "redis://localhost:6379/2",
}

_BACKEND_MANIFEST = {"backend_module": "myapp.backend"}

_POST_CHANGE_RULES = [
    pytest.param("2", {"mcp": []}, "SUB_MCP_REDIS_URL", WorkersNeedSubMcpStoreError, id="workers-sub-mcp-store"),
    pytest.param("2", {"mcp": []}, "HOOKS_REDIS_URL", WorkersNeedHooksStoreError, id="workers-hooks-store"),
    pytest.param("2", {"mcp": []}, "TAI_BUS_REDIS_URL", WorkersNeedBusError, id="workers-bus"),
    pytest.param(None, _BACKEND_MANIFEST, "TAI_BUS_REDIS_URL", BackendNeedsBusError, id="backend-bus"),
    pytest.param(None, _BACKEND_MANIFEST, "HOOKS_REDIS_URL", BackendNeedsHooksStoreError, id="backend-hooks"),
]

_SERVER_SHAPES = [
    pytest.param("2", {"mcp": []}, id="multi-worker"),
    pytest.param(None, _BACKEND_MANIFEST, id="backend"),
]


def _write_env_file(path: Path, env: dict[str, str]) -> None:
    path.write_text("".join(f"{key}={value}\n" for key, value in env.items()))


def _file_store(config_dir: Path, *, env: dict[str, str], manifest: dict[str, Any]) -> FileConfigManager:
    config_dir.mkdir(exist_ok=True)
    store = FileConfigManager(str(config_dir))
    store.write_env(env)
    store.replace_manifest(manifest)
    return store


def _server_env(monkeypatch: pytest.MonkeyPatch, *, workers: str | None, applied: dict[str, str]) -> None:
    """The process env: ``applied`` is the stored env the boot bridge applied, nothing else of the stores."""
    for key in (*_STORES_AND_BUS, "TAI_DEFAULT_REDIS_URL", "TAI_MANIFEST_PATH", "TAI_CONFIG_DIR_PATH"):
        monkeypatch.delenv(key, raising=False)
    for key, value in applied.items():
        monkeypatch.setenv(key, value)
    if workers is None:
        monkeypatch.delenv("TAI_SERVE_WORKERS", raising=False)
    else:
        monkeypatch.setenv("TAI_SERVE_WORKERS", workers)
    reset_all_settings()


def _server_in_its_config_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, workers: str | None, manifest: dict[str, Any]
) -> FileConfigManager:
    """A file-mode server running in its config dir: the stored env is its ``.env`` and its process env."""
    store = _file_store(tmp_path, env=_STORES_AND_BUS, manifest=manifest)
    monkeypatch.chdir(tmp_path)
    _server_env(monkeypatch, workers=workers, applied=_STORES_AND_BUS)
    return store


def _server_beside_a_working_directory_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    store_kind: str,
    *,
    workers: str | None,
    manifest: dict[str, Any],
    stored: dict[str, str],
    working_dir_env: dict[str, str],
) -> FileConfigManager | FakeConfigStore:
    """A server whose working-directory ``.env`` is not its stored env file.

    ``external`` keeps the stored env in a provider store; ``file`` keeps it in a config dir
    other than the working directory.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_env_file(run_dir / ".env", working_dir_env)
    monkeypatch.chdir(run_dir)
    _server_env(monkeypatch, workers=workers, applied=stored)
    if store_kind == "file":
        return _file_store(tmp_path / "config", env=stored, manifest=manifest)
    return FakeConfigStore(manifest=manifest, env=dict(stored))


_STORE_KINDS = pytest.mark.parametrize("store_kind", ["external", "file"])


def _add_server(document: dict[str, Any]) -> None:
    document["mcp"] = [{"title": "srv", "config": {"url": "http://x"}}]


@pytest.mark.parametrize(("workers", "manifest", "setting", "error"), _POST_CHANGE_RULES)
async def test_env_change_is_judged_without_the_stored_env_file_it_replaces(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    workers: str | None,
    manifest: dict[str, Any],
    setting: str,
    error: type[Exception],
) -> None:
    store = _server_in_its_config_dir(monkeypatch, tmp_path, workers=workers, manifest=manifest)
    service, admin, bus = _service(store)

    with pytest.raises(error, match=setting):
        await service.apply_env_change({setting: ""})

    assert store.read_env() == _STORES_AND_BUS
    assert admin.calls == 0
    assert bus.publish_calls == []


@pytest.mark.parametrize(("workers", "manifest", "setting", "error"), _POST_CHANGE_RULES)
async def test_env_and_manifest_change_is_judged_without_the_stored_env_file_it_replaces(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    workers: str | None,
    manifest: dict[str, Any],
    setting: str,
    error: type[Exception],
) -> None:
    store = _server_in_its_config_dir(monkeypatch, tmp_path, workers=workers, manifest=manifest)
    service, admin, bus = _service(store)

    with pytest.raises(error, match=setting):
        await service.apply_env_and_change(_prepare_env_and_mutator({setting: ""}))

    assert store.read_env() == _STORES_AND_BUS
    assert store.read_manifest_preserved() == manifest
    assert admin.calls == 0
    assert bus.publish_calls == []


@pytest.mark.parametrize(("workers", "manifest", "setting", "error"), _POST_CHANGE_RULES)
async def test_profile_replace_is_judged_without_the_stored_env_file_it_replaces(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    workers: str | None,
    manifest: dict[str, Any],
    setting: str,
    error: type[Exception],
) -> None:
    store = _server_in_its_config_dir(monkeypatch, tmp_path, workers=workers, manifest=manifest)
    service, _admin, bus = _service(store)
    kept = {key: value for key, value in _STORES_AND_BUS.items() if key != setting}

    saved: list[dict[str, str]] = []

    async def save_previous(env: dict[str, str]) -> None:
        saved.append(env)

    with pytest.raises(error, match=setting):
        await service.apply_replace_env(kept, save_previous=save_previous)

    assert saved == []
    assert store.read_env() == _STORES_AND_BUS
    assert bus.publish_calls == []


async def test_env_change_keeping_the_stores_and_bus_in_the_config_dir_is_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = _server_in_its_config_dir(monkeypatch, tmp_path, workers="2", manifest=_BACKEND_MANIFEST)
    service, admin, _bus = _service(store)

    await service.apply_env_change({"SOME_KEY": "v"})

    assert store.read_env() == {**_STORES_AND_BUS, "SOME_KEY": "v"}
    assert admin.calls == 1


@_STORE_KINDS
@pytest.mark.parametrize(("workers", "manifest"), _SERVER_SHAPES)
async def test_env_change_over_a_live_working_directory_env_file_is_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store_kind: str, workers: str | None, manifest: dict[str, Any]
) -> None:
    store = _server_beside_a_working_directory_env_file(
        monkeypatch,
        tmp_path,
        store_kind,
        workers=workers,
        manifest=manifest,
        stored={},
        working_dir_env=_STORES_AND_BUS,
    )
    service, admin, _bus = _service(store)

    await service.apply_env_change({"SOME_KEY": "v"})

    assert store.read_env() == {"SOME_KEY": "v"}
    assert admin.calls == 1


@_STORE_KINDS
@pytest.mark.parametrize(("workers", "manifest"), _SERVER_SHAPES)
async def test_env_and_manifest_change_over_a_live_working_directory_env_file_is_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store_kind: str, workers: str | None, manifest: dict[str, Any]
) -> None:
    store = _server_beside_a_working_directory_env_file(
        monkeypatch,
        tmp_path,
        store_kind,
        workers=workers,
        manifest=manifest,
        stored={},
        working_dir_env=_STORES_AND_BUS,
    )
    service, admin, _bus = _service(store)

    await service.apply_env_and_change(_prepare_env_and_mutator({"SOME_KEY": "v"}))

    assert store.read_env() == {"SOME_KEY": "v"}
    assert admin.calls == 1


@_STORE_KINDS
@pytest.mark.parametrize(("workers", "manifest"), _SERVER_SHAPES)
async def test_profile_replace_over_a_live_working_directory_env_file_is_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store_kind: str, workers: str | None, manifest: dict[str, Any]
) -> None:
    store = _server_beside_a_working_directory_env_file(
        monkeypatch,
        tmp_path,
        store_kind,
        workers=workers,
        manifest=manifest,
        stored={},
        working_dir_env=_STORES_AND_BUS,
    )
    service, _admin, _bus = _service(store)
    swapped: list[dict[str, str]] = []

    async def save_previous(env: dict[str, str]) -> None:
        return None

    async def build_and_swap(env: dict[str, str], *, ownership: DeploymentOwnership) -> Epoch:
        swapped.append(env)
        return Epoch(number=0)

    await service.apply_replace_env({"SOME_KEY": "v"}, save_previous=save_previous, build_and_swap=build_and_swap)

    assert store.read_env() == {"SOME_KEY": "v"}
    assert len(swapped) == 1


@_STORE_KINDS
@pytest.mark.parametrize(("workers", "manifest"), _SERVER_SHAPES)
async def test_manifest_change_over_a_live_working_directory_env_file_is_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store_kind: str, workers: str | None, manifest: dict[str, Any]
) -> None:
    store = _server_beside_a_working_directory_env_file(
        monkeypatch,
        tmp_path,
        store_kind,
        workers=workers,
        manifest=manifest,
        stored={},
        working_dir_env=_STORES_AND_BUS,
    )
    service, admin, _bus = _service(store)

    await service.apply_change(_add_server)

    assert store.read_manifest_preserved()["mcp"] == [{"title": "srv", "config": {"url": "http://x"}}]
    assert admin.calls == 1


_ONLY_STORED = {"SUB_MCP_REDIS_URL": _STORES_AND_BUS["SUB_MCP_REDIS_URL"]}
_WORKING_DIR_KEEPS = {key: _STORES_AND_BUS[key] for key in ("TAI_BUS_REDIS_URL", "HOOKS_REDIS_URL")}


@_STORE_KINDS
async def test_env_change_dropping_a_store_only_the_stored_env_carries_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store_kind: str
) -> None:
    store = _server_beside_a_working_directory_env_file(
        monkeypatch,
        tmp_path,
        store_kind,
        workers="2",
        manifest={"mcp": []},
        stored=_ONLY_STORED,
        working_dir_env=_WORKING_DIR_KEEPS,
    )
    service, admin, _bus = _service(store)

    with pytest.raises(WorkersNeedSubMcpStoreError, match="SUB_MCP_REDIS_URL"):
        await service.apply_env_change({"SUB_MCP_REDIS_URL": ""})

    assert store.read_env() == _ONLY_STORED
    assert admin.calls == 0


@_STORE_KINDS
async def test_env_and_manifest_change_dropping_a_store_only_the_stored_env_carries_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store_kind: str
) -> None:
    store = _server_beside_a_working_directory_env_file(
        monkeypatch,
        tmp_path,
        store_kind,
        workers="2",
        manifest={"mcp": []},
        stored=_ONLY_STORED,
        working_dir_env=_WORKING_DIR_KEEPS,
    )
    service, admin, _bus = _service(store)

    with pytest.raises(WorkersNeedSubMcpStoreError, match="SUB_MCP_REDIS_URL"):
        await service.apply_env_and_change(_prepare_env_and_mutator({"SUB_MCP_REDIS_URL": ""}))

    assert store.read_env() == _ONLY_STORED
    assert admin.calls == 0


@_STORE_KINDS
async def test_profile_replace_dropping_a_store_only_the_stored_env_carries_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store_kind: str
) -> None:
    store = _server_beside_a_working_directory_env_file(
        monkeypatch,
        tmp_path,
        store_kind,
        workers="2",
        manifest={"mcp": []},
        stored=_ONLY_STORED,
        working_dir_env=_WORKING_DIR_KEEPS,
    )
    service, admin, _bus = _service(store)

    async def save_previous(env: dict[str, str]) -> None:
        return None

    with pytest.raises(WorkersNeedSubMcpStoreError, match="SUB_MCP_REDIS_URL"):
        await service.apply_replace_env({}, save_previous=save_previous)

    assert store.read_env() == _ONLY_STORED
    assert admin.calls == 0


@_STORE_KINDS
async def test_env_change_dropping_a_store_the_working_directory_env_file_keeps_is_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store_kind: str
) -> None:
    # The working-directory file keeps the hooks registry shared after the stored copy goes.
    stored = {"HOOKS_REDIS_URL": _STORES_AND_BUS["HOOKS_REDIS_URL"]}
    store = _server_beside_a_working_directory_env_file(
        monkeypatch,
        tmp_path,
        store_kind,
        workers="2",
        manifest={"mcp": []},
        stored=stored,
        working_dir_env=_STORES_AND_BUS,
    )
    service, admin, _bus = _service(store)

    await service.apply_env_change({"HOOKS_REDIS_URL": ""})

    assert not store.read_env().get("HOOKS_REDIS_URL")
    assert admin.calls == 1


# ---------------------------------------------------------------------------
# apply-window invariant — the window covers three drain budgets after the change
# ---------------------------------------------------------------------------


async def test_env_change_raising_the_drain_past_the_apply_window_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_skeleton.app.boot_rules import ApplyWindowBelowDrainError

    _with_bus(monkeypatch)
    monkeypatch.setenv("TAI_BUS_APPLY_TIMEOUT", "90")
    monkeypatch.delenv("TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS", raising=False)
    reset_all_settings()
    store = FakeConfigStore(manifest={"mcp": []}, env={})
    service, admin, bus = _service(store)

    with pytest.raises(ApplyWindowBelowDrainError) as exc:
        await service.apply_env_change({"TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS": "90"})

    message = str(exc.value)
    assert "TAI_BUS_APPLY_TIMEOUT=90" in message
    assert "TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS=90" in message
    assert "270" in message
    assert store.env_writes == []
    assert admin.calls == 0
    assert bus.publish_calls == []


async def test_env_change_raising_the_drain_within_a_wide_apply_window_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_bus(monkeypatch)
    monkeypatch.setenv("TAI_BUS_APPLY_TIMEOUT", "270")
    reset_all_settings()
    store = FakeConfigStore(manifest={"mcp": []}, env={})
    service, admin, _bus = _service(store)

    await service.apply_env_change({"TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS": "90"})

    assert store.env_writes == [{"TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS": "90"}]
    assert admin.calls == 1


async def test_env_change_without_a_bus_never_applies_the_window_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_bus(monkeypatch)
    monkeypatch.setenv("TAI_BUS_APPLY_TIMEOUT", "5")
    reset_all_settings()
    store = FakeConfigStore(manifest={"mcp": []}, env={})
    service, admin, _bus = _service(store)

    await service.apply_env_change({"TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS": "90"})

    assert store.env_writes == [{"TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS": "90"}]
    assert admin.calls == 1


def _prepare_env_and_mutator(
    changes: dict[str, str],
) -> Callable[[dict[str, str]], Awaitable[tuple[dict[str, str], Callable[[dict[str, Any]], None]]]]:
    def add_server(document: dict[str, Any]) -> None:
        document["mcp"] = [{"title": "srv", "config": {"url": "http://x"}}]

    async def prepare(_stored: dict[str, str]) -> tuple[dict[str, str], Callable[[dict[str, Any]], None]]:
        return changes, add_server

    return prepare


async def test_env_and_manifest_change_raising_the_drain_past_the_apply_window_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tai42_skeleton.app.boot_rules import ApplyWindowBelowDrainError

    _with_bus(monkeypatch)
    monkeypatch.setenv("TAI_BUS_APPLY_TIMEOUT", "90")
    reset_all_settings()
    store = FakeConfigStore(manifest={"mcp": []}, env={})
    service, admin, bus = _service(store)

    with pytest.raises(ApplyWindowBelowDrainError, match="270"):
        await service.apply_env_and_change(_prepare_env_and_mutator({"TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS": "90"}))

    assert store.env_writes == []
    assert store.persisted == []
    assert admin.calls == 0
    assert bus.publish_calls == []
