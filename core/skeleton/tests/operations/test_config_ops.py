"""Op-level oracles for the config operations.

``reload_config`` is a convergence op: it applies locally when this worker is a
target, then broadcasts, embedding the per-origin fleet report, and restarts the
workers whose reload built and reports a recycle-class key off its bound value (one
whose reload was refused at its epoch build is left running, its restart withheld); a failed local
reload publishes anyway and re-raises with the report. The projection carries
``destructiveHint``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.manifest import ApiToolsConfig

from tai42_skeleton.app import instance
from tai42_skeleton.app.bus import OpOutcome
from tai42_skeleton.app.writer_exit import WRITER_EXIT_REASON, open_writer_exit_scope, writer_exit_scope
from tai42_skeleton.config import recycle_roll
from tai42_skeleton.operations import BadRequestError, OperationRegistry, operation_metadata_of
from tai42_skeleton.operations import config as config_ops
from tai42_skeleton.operations import settings_profiles as profile_ops
from tai42_skeleton.operations._broadcast import FleetBroadcastError
from tai42_skeleton.operations.projection import project_operations

from .._fakes.bus import FakeBus
from .._fakes.mcp_listing import no_mcp_listing
from .._fakes.write_span import ExclusiveWriteStore
from .._writer_exit_edge import admit_change
from ..config.fake_pipeline import ComboChecks


class _Admin:
    def __init__(self, result: object, *, raise_reload: Exception | None = None) -> None:
        self._result = result
        self._raise_reload = raise_reload
        self.calls = 0

    async def reload_config(self) -> object:
        self.calls += 1
        if self._raise_reload is not None:
            raise self._raise_reload
        return self._result

    served_mcp_names = staticmethod(no_mcp_listing)


def _install(
    monkeypatch: pytest.MonkeyPatch, *, admin: _Admin, manager: object = None, bus: FakeBus | None = None
) -> FakeBus:
    impl = SimpleNamespace(
        config=SimpleNamespace(config_manager=manager),
        admin=admin,
        backends=SimpleNamespace(backend=None),
    )
    monkeypatch.setattr(tai42_app, "_impl", impl)
    bus = bus or FakeBus()
    monkeypatch.setattr(instance.app, "_bus", bus)
    return bus


# -- reload_config (convergence op, class c) ---------------


async def test_reload_config_untargeted_applies_and_broadcasts(monkeypatch: pytest.MonkeyPatch) -> None:
    admin = _Admin({"status": "ok", "env_keys": 3, "awaiting_respawn": []})
    bus = _install(monkeypatch, admin=admin)

    result = await config_ops.reload_config()

    assert admin.calls == 1
    assert bus.publish_calls[0][0] == {"op": "reload_config"}
    assert bus.publish_calls[0][1] is None
    assert result["results"][0]["payload"] == {"status": "ok", "env_keys": 3, "awaiting_respawn": []}
    assert result["recycle"] is None
    assert result["awaiting_respawn"] == []


async def test_reload_config_targeted_to_remote_skips_local(monkeypatch: pytest.MonkeyPatch) -> None:
    admin = _Admin({"status": "ok"})
    bus = _install(monkeypatch, admin=admin, bus=FakeBus(remotes=["serve-w1"]))

    result = await config_ops.reload_config(["serve-w1"])

    assert admin.calls == 0  # self not targeted → no local reload
    assert bus.publish_calls == [({"op": "reload_config"}, ["serve-w1"], None)]
    assert {r["name"] for r in result["results"]} == {"serve-w1"}


async def test_reload_config_local_failure_still_broadcasts_then_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # Convergence: a failed local reload must NOT abort the broadcast — the door
    # exists to heal stale siblings — so it publishes anyway (self entry = failed)
    # and re-raises with the fleet report attached.
    admin = _Admin({"status": "ok"}, raise_reload=RuntimeError("local reload boom"))
    bus = _install(monkeypatch, admin=admin)

    with writer_exit_scope() as exits:
        admit_change(bus)
        with pytest.raises(FleetBroadcastError) as exc:
            await config_ops.reload_config()
        await exits.settle()

    assert len(bus.publish_calls) == 1
    self_result = bus.publish_calls[0][2]
    assert self_result is not None
    assert self_result.outcome == OpOutcome.failed
    assert exc.value.report.op == "reload_config"


async def test_reload_config_local_failure_raises_with_the_roll_that_restarts_the_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The writer's failed reload leaves its state unknown: the roll runs (no sibling to restart,
    # so it converges at once) and arms the writer's own restart, and the raised error carries it.
    monkeypatch.setattr(recycle_roll, "recycle_step_timeout", lambda: 1.0)
    admin = _Admin({"status": "ok"}, raise_reload=RuntimeError("local reload boom"))
    bus = _install(monkeypatch, admin=admin)

    with writer_exit_scope() as exits:
        admit_change(bus)
        with pytest.raises(FleetBroadcastError) as exc:
            await config_ops.reload_config()
        await exits.settle()

    roll = exc.value.roll
    assert roll is not None
    assert roll.decision.writer_needs
    assert roll.recycle is not None
    assert roll.self_exit_armed is True
    # The error's edge settles the exit the roll owed: the writer restarts all the same.
    assert bus.exit_requests == [WRITER_EXIT_REASON]


# -- write_env ----------------------------------------------------------------


async def test_write_env_maps_manager_value_error_to_400(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Manager(ExclusiveWriteStore):
        def read_env(self) -> dict:
            return {}

        def read_manifest_preserved(self) -> dict:
            return {}

        def write_env(self, config: dict) -> None:
            raise ValueError("invalid env key 'BAD KEY'")

    _install(monkeypatch, admin=_Admin({"status": "ok"}), manager=_Manager())

    with pytest.raises(BadRequestError, match="invalid env key"):
        await config_ops.write_env({"BAD KEY": "v"})


async def test_write_env_local_only_fanout(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Manager(ExclusiveWriteStore):
        def __init__(self) -> None:
            super().__init__()
            self.written: list[dict] = []
            self.env: dict[str, str] = {}

        def read_env(self) -> dict:
            return dict(self.env)

        def read_manifest_preserved(self) -> dict:
            # No backend registered, so the env-change invariant has nothing to reject.
            return {}

        def write_env(self, config: dict) -> None:
            self.written.append(config)
            self.env = {**self.env, **config}

    manager = _Manager()
    admin = _Admin({"status": "ok", "env_keys": 1, "awaiting_respawn": []})
    _install(monkeypatch, admin=admin, manager=manager)

    result = await config_ops.write_env({"NEW": "v"})

    # The env was validated (effective config resolved), persisted, reloaded locally,
    # and the reload broadcast; a lone worker collapses the fan-out to the local note. A
    # hot-only change rolls no recycle.
    assert manager.written == [{"NEW": "v"}]
    assert result == {
        "status": "ok",
        "env_keys": 1,
        "fanout": {"mode": "local-only", "note": "no worker bus configured; only this worker reloaded"},
        "recycle": None,
        "awaiting_respawn": [],
        "roll_deferred": False,
        "roll_deferred_to": None,
        "span_release_error": None,
    }


async def test_write_env_removing_bus_with_backend_maps_to_400(monkeypatch: pytest.MonkeyPatch) -> None:
    # A registered backend needs the bus; an env change that empties TAI_BUS_REDIS_URL
    # leaves it with none, so ConfigService raises the RuntimeError
    # ``BackendNeedsBusError`` at MUTATE time. The op must map it to a loud 400 naming
    # TAI_BUS_REDIS_URL, not let it escape as an unhandled 500.
    monkeypatch.delenv("TAI_BUS_REDIS_URL", raising=False)

    class _Manager(ExclusiveWriteStore):
        def __init__(self) -> None:
            super().__init__()
            self.written: list[dict] = []

        def read_env(self) -> dict:
            # The bus is configured only through the stored env; the change empties it.
            return {"TAI_BUS_REDIS_URL": "redis://localhost:6379/0"}

        def read_manifest_preserved(self) -> dict:
            return {"backend_module": "myapp.backend"}

        def write_env(self, config: dict) -> None:
            self.written.append(config)

    manager = _Manager()
    _install(monkeypatch, admin=_Admin({"status": "ok"}), manager=manager)

    with pytest.raises(BadRequestError, match="TAI_BUS_REDIS_URL"):
        await config_ops.write_env({"TAI_BUS_REDIS_URL": ""})

    assert manager.written == []  # rejected in validation, before any write


# -- projection ---------------------------------------------------------------


def test_reload_config_projects_with_destructive_hint() -> None:
    reg = OperationRegistry()
    reg.register(operation_metadata_of(config_ops.reload_config))

    class _Rec:
        def __init__(self) -> None:
            self.registered: dict[str, dict] = {}

        def tool(self, *, force, name, tags, annotations):
            self.registered[name] = {"annotations": annotations}
            return lambda fn: fn

    app = SimpleNamespace(tools=_Rec())
    names = project_operations(app, ApiToolsConfig(expose_destructive=True), registry=reg)
    assert "reload_config" in names  # default-in (not authority-changing), destructive
    assert app.tools.registered["reload_config"]["annotations"].destructiveHint is True


# -- the writer's own recycle at the env doors -------------------------------


def _writer_moved_decision(writer_name: str, generation: int) -> Any:
    """The decision of a reload whose writer and ``backend-1`` both report BACKEND_MANIFEST_KEY moved."""
    from tai42_skeleton.app.bus import WorkerIdentity, WorkerKind
    from tai42_skeleton.config.recycle_policy import RecycleDecision, RecycleReason, ReportedKeys

    return RecycleDecision(
        writer=WorkerIdentity(name=writer_name, kind=WorkerKind.serve, pid=1, generation=generation),
        reported={"backend-1": ReportedKeys(generation=1, keys=("BACKEND_MANIFEST_KEY",))},
        unconfirmed={},
        clean=frozenset(),
        writer_reason=RecycleReason(keys=("BACKEND_MANIFEST_KEY",)),
        withheld={},
        addressed=None,
        report_unreachable=False,
    )


def _env_result(*, armed: bool) -> object:
    from tai42_skeleton.app.bus import FleetResult
    from tai42_skeleton.app.recycle import RECYCLED, TIMED_OUT, RecycleReport, RecycleRow, RecycleStop
    from tai42_skeleton.config.service import ApplyResult

    if armed:
        report = RecycleReport(
            rows=[RecycleRow(name="backend-1", kind="backend", generation_before=1, status=RECYCLED)]
        )
    else:
        report = RecycleReport(
            rows=[RecycleRow(name="backend-1", kind="backend", generation_before=1, status=TIMED_OUT, detail="slow")],
            stopped=RecycleStop(kind="backend", name="backend-1", detail="slow"),
        )
    return ApplyResult(
        fleet=FleetResult(op="reload_config", results=[]),
        local={"status": "ok", "env_keys": 1, "awaiting_respawn": []},
        decision=_writer_moved_decision("serve-writer", 4),
        recycle=report,
        self_exit_armed=armed,
    )


def _owe_writer_exit(result: Any) -> Any:
    """Stand in for the pipeline's roll: an armed roll owes the writer's exit into the open scope."""
    if result.self_exit_armed:
        bus = instance.app.bus
        scope = open_writer_exit_scope("this env change")
        scope.hold_exit(bus, "this env change")
        scope.owe(bus)
    return result


def _stub_env_service(monkeypatch: pytest.MonkeyPatch, result: object) -> None:
    class _Svc:
        async def apply_env_change(self, env, secret_keys=None):  # type: ignore[no-untyped-def]
            return _owe_writer_exit(result)

        async def apply_env_and_change(self, prepare, *, manifest_pointer=None):  # type: ignore[no-untyped-def]
            return _owe_writer_exit(result)

    monkeypatch.setattr(config_ops.ConfigService, "from_app", classmethod(lambda cls: _Svc()))


async def test_write_env_renders_the_writer_self_deferred_line_and_runs_its_exit_after_the_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = _install(monkeypatch, admin=_Admin({}))
    _stub_env_service(monkeypatch, _env_result(armed=True))

    with writer_exit_scope() as exits:
        response = await config_ops.write_env({"BACKEND_MANIFEST_KEY": "NEW"})
        assert bus.exit_requests == []
        await exits.settle()

    assert bus.exit_requests == [WRITER_EXIT_REASON]
    roll = response["recycle"]
    assert [entry["status"] for entry in roll["recycle"]] == ["recycled", "self-deferred"]
    assert roll["recycle"][-1] == {
        "name": "serve-writer",
        "kind": "serve",
        "status": "self-deferred",
        "generation_before": 4,
        "keys": ["BACKEND_MANIFEST_KEY"],
        "detail": None,
    }
    assert roll["recycle_stopped"] is None
    assert "NEW" not in str(response)


async def test_write_env_renders_the_stop_when_its_recycle_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = _install(monkeypatch, admin=_Admin({}))
    _stub_env_service(monkeypatch, _env_result(armed=False))

    with writer_exit_scope() as exits:
        response = await config_ops.write_env({"BACKEND_MANIFEST_KEY": "NEW"})
        await exits.settle()

    assert bus.exit_requests == []
    assert response["recycle"]["recycle_stopped"] == {"kind": "backend", "name": "backend-1", "detail": "slow"}


async def test_set_mcp_secret_env_runs_the_writer_exit_its_converged_recycle_owes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tai42_skeleton.operations.manifest import secret_env

    bus = _install(monkeypatch, admin=_Admin({}))
    _stub_env_service(monkeypatch, _env_result(armed=True))

    with writer_exit_scope() as exits:
        response = await secret_env.set_mcp_secret_env("s3cr3t", "mcp/0/config/headers/Authorization", key_hint="gh")
        await exits.settle()

    assert bus.exit_requests == [WRITER_EXIT_REASON]
    assert [entry["status"] for entry in response["recycle"]["recycle"]] == ["recycled", "self-deferred"]


# -- the reload doors restart the workers their reload reports as drifted ---------


def _moved_fleet() -> FakeBus:
    """A writer and one sibling, the sibling's reload reporting BACKEND_MANIFEST_KEY off its bound value."""
    bus = FakeBus(remotes=["serve-b"])
    bus.remote_payloads["serve-b"] = {"status": "ok", "env_keys": 3, "awaiting_respawn": ["BACKEND_MANIFEST_KEY"]}
    return bus


def _recording_orchestrate(monkeypatch: pytest.MonkeyPatch, admin: _Admin, bus: FakeBus) -> list[dict[str, Any]]:
    """Wire the ops' pipeline to ``admin`` and ``bus``, its roll recycling each expected worker it rolls over."""
    from tai42_skeleton.app.recycle import RECYCLED, RecycleReport, RecycleRow

    calls: list[dict[str, Any]] = []

    async def orchestrate(bus: Any, *, decision: Any, expected: Any, **kwargs: Any) -> RecycleReport:
        calls.append({"decision": decision, **kwargs})
        return RecycleReport(
            rows=[
                RecycleRow(
                    name=name,
                    kind=kind.value,
                    generation_before=generation,
                    status=RECYCLED,
                    keys=list(decision.reported[name].keys) if name in decision.reported else [],
                )
                for name, (kind, generation) in sorted(expected.items())
                if decision.rolls_over(name)
            ]
        )

    service = config_ops.ConfigService(
        config_manager=None,  # type: ignore[arg-type]  — a reload reads no store
        admin=admin,  # type: ignore[arg-type]
        bus=bus,  # type: ignore[arg-type]
        combo_check=ComboChecks(),
        orchestrate=orchestrate,
    )
    monkeypatch.setattr(config_ops.ConfigService, "from_app", classmethod(lambda cls: service))
    return calls


@pytest.mark.parametrize("door", ["reload_config", "fleet_reload_config"])
async def test_a_reload_door_restarts_the_sibling_reporting_drift_and_leaves_the_writer_running(
    door: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tai42_skeleton.operations import backend as backend_ops

    admin = _Admin({"status": "ok", "env_keys": 3, "awaiting_respawn": []})
    bus = _install(monkeypatch, admin=admin, bus=_moved_fleet())
    calls = _recording_orchestrate(monkeypatch, admin, bus)
    op = config_ops.reload_config if door == "reload_config" else backend_ops.fleet_reload_config

    with writer_exit_scope() as exits:
        admit_change(bus)
        response = await op(None)
        await exits.settle()

    (call,) = calls
    assert call["applier_self_deferred"] is False
    assert bus.exit_requests == []
    roll = response["recycle"]
    assert [(e["name"], e["status"], e["keys"]) for e in roll["recycle"]] == [
        ("serve-b", "recycled", ["BACKEND_MANIFEST_KEY"])
    ]
    assert response["awaiting_respawn"] == []


@pytest.mark.parametrize("door", ["reload_config", "fleet_reload_config"])
async def test_a_reload_door_whose_writer_reports_drift_runs_its_exit_after_the_reply(
    door: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tai42_skeleton.operations import backend as backend_ops

    admin = _Admin({"status": "ok", "env_keys": 3, "awaiting_respawn": ["BACKEND_MANIFEST_KEY"]})
    bus = _install(monkeypatch, admin=admin, bus=FakeBus(remotes=["serve-b"]))
    calls = _recording_orchestrate(monkeypatch, admin, bus)
    op = config_ops.reload_config if door == "reload_config" else backend_ops.fleet_reload_config

    with writer_exit_scope() as exits:
        admit_change(bus)
        response = await op(None)
        # The writer holds its exit until the edge answers the change.
        assert bus.exit_deferred is not None
        assert bus.exit_requests == []
        await exits.settle()

    (call,) = calls
    assert call["applier_self_deferred"] is True
    assert call["decision"].clean == {"serve-b"}
    assert bus.exit_requests == [WRITER_EXIT_REASON]
    assert bus.exit_deferred is None
    assert response["recycle"]["recycle"] == [
        {
            "name": bus.member_identity.name,
            "kind": "serve",
            "status": "self-deferred",
            "generation_before": bus.member_identity.generation,
            "keys": ["BACKEND_MANIFEST_KEY"],
            "detail": None,
        }
    ]
    assert response["awaiting_respawn"] == []


def test_the_env_door_reply_carries_the_operation_verdict_not_the_local_fact(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_contract.app.responses import AwaitingWorker

    from tai42_skeleton.app.bus import FleetResult
    from tai42_skeleton.config.service import ApplyResult
    from tai42_skeleton.operations._broadcast import apply_response

    result = ApplyResult(
        fleet=FleetResult(op="reload_config", results=[]),
        local={"status": "ok", "env_keys": 1, "awaiting_respawn": ["BACKEND_MANIFEST_KEY"]},
        awaiting_respawn=[AwaitingWorker(name="serve-2", keys=["BACKEND_MANIFEST_KEY"])],
    )
    monkeypatch.setattr(instance.app, "_bus", FakeBus())
    body = apply_response(result)
    assert body["awaiting_respawn"] == [{"name": "serve-2", "keys": ["BACKEND_MANIFEST_KEY"]}]
    assert body["status"] == "ok"
    assert body["env_keys"] == 1


def _project(op: Any) -> Any:
    """Project one operation through the real projection seam and return its tool function."""
    from tai42_skeleton.operations import OperationRegistry, operation_metadata_of
    from tai42_skeleton.operations.projection import project_operations

    reg = OperationRegistry()
    reg.register(operation_metadata_of(op))
    registered: dict[str, Any] = {}

    class _Tools:
        def tool(self, *, force, name, tags, annotations):  # type: ignore[no-untyped-def]
            def decorator(func):  # type: ignore[no-untyped-def]
                registered[name] = func
                return func

            return decorator

    project_operations(
        SimpleNamespace(tools=_Tools()),
        ApiToolsConfig(include=[operation_metadata_of(op).name], expose_destructive=True),
        registry=reg,
    )
    return registered[operation_metadata_of(op).name]


async def test_a_projected_env_write_runs_the_writer_self_exit_after_its_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every tool door flows through the projection: after a converged roll the writer recycles
    # itself after replying there too, so the one reply — its self-deferred line, nobody awaiting
    # — is true on every edge.
    bus = _install(monkeypatch, admin=_Admin({}))
    _stub_env_service(monkeypatch, _env_result(armed=True))
    exits = bus.exit_requests

    reply = await _project(config_ops.write_env)(env={"BACKEND_MANIFEST_KEY": "NEW"})

    assert [entry["status"] for entry in reply["recycle"]["recycle"]] == ["recycled", "self-deferred"]
    assert reply["awaiting_respawn"] == []
    # The exit was delivered by the time the call returned; the worker's graceful drain is what
    # lets the reply leave before the process stops.
    assert exits == [WRITER_EXIT_REASON]


async def test_a_projected_env_write_whose_roll_stopped_runs_no_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = _install(monkeypatch, admin=_Admin({}))
    _stub_env_service(monkeypatch, _env_result(armed=False))
    exits = bus.exit_requests

    await _project(config_ops.write_env)(env={"BACKEND_MANIFEST_KEY": "NEW"})

    assert exits == []


async def test_a_projected_profile_apply_runs_the_applier_self_exit_after_its_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tai42_contract.settings_profiles import SettingsProfileBody

    from tai42_skeleton.app.bus import FleetResult
    from tai42_skeleton.app.recycle import RECYCLED, RecycleReport, RecycleRow
    from tai42_skeleton.config.service import ProfileApplyOutcome

    bus = _install(monkeypatch, admin=_Admin({}))
    exits = bus.exit_requests
    outcome = ProfileApplyOutcome(
        hot=[],
        recycle=RecycleReport(
            rows=[RecycleRow(name="backend-1", kind="backend", generation_before=1, status=RECYCLED)]
        ),
        decision=_writer_moved_decision("serve-applier", 3),
        fleet=FleetResult(op="reload_config", results=[]),
        self_exit_armed=True,
    )

    class _Svc:
        async def apply_replace_env(self, env, *, save_previous):  # type: ignore[no-untyped-def]
            return _owe_writer_exit(outcome)

    class _Store:
        async def get_active_body(self, name: str) -> Any:
            return SettingsProfileBody(env={"BACKEND_MANIFEST_KEY": "NEW"}, secret_keys=[])

    monkeypatch.setattr(config_ops.ConfigService, "from_app", classmethod(lambda cls: _Svc()))
    monkeypatch.setattr(profile_ops, "_require_profile_store", lambda: None)
    monkeypatch.setattr(profile_ops, "_profile_store", lambda: _Store())

    reply = await _project(profile_ops.apply_profile)(name="p")

    assert [entry["status"] for entry in reply["recycle"]] == ["recycled", "self-deferred"]
    assert reply["awaiting_respawn"] == []
    assert exits == [WRITER_EXIT_REASON]
