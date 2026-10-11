"""Op-level oracles for the config operations.

``reload_config`` is a convergence op: it applies locally when this worker is a
target, then broadcasts, embedding the per-origin fleet report; a failed local
reload publishes anyway and re-raises with the report. The projection carries
``destructiveHint``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.manifest import ApiToolsConfig

from tai42_skeleton.app import graceful_exit, instance
from tai42_skeleton.app.bus import OpOutcome
from tai42_skeleton.operations import BadRequestError, OperationRegistry, operation_metadata_of
from tai42_skeleton.operations import config as config_ops
from tai42_skeleton.operations._broadcast import FleetBroadcastError
from tai42_skeleton.operations.projection import project_operations

from .._fakes.bus import FakeBus


class _Admin:
    def __init__(self, result: object, *, raise_reload: Exception | None = None) -> None:
        self._result = result
        self._raise_reload = raise_reload
        self.calls = 0

    def reload_config(self) -> object:
        self.calls += 1
        if self._raise_reload is not None:
            raise self._raise_reload
        return self._result


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
    admin = _Admin({"status": "ok", "env_keys": 3})
    bus = _install(monkeypatch, admin=admin)

    result = await config_ops.reload_config()

    assert admin.calls == 1
    assert bus.publish_calls[0][0] == {"op": "reload_config"}
    assert bus.publish_calls[0][1] is None
    assert result["results"][0]["payload"] == {"status": "ok", "env_keys": 3}


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

    with pytest.raises(FleetBroadcastError) as exc:
        await config_ops.reload_config()

    assert len(bus.publish_calls) == 1
    self_result = bus.publish_calls[0][2]
    assert self_result is not None
    assert self_result.outcome == OpOutcome.failed
    assert exc.value.report.op == "reload_config"


# -- write_env ----------------------------------------------------------------


async def test_write_env_maps_manager_value_error_to_400(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Manager:
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
    class _Manager:
        def __init__(self) -> None:
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
    admin = _Admin({"status": "ok", "env_keys": 1})
    _install(monkeypatch, admin=admin, manager=manager)

    result = await config_ops.write_env({"NEW": "v"})

    # The env was validated (effective config resolved), persisted, reloaded locally,
    # and the reload broadcast; a lone worker collapses the fan-out to the local note. A
    # hot-only change rolls no recycle and arms no self-exit.
    assert manager.written == [{"NEW": "v"}]
    assert result.background is None
    assert result.payload == {
        "status": "ok",
        "env_keys": 1,
        "fanout": {"mode": "local-only", "note": "no worker bus configured; only this worker reloaded"},
        "recycle": None,
        "awaiting_respawn": [],
    }


async def test_write_env_removing_bus_with_backend_maps_to_400(monkeypatch: pytest.MonkeyPatch) -> None:
    # A registered backend needs the bus; an env change that empties TAI_BUS_REDIS_URL
    # leaves it with none, so ConfigService raises the RuntimeError
    # ``BackendNeedsBusError`` at MUTATE time. The op must map it to a loud 400 naming
    # TAI_BUS_REDIS_URL, not let it escape as an unhandled 500.
    monkeypatch.delenv("TAI_BUS_REDIS_URL", raising=False)

    class _Manager:
        def __init__(self) -> None:
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


def _env_result(*, armed: bool) -> object:
    from tai42_skeleton.app.bus import FleetResult, WorkerIdentity, WorkerKind
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
        recycle=report,
        self_identity=WorkerIdentity(name="serve-writer", kind=WorkerKind.serve, pid=1, generation=4),
        serve_affecting=True,
    )


def _stub_env_service(monkeypatch: pytest.MonkeyPatch, result: object) -> None:
    class _Svc:
        async def apply_env_change(self, env, secret_keys=None):  # type: ignore[no-untyped-def]
            return result

        async def apply_env_and_change(self, prepare, *, manifest_pointer=None):  # type: ignore[no-untyped-def]
            return result

    monkeypatch.setattr(config_ops.ConfigService, "from_app", classmethod(lambda cls: _Svc()))


async def test_write_env_arms_the_writer_self_exit_when_its_recycle_converged(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_skeleton.operations import OperationResponse

    _install(monkeypatch, admin=_Admin({}))
    _stub_env_service(monkeypatch, _env_result(armed=True))

    response = await config_ops.write_env({"BACKEND_MANIFEST_KEY": "NEW"})

    assert isinstance(response, OperationResponse)
    assert response.background is not None
    assert response.background.func is graceful_exit.request_serve_graceful_exit
    roll = response.payload["recycle"]
    assert [entry["status"] for entry in roll["recycle"]] == ["recycled", "self-deferred"]
    assert roll["recycle"][-1] == {
        "name": "serve-writer",
        "kind": "serve",
        "status": "self-deferred",
        "generation_before": 4,
        "detail": None,
    }
    assert roll["recycle_stopped"] is None
    assert "NEW" not in str(response.payload)


async def test_write_env_arms_nothing_when_its_recycle_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_skeleton.operations import OperationResponse

    _install(monkeypatch, admin=_Admin({}))
    _stub_env_service(monkeypatch, _env_result(armed=False))

    response = await config_ops.write_env({"BACKEND_MANIFEST_KEY": "NEW"})

    assert isinstance(response, OperationResponse)
    assert response.background is None
    assert response.payload["recycle"]["recycle_stopped"] == {"kind": "backend", "name": "backend-1", "detail": "slow"}


async def test_set_mcp_secret_env_arms_the_writer_self_exit_when_its_recycle_converged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tai42_skeleton.operations import OperationResponse
    from tai42_skeleton.operations.manifest import secret_env

    _install(monkeypatch, admin=_Admin({}))
    _stub_env_service(monkeypatch, _env_result(armed=True))

    response = await secret_env.set_mcp_secret_env("s3cr3t", "mcp/0/config/headers/Authorization", key_hint="gh")

    assert isinstance(response, OperationResponse)
    assert response.background is not None
    assert response.background.func is graceful_exit.request_serve_graceful_exit


# -- the bare reload doors judge the rows they collected ------------------------


async def test_reload_config_logs_the_workers_awaiting_a_respawn(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    _install(monkeypatch, admin=_Admin({"status": "ok", "env_keys": 3, "awaiting_respawn": ["BACKEND_MANIFEST_KEY"]}))
    with caplog.at_level(logging.ERROR):
        await config_ops.reload_config()
    lines = [r.getMessage() for r in caplog.records if "awaiting a respawn" in r.getMessage()]
    assert len(lines) == 1
    assert "serve-test (BACKEND_MANIFEST_KEY)" in lines[0]


async def test_fleet_reload_config_logs_the_workers_awaiting_a_respawn(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    from tai42_skeleton.operations import backend as backend_ops

    _install(monkeypatch, admin=_Admin({"status": "ok", "env_keys": 3, "awaiting_respawn": ["BACKEND_MANIFEST_KEY"]}))
    with caplog.at_level(logging.ERROR):
        report = await backend_ops.fleet_reload_config(None)
    assert report.results[0].payload == {"status": "ok", "env_keys": 3, "awaiting_respawn": ["BACKEND_MANIFEST_KEY"]}
    lines = [r.getMessage() for r in caplog.records if "awaiting a respawn" in r.getMessage()]
    assert len(lines) == 1
    assert "serve-test (BACKEND_MANIFEST_KEY)" in lines[0]


def test_the_env_door_reply_carries_the_operation_verdict_not_the_local_fact() -> None:
    from tai42_contract.app.responses import AwaitingWorker

    from tai42_skeleton.app.bus import FleetResult
    from tai42_skeleton.config.service import ApplyResult
    from tai42_skeleton.operations._broadcast import apply_response

    result = ApplyResult(
        fleet=FleetResult(op="reload_config", results=[]),
        local={"status": "ok", "env_keys": 1, "awaiting_respawn": ["BACKEND_MANIFEST_KEY"]},
        awaiting_respawn=[AwaitingWorker(name="serve-2", keys=["BACKEND_MANIFEST_KEY"])],
    )
    instance.app._bus = FakeBus()  # type: ignore[attr-defined]
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


def _record_exits(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    exits: list[str] = []
    monkeypatch.setattr(graceful_exit, "_deliver_self_sigterm", lambda: exits.append("sigterm"))
    return exits


async def _let_the_post_reply_task_run() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


async def test_a_projected_env_write_runs_the_writer_self_exit_after_its_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every tool door flows through the projection: after a converged roll the writer recycles
    # itself after replying there too, so the one reply — its self-deferred line, nobody awaiting
    # — is true on every edge.
    _install(monkeypatch, admin=_Admin({}))
    _stub_env_service(monkeypatch, _env_result(armed=True))
    exits = _record_exits(monkeypatch)

    reply = await _project(config_ops.write_env)(env={"BACKEND_MANIFEST_KEY": "NEW"})

    assert [entry["status"] for entry in reply["recycle"]["recycle"]] == ["recycled", "self-deferred"]
    assert reply["awaiting_respawn"] == []
    assert exits == []  # nothing runs before the reply is handed back
    await _let_the_post_reply_task_run()
    assert exits == ["sigterm"]


async def test_a_projected_env_write_whose_roll_stopped_runs_no_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, admin=_Admin({}))
    _stub_env_service(monkeypatch, _env_result(armed=False))
    exits = _record_exits(monkeypatch)

    await _project(config_ops.write_env)(env={"BACKEND_MANIFEST_KEY": "NEW"})
    await _let_the_post_reply_task_run()

    assert exits == []


async def test_a_projected_profile_apply_runs_the_applier_self_exit_after_its_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tai42_contract.settings_profiles import SettingsProfileBody

    from tai42_skeleton.app.bus import FleetResult, WorkerIdentity, WorkerKind
    from tai42_skeleton.app.recycle import RECYCLED, RecycleReport, RecycleRow
    from tai42_skeleton.config.service import ProfileApplyOutcome

    _install(monkeypatch, admin=_Admin({}))
    exits = _record_exits(monkeypatch)
    outcome = ProfileApplyOutcome(
        hot=[],
        recycle=RecycleReport(
            rows=[RecycleRow(name="backend-1", kind="backend", generation_before=1, status=RECYCLED)]
        ),
        self_identity=WorkerIdentity(name="serve-applier", kind=WorkerKind.serve, pid=1, generation=3),
        serve_affecting=True,
        fleet=FleetResult(op="reload_config", results=[]),
    )

    class _Svc:
        async def apply_replace_env(self, env, *, save_previous):  # type: ignore[no-untyped-def]
            return outcome

    class _Store:
        async def get_active_body(self, name: str) -> Any:
            return SettingsProfileBody(env={"BACKEND_MANIFEST_KEY": "NEW"}, secret_keys=[])

    monkeypatch.setattr(config_ops.ConfigService, "from_app", classmethod(lambda cls: _Svc()))
    monkeypatch.setattr(config_ops, "_require_profile_store", lambda: None)
    monkeypatch.setattr(config_ops, "_profile_store", lambda: _Store())

    reply = await _project(config_ops.apply_profile)(name="p")

    assert [entry["status"] for entry in reply["recycle"]] == ["recycled", "self-deferred"]
    assert reply["awaiting_respawn"] == []
    await _let_the_post_reply_task_run()
    assert exits == ["sigterm"]
