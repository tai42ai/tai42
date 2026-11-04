"""Unit oracles for the settings-profile APPLY pipeline
(:meth:`~tai42_skeleton.config.service.ConfigService.apply_replace_env`) and its
dedicated response builder
(:func:`~tai42_skeleton.operations._broadcast.profile_apply_response`).

The pipeline is env-write-LAST: a failed build leaves the STORE untouched and the old
surface serving, so ordering + failure discipline is the load-bearing contract. These
drive the service against the shared fakes, with the ``build_and_swap`` / ``orchestrate``
seams injected so the ordering / drain / recycle / self-exit contract is asserted without
booting a server. One test uses the REAL :func:`build_and_swap_epoch` (its own injectable
``build_serving_app`` failing) to prove the os.environ restore + store-untouched invariant.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections.abc import Awaitable, Callable, Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from tai42_kit.fork_gate import fork_gate
from tests._fakes.rebuild import no_rebuild

# ``bus_settings`` is imported for its registration side effect — the apply's recycle
# classification reflects only IMPORTED settings classes, and TAI_BUS_* are recycle-class.
import tai42_skeleton.app.bus_settings  # noqa: F401
from tai42_skeleton.app import admission as admission_mod
from tai42_skeleton.app import applied_env, instance
from tai42_skeleton.app import epoch as epoch_mod
from tai42_skeleton.app.admission import Admission
from tai42_skeleton.app.bus import FleetResult, WorkerIdentity, WorkerKind, WorkerRow, WorkerState
from tai42_skeleton.app.epoch import Epoch, build_and_swap_epoch
from tai42_skeleton.app.recycle import (
    FAILED,
    RECYCLED,
    TIMED_OUT,
    FreshLife,
    RecycleError,
    RecycleReport,
    RecycleRow,
    RecycleStop,
    RecycleTimeoutError,
    orchestrate_recycle,
)
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.config.deployment_env import DeploymentOwnership, published_env_readers
from tai42_skeleton.config.recycle_policy import RecycleDecision, RecycleReason, ReportedKeys
from tai42_skeleton.config.service import ConfigService, ProfileApplyOutcome
from tai42_skeleton.operations._broadcast import SELF_DEFERRED, FleetBroadcastError, profile_apply_response
from tai42_skeleton.tools.binding.surface import bump_tool_surface_generation

from .._fakes.bus import FakeBus
from .._helpers import recapture_deployment_env
from ..connectors.credential_path import job_loop, owner_loop
from .fake_pipeline import ComboChecks, FakeConfigStore, FakeReloadAdmin, _orchestrate_spy

# The door's edge settles the writer's exit its change owes; these tests drive the door below it.
pytestmark = pytest.mark.usefixtures("writer_exit_edge")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _service(
    store: FakeConfigStore,
    bus: FakeBus | None = None,
    *,
    orchestrate: Callable[..., Awaitable[RecycleReport]] = orchestrate_recycle,
    combo_check: ComboChecks | None = None,
) -> ConfigService:
    return ConfigService(
        config_manager=store,
        admin=FakeReloadAdmin(),
        bus=cast("Any", bus or FakeBus()),
        combo_check=combo_check or ComboChecks(),
        orchestrate=orchestrate,
    )


class _PrevSpy:
    """Records the stored env handed to the ``@previous`` snapshot callback."""

    def __init__(self) -> None:
        self.saved: dict[str, str] | None = None

    async def __call__(self, stored_env: dict[str, str]) -> None:
        self.saved = dict(stored_env)


def _swap_spy() -> tuple[Callable[..., Awaitable[Epoch]], dict[str, Any]]:
    """A ``build_and_swap`` seam that records its call and swaps nothing."""
    calls: dict[str, Any] = {}

    async def spy(env: dict[str, str], *, ownership: DeploymentOwnership) -> Epoch:
        calls["env"] = dict(env)
        return Epoch(number=0)

    return spy, calls


def _discard_spy() -> tuple[Callable[..., Awaitable[None]], dict[str, Any]]:
    """A ``build_and_discard`` seam that records its call and builds nothing."""
    calls: dict[str, Any] = {}

    async def spy(env: dict[str, str], *, ownership: DeploymentOwnership) -> None:
        calls["env"] = dict(env)

    return spy, calls


_MOVED = {"status": "ok", "env_keys": 1, "awaiting_respawn": ["TAI_BUS_HEARTBEAT_TTL"]}


class _MovedBus(FakeBus):
    """A fake bus whose every sibling answers the reload with the fact that its env moved."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        for name in kwargs.get("remotes") or []:
            self.remote_payloads[name] = dict(_MOVED)


def _writer_moved(monkeypatch: pytest.MonkeyPatch) -> None:
    """The applier's own swap states the recycle-class key moved off the value it bound it under."""
    from tai42_skeleton.app.lifecycle import config_reload

    monkeypatch.setattr(config_reload, "awaiting_respawn", lambda: ["TAI_BUS_HEARTBEAT_TTL"])


# ---------------------------------------------------------------------------
# Ordering / failure — env-write-LAST
# ---------------------------------------------------------------------------


@pytest.fixture
def _epoch_state() -> Iterator[None]:
    """Reset the epoch spine globals so a REAL ``build_and_swap_epoch`` failure path runs
    in isolation; the suite-wide ``_preserve_generation_globals`` fixture restores the
    per-generation registries the build stages."""
    loaded_before = set(applied_env._loaded_env_keys)
    try:
        yield
    finally:
        for name in ("_current", "_serving_slot"):
            setattr(epoch_mod, name, None)
        applied_env._loaded_env_keys = loaded_before


class _BuildBoomError(RuntimeError):
    pass


@pytest.mark.usefixtures("_epoch_state")
async def test_failed_build_leaves_store_untouched_and_restores_env(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeConfigStore(env={"APP_KEY": "stored", "OLD_ONLY": "keep"})
    prev = _PrevSpy()
    env_before = dict(os.environ)

    async def _fail_serve(_epoch: Epoch) -> Any:
        raise _BuildBoomError("deliberate build failure under the proposed env")

    async def _noop() -> None:
        return None

    async def real_build(env: dict[str, str], *, ownership: DeploymentOwnership) -> Epoch:
        # Drive the REAL primitive with a failing build_serving_app seam so its
        # os.environ restore-on-failure + staged-abort actually run.
        return await build_and_swap_epoch(
            env,
            rebuild=no_rebuild,
            build_serving_app=_fail_serve,
            establish_background_loops=_noop,
            ownership=ownership,
        )

    with pytest.raises(_BuildBoomError):
        await _service(store).apply_replace_env(
            {"APP_KEY": "proposed", "NEW_KEY": "x"},
            save_previous=prev,
            build_and_swap=real_build,
        )

    # env-write-LAST: the store never saw ``replace_env`` (the persist step is unreached).
    assert store.env == {"APP_KEY": "stored", "OLD_ONLY": "keep"}
    assert store.env_writes == []
    # os.environ restored EXACTLY (the proposed keys the build applied are gone).
    assert dict(os.environ) == env_before
    # @previous WAS snapshotted before the build (harmless — reflects unchanged state).
    assert prev.saved == {"APP_KEY": "stored", "OLD_ONLY": "keep"}


_DATABASE_KEYS = ("TAI_DATABASE_DEFAULT_PG_DB", "TAI_DATABASE_DEFAULT_PG_PASSWORD", "TAI_DB_BINDING_STATES")


@pytest.mark.usefixtures("_epoch_state")
async def test_profile_apply_changing_the_default_database_retargets_the_states_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """File config mode: the stored env is the working directory's ``.env``.

    The build reads the states store's connection settings under the proposed env;
    a failed build restores the env and the store reads the stored database again;
    the stored ``.env`` rewritten in place of the old one is read with no reset.
    """
    from tai42_skeleton.states.store.connection import _settings as states_store_settings

    for key in _DATABASE_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    stored = {"TAI_DATABASE_DEFAULT_PG_PASSWORD": "pw", "TAI_DATABASE_DEFAULT_PG_DB": "first"}
    (tmp_path / ".env").write_text("".join(f"{key}={value}\n" for key, value in stored.items()))
    store = FakeConfigStore(env=dict(stored))
    assert states_store_settings().pg_db == "first"

    seen: dict[str, str | None] = {}

    async def _read_during_build(_epoch: Epoch) -> None:
        seen["during"] = states_store_settings().pg_db

    async def _fail_serve(_epoch: Epoch) -> Any:
        raise _BuildBoomError("deliberate build failure after the settings read")

    async def _noop() -> None:
        return None

    async def real_build(env: dict[str, str], *, ownership: DeploymentOwnership) -> Epoch:
        return await build_and_swap_epoch(
            env,
            rebuild=_read_during_build,
            build_serving_app=_fail_serve,
            establish_background_loops=_noop,
            ownership=ownership,
        )

    with pytest.raises(_BuildBoomError):
        await _service(store).apply_replace_env(
            {**stored, "TAI_DATABASE_DEFAULT_PG_DB": "second"},
            save_previous=_PrevSpy(),
            build_and_swap=real_build,
        )

    assert seen == {"during": "second"}
    assert states_store_settings().pg_db == "first"

    staged = tmp_path / ".env.staged"
    staged.write_text("TAI_DATABASE_DEFAULT_PG_PASSWORD=pw\nTAI_DATABASE_DEFAULT_PG_DB=third\n")
    os.replace(staged, tmp_path / ".env")
    assert states_store_settings().pg_db == "third"


# ---------------------------------------------------------------------------
# Drain — no self-deadlock
# ---------------------------------------------------------------------------


async def test_apply_records_its_own_request_as_the_swaps_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeConfigStore(env={"MY_APP_FLAG": "old"})
    serving = Epoch(number=0)
    serving.admit()  # the apply's own request, admitted on the generation the swap retires
    request = Admission(epoch=serving, kind="served_request")
    driving_at_swap: list[bool] = []

    async def spy(env: dict[str, str], *, ownership: DeploymentOwnership) -> Epoch:
        # The retire reads the generation's own record: the apply's request drives it and
        # has left the in-flight count, so the drain budget never waits on it.
        driving_at_swap.append(request in serving.driving and serving.in_flight == 0)
        return Epoch(number=0)

    token = admission_mod._admission.set(request)
    try:
        outcome = await _service(store).apply_replace_env(
            {"MY_APP_FLAG": "new"}, save_previous=_PrevSpy(), build_and_swap=spy
        )
    finally:
        admission_mod._admission.reset(token)
    assert driving_at_swap == [True]
    # env-write-LAST landed after a successful build.
    assert store.env == {"MY_APP_FLAG": "new"}
    # A hot-only diff the fleet confirms clean neither recycles nor self-exits.
    assert outcome.hot == ["MY_APP_FLAG"]
    assert outcome.recycle is None
    assert outcome.decision is not None
    assert outcome.decision.writer_needs is False


async def test_apply_holds_the_fork_gate_across_the_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """The build is a re-importing reload body (``reload_gate.run``), so the fork gate is held
    across it: its rebuild re-imports the manifest modules, and a backend work loop forking a
    job child mid-reimport leaves the child deadlocked on an inherited ``importlib`` per-module lock."""
    store = FakeConfigStore(env={"MY_APP_FLAG": "old"})
    seen: dict[str, Any] = {}

    async def _build(env: dict[str, str], *, ownership: DeploymentOwnership) -> Epoch:
        seen["blocked"] = fork_gate.blocked
        seen["owner"] = fork_gate._owner
        return Epoch(number=0)

    assert not fork_gate.blocked
    await _service(store).apply_replace_env({"MY_APP_FLAG": "new"}, save_previous=_PrevSpy(), build_and_swap=_build)
    assert seen["blocked"] is True
    # Ownership sits on the façade's dedicated holder thread, never the serving loop's.
    assert seen["owner"] is not None
    assert seen["owner"] != threading.get_ident()
    assert not fork_gate.blocked


async def test_apply_driven_from_a_job_loop_builds_on_the_serving_loop_under_the_fork_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An apply an in-process job drives from its own loop builds the epoch on the serving loop,
    where the epoch's loop-affine lifespan lives, holding the fork gate across the build."""
    store = FakeConfigStore(env={"MY_APP_FLAG": "old"})
    seen: dict[str, Any] = {}

    async def _build(env: dict[str, str], *, ownership: DeploymentOwnership) -> Epoch:
        seen["loop"] = asyncio.get_running_loop()
        seen["blocked"] = fork_gate.blocked
        return Epoch(number=0)

    with owner_loop() as serving, job_loop() as job:
        monkeypatch.setattr(instance, "_app", SimpleNamespace(serving_loop=lambda: serving))
        await job.run_awaited(
            _service(store).apply_replace_env({"MY_APP_FLAG": "new"}, save_previous=_PrevSpy(), build_and_swap=_build)
        )
    assert seen["loop"] is serving
    assert seen["blocked"] is True
    assert store.env == {"MY_APP_FLAG": "new"}


class _LoopNotingChecks(ComboChecks):
    """Combo checks that note the loop each served-tools read runs on."""

    def __init__(self, *, served: dict[str, Any]) -> None:
        super().__init__(served=served)
        self.read_on: list[asyncio.AbstractEventLoop] = []

    async def served_bases(self) -> Any:
        self.read_on.append(asyncio.get_running_loop())
        return await super().served_bases()


async def test_a_tool_surface_change_before_the_build_retakes_the_bases_on_the_serving_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tools a job-driven apply checks its combos against are re-taken right before the build,
    on the serving loop the build runs on, when the tool surface moved since step 1."""
    labelled = {"name": "labelled", "config": {"label": "y"}}
    manifest = {
        "tools": [
            {
                "title": "a",
                "module": "tests.app._fixtures.tools_check",
                "include": ["echo"],
                "extensions": {"echo": [[labelled]]},
            }
        ]
    }
    store = FakeConfigStore(manifest=manifest, env={"MY_APP_FLAG": "old"})
    before, after = object(), object()
    checks = _LoopNotingChecks(served={"echo": before})

    async def save_previous(_env: dict[str, str]) -> None:
        bump_tool_surface_generation()
        checks.served = {"echo": after}

    build, _calls = _swap_spy()
    with owner_loop() as serving, job_loop() as job:
        monkeypatch.setattr(instance, "_app", SimpleNamespace(serving_loop=lambda: serving))
        await job.run_awaited(
            _service(store, combo_check=checks).apply_replace_env(
                {"MY_APP_FLAG": "new"}, save_previous=save_previous, build_and_swap=build
            )
        )
    assert [base for _tool, _combo, base in checks.calls] == [before, after]
    assert checks.read_on[-1] is serving
    assert store.env == {"MY_APP_FLAG": "new"}


async def test_apply_releases_the_fork_gate_when_the_build_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed apply must not leave the gate held — that would block every subsequent
    job child for the process lifetime."""
    store = FakeConfigStore(env={"MY_APP_FLAG": "old"})

    async def _build(env: dict[str, str], *, ownership: DeploymentOwnership) -> Epoch:
        raise _BuildBoomError("deliberate build failure")

    with pytest.raises(_BuildBoomError):
        await _service(store).apply_replace_env({"MY_APP_FLAG": "new"}, save_previous=_PrevSpy(), build_and_swap=_build)
    assert not fork_gate.blocked
    assert not reload_gate.locked


class _StoreChangingBus(FakeBus):
    """A bus whose op-start census (its second read, after the ownership read) stores a manifest marker on a key
    the profile omits."""

    def __init__(self, store: FakeConfigStore) -> None:
        super().__init__()
        self._store = store
        self._reads = 0

    async def census(self) -> list[WorkerRow]:
        self._reads += 1
        if self._reads < 2:
            return await super().census()
        self._store.manifest["mcp"] = [
            {"title": "m", "config": {"url": "http://x", "headers": {"X-Race": "!ENV ${PROFILE_GONE}"}}}
        ]
        return await super().census()


async def test_a_store_change_before_the_gates_is_refused_before_the_build(monkeypatch: pytest.MonkeyPatch) -> None:
    # The stored manifest changes after the apply's held-unpick check, while it censuses the
    # fleet: the apply validates again before the build, so it refuses with ``@previous``
    # already written and nothing built or persisted.
    store = FakeConfigStore(env={"MY_APP_FLAG": "old"})
    prev = _PrevSpy()
    swap, swapped = _swap_spy()

    with pytest.raises(ValueError, match=r"markers dangling .*PROFILE_GONE"):
        await _service(store, _StoreChangingBus(store)).apply_replace_env(
            {"MY_APP_FLAG": "new"}, save_previous=prev, build_and_swap=swap
        )

    assert prev.saved == {"MY_APP_FLAG": "old"}
    assert swapped == {}
    assert store.env_writes == []
    assert not fork_gate.blocked
    assert not reload_gate.locked


# ---------------------------------------------------------------------------
# Serve-affecting recycle — orchestrate rolls, applier self-defers
# ---------------------------------------------------------------------------


async def test_a_recycle_class_diff_restarts_every_worker_reporting_it_and_defers_the_applier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _writer_moved(monkeypatch)
    store = FakeConfigStore(env={"TAI_BUS_HEARTBEAT_TTL": "5"})
    bus = _MovedBus(origin="serve-applier", remotes=["serve-b"])
    spy, _ = _swap_spy()
    orch, seen = _orchestrate_spy()
    outcome = await _service(store, bus, orchestrate=orch).apply_replace_env(
        {"TAI_BUS_HEARTBEAT_TTL": "7"},
        save_previous=_PrevSpy(),
        build_and_swap=spy,
    )
    # Every worker reports TAI_BUS_HEARTBEAT_TTL off its bound value: the recycle rolls over
    # serve-b, and the applier's own restart is deferred to its exit.
    assert outcome.decision is not None
    assert outcome.decision.writer_needs is True
    assert set(outcome.decision.reported) == {"serve-b"}
    assert seen["decision"].writer.name == "serve-applier"
    assert seen["decision"].writer.generation == 1
    assert seen["applier_self_deferred"] is True
    assert [k.value for k in seen["target_kinds"]] == ["backend", "serve"]
    assert outcome.recycle is not None
    assert [row.name for row in outcome.recycle.rows] == ["serve-b"]
    assert store.env == {"TAI_BUS_HEARTBEAT_TTL": "7"}


class _UnreachableBus(FakeBus):
    """A fake bus whose reload broadcast hits a transport fault: the report holds no worker row."""

    async def publish(self, op: Any, targets: Any, local: Any, *, expected_at_start: Any = None) -> FleetResult:
        await super().publish(op, targets, local, expected_at_start=expected_at_start)
        return FleetResult(op=op.op, reachable=False, error="ConnectionError: bus down")


async def test_an_applier_whose_swap_stated_no_drift_keeps_running_when_its_broadcast_misses_the_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The report holds no row for the applier, yet its own swap stated no drift: the roll asks the
    # sibling and leaves the applier running.
    store = FakeConfigStore(env={"TAI_BUS_HEARTBEAT_TTL": "5"})
    bus = _UnreachableBus(origin="serve-applier", remotes=["serve-b"])
    spy, _ = _swap_spy()
    orch, seen = _orchestrate_spy(RecycleReport())
    outcome = await _service(store, bus, orchestrate=orch).apply_replace_env(
        {"TAI_BUS_HEARTBEAT_TTL": "7"},
        save_previous=_PrevSpy(),
        build_and_swap=spy,
    )

    assert outcome.fleet.reachable is False
    assert outcome.fleet.results == []
    assert outcome.decision is not None
    assert outcome.decision.writer_reason is None
    assert set(outcome.decision.unconfirmed) == {"serve-b"}
    assert seen["applier_self_deferred"] is False


class _RaisingBus(FakeBus):
    """A fake bus whose reload broadcast raises: the error's report holds no worker row."""

    async def publish(self, op: Any, targets: Any, local: Any, *, expected_at_start: Any = None) -> FleetResult:
        await super().publish(op, targets, local, expected_at_start=expected_at_start)
        raise RuntimeError("ResponseError: WRONGTYPE")


async def test_an_applier_whose_swap_stated_no_drift_keeps_running_when_its_broadcast_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The raised broadcast leaves a report with no row for the applier, yet its own swap stated no
    # drift: the roll the error carries asks the sibling and leaves the applier running.
    store = FakeConfigStore(env={"TAI_BUS_HEARTBEAT_TTL": "5"})
    bus = _RaisingBus(origin="serve-applier", remotes=["serve-b"])
    spy, _ = _swap_spy()
    orch, seen = _orchestrate_spy(RecycleReport())

    with pytest.raises(FleetBroadcastError, match="WRONGTYPE") as exc:
        await _service(store, bus, orchestrate=orch).apply_replace_env(
            {"TAI_BUS_HEARTBEAT_TTL": "7"},
            save_previous=_PrevSpy(),
            build_and_swap=spy,
        )

    assert exc.value.report.reachable is False
    assert exc.value.report.results == []
    roll = exc.value.roll
    assert roll is not None
    assert roll.decision.writer_reason is None
    assert set(roll.decision.unconfirmed) == {"serve-b"}
    assert seen["applier_self_deferred"] is False
    assert roll.self_exit_armed is False
    assert store.env == {"TAI_BUS_HEARTBEAT_TTL": "7"}


# ---------------------------------------------------------------------------
# Refused-keys branches — each aborts upfront, naming the key, nothing persisted
# ---------------------------------------------------------------------------


async def test_apply_refuses_the_bus_url_upfront() -> None:
    # A bus URL reaches the worker bus itself, which no recycle can carry.
    store = FakeConfigStore(env={})
    spy, calls = _swap_spy()
    prev = _PrevSpy()
    with pytest.raises(ValueError, match="TAI_BUS_REDIS_URL"):
        await _service(store).apply_replace_env(
            {"TAI_BUS_REDIS_URL": "redis://localhost:6379/5"}, save_previous=prev, build_and_swap=spy
        )
    # Aborted BEFORE the census / @previous snapshot / build / persist.
    assert store.env_writes == []
    assert prev.saved is None
    assert "env" not in calls


async def test_apply_refuses_a_profile_setting_a_key_the_deployment_sets(monkeypatch: pytest.MonkeyPatch) -> None:
    # The deployment's value wins at every boot and reload, so the profile's value would never
    # take effect: refused upfront, naming the key, nothing snapshotted, built or persisted.
    monkeypatch.setenv("DEPLOYED_KEY", "from-deployment")
    recapture_deployment_env()
    store = FakeConfigStore(env={"DEPLOYED_KEY": "stale"})
    spy, calls = _swap_spy()
    prev = _PrevSpy()
    with pytest.raises(ValueError, match=r"set by the deployment.*DEPLOYED_KEY"):
        await _service(store).apply_replace_env(
            {"DEPLOYED_KEY": "from-profile", "MY_APP_FLAG": "on"}, save_previous=prev, build_and_swap=spy
        )
    assert store.env_writes == []
    assert prev.saved is None
    assert "env" not in calls


async def test_apply_drops_or_carries_a_stored_copy_of_a_key_the_deployment_sets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A profile may drop a stored copy of a deployment key or carry it unchanged: neither sets it.
    monkeypatch.setenv("DEPLOYED_KEY", "from-deployment")
    monkeypatch.setenv("DEPLOYED_OTHER", "from-deployment")
    recapture_deployment_env()
    store = FakeConfigStore(env={"DEPLOYED_KEY": "stale", "DEPLOYED_OTHER": "kept"})
    spy, _calls = _swap_spy()
    await _service(store).apply_replace_env(
        {"DEPLOYED_OTHER": "kept", "MY_APP_FLAG": "on"}, save_previous=_PrevSpy(), build_and_swap=spy
    )
    assert store.env == {"DEPLOYED_OTHER": "kept", "MY_APP_FLAG": "on"}


async def test_applying_the_previous_snapshot_undoes_an_apply_that_removed_a_dead_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The snapshot holds the stored env that counted before the apply: the stored copy of a key
    # the deployment sets is not part of it, so applying the snapshot sets no deployment key.
    monkeypatch.setenv("DEPLOYED_KEY", "from-deployment")
    recapture_deployment_env()
    store = FakeConfigStore(env={"DEPLOYED_KEY": "stale", "MY_APP_FLAG": "one"})
    spy, _calls = _swap_spy()
    prev = _PrevSpy()
    await _service(store).apply_replace_env({"MY_APP_FLAG": "two"}, save_previous=prev, build_and_swap=spy)
    snapshot = prev.saved
    assert snapshot is not None
    assert snapshot == {"MY_APP_FLAG": "one"}
    assert store.env == {"MY_APP_FLAG": "two"}

    revert_spy, revert_calls = _swap_spy()
    await _service(store).apply_replace_env(snapshot, save_previous=_PrevSpy(), build_and_swap=revert_spy)
    assert store.env == {"MY_APP_FLAG": "one"}
    assert revert_calls["env"] == {"MY_APP_FLAG": "one"}


async def test_apply_rolls_no_recycle_for_a_removed_dead_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    # TAI_BUS_HEARTBEAT_TTL is recycle-class; the deployment sets it, so its stored copy never counted
    # and dropping it changes nothing the reload produces: no hot key, no recycle, no self-exit.
    monkeypatch.setenv("TAI_BUS_HEARTBEAT_TTL", "4")
    recapture_deployment_env()
    store = FakeConfigStore(env={"TAI_BUS_HEARTBEAT_TTL": "3"})
    bus = FakeBus(origin="serve-applier", remotes=["serve-b"])
    spy, _calls = _swap_spy()
    orch, seen = _orchestrate_spy()
    outcome = await _service(store, bus, orchestrate=orch).apply_replace_env(
        {}, save_previous=_PrevSpy(), build_and_swap=spy
    )
    assert outcome.hot == []
    assert outcome.recycle is None
    assert outcome.decision is not None
    assert outcome.decision.writer_needs is False
    assert seen == {}
    assert store.env == {}


async def test_apply_refuses_a_drain_budget_past_the_apply_window(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_kit.settings import reset_all_settings

    from tai42_skeleton.app.boot_rules import ApplyWindowBelowDrainError

    monkeypatch.setenv("TAI_BUS_REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("TAI_BUS_APPLY_TIMEOUT", "90")
    monkeypatch.delenv("TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS", raising=False)
    reset_all_settings()
    store = FakeConfigStore(env={})
    spy, calls = _swap_spy()
    prev = _PrevSpy()
    try:
        with pytest.raises(ApplyWindowBelowDrainError, match="270"):
            await _service(store).apply_replace_env(
                {"TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS": "90"}, save_previous=prev, build_and_swap=spy
            )
    finally:
        reset_all_settings()
    # Refused before the @previous snapshot, the build and the persist.
    assert store.env_writes == []
    assert prev.saved is None
    assert "env" not in calls


# ---------------------------------------------------------------------------
# NAMES-ONLY — a sentinel secret VALUE never reaches the report
# ---------------------------------------------------------------------------


async def test_apply_report_carries_names_never_values(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = "s3cr3t-sentinel-VALUE-must-not-leak"
    store = FakeConfigStore(env={"APP_SECRET": "old"})
    bus = FakeBus(origin="serve-applier")
    # ``fleet_fanout`` reads the process bus origin to decide local-only vs fleet; drive
    # the pipeline through that same bus, installed as ``instance.app.bus``.
    monkeypatch.setattr(instance.app, "_bus", bus)
    spy, _ = _swap_spy()
    outcome = await _service(store, bus).apply_replace_env(
        {"APP_SECRET": sentinel}, save_previous=_PrevSpy(), build_and_swap=spy
    )
    response = profile_apply_response(outcome)
    blob = json.dumps(response)
    assert sentinel not in blob  # the VALUE never rides the report
    assert "APP_SECRET" in blob  # the NAME does (a hot diff)
    # The whole serialized outcome (report material) is names-only too.
    material = json.dumps(
        {
            "hot": outcome.hot,
            "writer": outcome.decision.writer.model_dump() if outcome.decision else None,
            "recycle": outcome.recycle.model_dump() if outcome.recycle else None,
        }
    )
    assert sentinel not in material
    # The store DID receive the secret value (it persists; only the report must not).
    assert store.env == {"APP_SECRET": sentinel}


# ---------------------------------------------------------------------------
# Response shape — refused==[] on success, applier line status==SELF_DEFERRED,
# recycle rows carry {name, kind, status, generation_before}, per-kind fresh list
# ---------------------------------------------------------------------------


def _identity(name: str, generation: int) -> WorkerIdentity:
    return WorkerIdentity(name=name, kind=WorkerKind.serve, pid=1, generation=generation)


def _decision(
    writer: WorkerIdentity, *, writer_needs: bool, reported: tuple[str, ...] = (), clean: tuple[str, ...] = ()
) -> RecycleDecision:
    """A recycle decision whose ``reported`` workers and writer (when it needs its restart) report TAI_X."""
    return RecycleDecision(
        writer=writer,
        reported=dict.fromkeys(reported, ReportedKeys(generation=1, keys=("TAI_X",))),
        unconfirmed={},
        clean=frozenset(clean),
        writer_reason=RecycleReason(keys=("TAI_X",)) if writer_needs else None,
        withheld={},
        addressed=None,
        report_unreachable=False,
    )


def test_profile_apply_response_shape() -> None:
    outcome = ProfileApplyOutcome(
        hot=["HOT_A", "HOT_B"],
        recycle=RecycleReport(
            rows=[
                RecycleRow(name="serve-b", kind="serve", generation_before=1, status=RECYCLED, keys=["TAI_X"]),
                RecycleRow(name="backend-z", kind="backend", generation_before=4, status=RECYCLED, keys=["TAI_X"]),
            ],
            fresh=[FreshLife(name="serve-b", kind="serve", generation=2)],
        ),
        decision=_decision(_identity("serve-applier", 7), writer_needs=True, reported=("serve-b", "backend-z")),
        fleet=FleetResult(op="reload_config", results=[]),
        self_exit_armed=True,
    )
    response = profile_apply_response(outcome)
    assert response["hot"] == ["HOT_A", "HOT_B"]
    assert response["refused"] == []  # empty on success BY CONSTRUCTION
    # Recycle rows carry the pre-apply identity + terminal status + detail; no generation_after.
    assert {
        "name": "serve-b",
        "kind": "serve",
        "status": "recycled",
        "generation_before": 1,
        "keys": ["TAI_X"],
        "detail": None,
    } in response["recycle"]
    assert {
        "name": "backend-z",
        "kind": "backend",
        "status": "recycled",
        "generation_before": 4,
        "keys": ["TAI_X"],
        "detail": None,
    } in response["recycle"]
    # The per-kind fresh list surfaces the new lives — informational, never a successor.
    assert response["fresh"] == [{"name": "serve-b", "kind": "serve", "generation": 2}]
    # The applier's OWN line — always kind "serve", carrying its own current generation.
    applier = [entry for entry in response["recycle"] if entry["status"] == SELF_DEFERRED]
    assert applier == [
        {
            "name": "serve-applier",
            "kind": "serve",
            "status": SELF_DEFERRED,
            "generation_before": 7,
            "keys": ["TAI_X"],
            "detail": None,
        }
    ]
    assert response["skipped"] == []
    # No generation_after / names-only replacements anywhere in the surfaced report.
    blob = json.dumps(response)
    assert "generation_after" not in blob
    assert "replacements" not in blob
    assert "fanout" in response
    assert response["recycle_stopped"] is None


def test_a_profile_apply_the_fleet_confirms_clean_reports_every_worker_skipped() -> None:
    outcome = ProfileApplyOutcome(
        hot=["HOT_A"],
        recycle=None,
        decision=_decision(_identity("serve-applier", 1), writer_needs=False, clean=("serve-c", "backend-1")),
        fleet=FleetResult(op="reload_config", results=[]),
    )
    response = profile_apply_response(outcome)
    assert response["recycle"] == []  # no applier line, no siblings
    assert response["skipped"] == ["backend-1", "serve-c"]
    assert response["fresh"] == []
    assert response["refused"] == []


def test_recycle_rows_carry_their_own_kind() -> None:
    """The recycle report rows carry kind natively (from the orchestrator's per-kind
    roll), so the response reads kind straight off the row — no separate name->kind map."""
    now = "2026-01-01T00:00:00+00:00"
    bus = FakeBus(origin="serve-applier", remotes=["serve-b"])
    row = WorkerRow(
        name="backend-1",
        kind=WorkerKind.backend,
        pid=9,
        generation=1,
        joined_at=now,
        beat_at=now,
        state=WorkerState.ready,
        deployment_keys=[],
        env_readers="",
    )
    outcome = ProfileApplyOutcome(
        hot=[],
        recycle=RecycleReport(
            rows=[
                RecycleRow(
                    name=row.name,
                    kind=row.kind.value,
                    generation_before=row.generation,
                    status=RECYCLED,
                    keys=["TAI_X"],
                )
            ],
            fresh=[],
        ),
        decision=_decision(bus.identity, writer_needs=False, reported=("backend-1",)),
        fleet=FleetResult(op="reload_config", results=[]),
    )
    (entry,) = profile_apply_response(outcome)["recycle"]
    assert entry == {
        "name": "backend-1",
        "kind": "backend",
        "status": "recycled",
        "generation_before": 1,
        "keys": ["TAI_X"],
        "detail": None,
    }


def test_a_roll_that_stopped_yields_its_rows_and_no_self_deferred_entry() -> None:
    # The roll stopped at a timed-out row: the applier's own recycle is not armed, so
    # no self-deferred line appears although the diff is serve-affecting.
    detail = "old life still present; the recycle op was not confirmed by the target (worker missing)"
    outcome = ProfileApplyOutcome(
        hot=[],
        recycle=RecycleReport(
            rows=[
                RecycleRow(name="backend-1", kind="backend", generation_before=1, status=RECYCLED, keys=["TAI_X"]),
                RecycleRow(
                    name="backend-2",
                    kind="backend",
                    generation_before=3,
                    status=TIMED_OUT,
                    keys=["TAI_X"],
                    detail=detail,
                ),
            ],
            stopped=RecycleStop(kind="backend", name="backend-2", detail=detail),
        ),
        decision=_decision(_identity("serve-applier", 7), writer_needs=True, reported=("backend-1", "backend-2")),
        fleet=FleetResult(op="reload_config", results=[]),
    )
    assert outcome.self_exit_armed is False
    response = profile_apply_response(outcome)
    assert response["recycle"] == [
        {
            "name": "backend-1",
            "kind": "backend",
            "status": "recycled",
            "generation_before": 1,
            "keys": ["TAI_X"],
            "detail": None,
        },
        {
            "name": "backend-2",
            "kind": "backend",
            "status": "timed-out",
            "generation_before": 3,
            "keys": ["TAI_X"],
            "detail": detail,
        },
    ]
    assert response["recycle_stopped"] == {"kind": "backend", "name": "backend-2", "detail": detail}


def test_a_roll_stopped_with_no_target_in_hand_yields_the_stop_and_no_self_deferred_entry() -> None:
    # Every row recycled, but the bus could not be read for the next step: the stop has
    # no row, and the response says where the roll stopped instead of reading complete.
    detail = "bus unreachable while reading the fresh capacity of the backend roll: ConnectionError: Error 111"
    outcome = ProfileApplyOutcome(
        hot=[],
        recycle=RecycleReport(
            rows=[RecycleRow(name="backend-1", kind="backend", generation_before=1, status=RECYCLED, keys=["TAI_X"])],
            stopped=RecycleStop(kind="backend", name=None, detail=detail),
        ),
        decision=_decision(_identity("serve-applier", 7), writer_needs=True, reported=("backend-1",)),
        fleet=FleetResult(op="reload_config", results=[]),
    )
    assert outcome.self_exit_armed is False
    response = profile_apply_response(outcome)
    assert all(entry["status"] != SELF_DEFERRED for entry in response["recycle"])
    assert response["recycle_stopped"] == {"kind": "backend", "name": None, "detail": detail}


# ---------------------------------------------------------------------------
# Expected membership is pinned to op start, not to publish time
# ---------------------------------------------------------------------------


async def test_expected_membership_is_censused_before_the_swap(monkeypatch: pytest.MonkeyPatch) -> None:
    # The swap IS this door's local apply and it is the slowest one there is (a full
    # epoch rebuild), so it is the widest window in which a sibling's presence can fade.
    # The step-5 broadcast censuses only when it publishes, so membership is read at
    # step 1 — before the swap — and handed to that publish.
    store = FakeConfigStore(env={"TAI_BUS_HEARTBEAT_TTL": "5"})
    bus = FakeBus(origin="serve-applier", remotes=["serve-b"])
    sibling = next(row for row in bus.rows if row.name == "serve-b")

    async def fading_swap(env: dict[str, str], *, ownership: DeploymentOwnership) -> Epoch:
        sibling.pttl_ms = 0
        return Epoch(number=0)

    orch, _seen = _orchestrate_spy()
    await _service(store, bus, orchestrate=orch).apply_replace_env(
        {"TAI_BUS_HEARTBEAT_TTL": "7"},
        save_previous=_PrevSpy(),
        build_and_swap=fading_swap,
    )

    assert sibling.pttl_ms == 0
    assert bus.expected_at_start_calls == [{"serve-b": 1}]


# ---------------------------------------------------------------------------
# A roll that stops is reported in the outcome, never raised past the pipeline
# ---------------------------------------------------------------------------


def _stopped(status: str, detail: str) -> RecycleReport:
    return RecycleReport(
        rows=[
            RecycleRow(name="serve-b", kind="serve", generation_before=1, status=RECYCLED),
            RecycleRow(name="backend-1", kind="backend", generation_before=2, status=status, detail=detail),
        ],
        stopped=RecycleStop(kind="backend", name="backend-1", detail=detail),
    )


@pytest.mark.parametrize(
    ("status", "detail", "make_error"),
    [
        pytest.param(
            TIMED_OUT,
            "old life still present",
            lambda report: RecycleTimeoutError("backend-1", "old life still present", report),
            id="timed-out",
        ),
        pytest.param(
            FAILED,
            "RuntimeError: boom",
            lambda report: RecycleError("recycle: worker 'backend-1' did not apply the recycle op (boom)", report),
            id="failed",
        ),
    ],
)
async def test_a_roll_that_stops_returns_the_partial_report(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    status: str,
    detail: str,
    make_error: Callable[[RecycleReport], RecycleError],
) -> None:
    _writer_moved(monkeypatch)
    store = FakeConfigStore(env={"TAI_BUS_HEARTBEAT_TTL": "5"})
    bus = _MovedBus(origin="serve-applier", remotes=["serve-b"])
    spy, _ = _swap_spy()
    partial = _stopped(status, detail)
    orch, _seen = _orchestrate_spy(raises=make_error(partial))
    with caplog.at_level(logging.ERROR):
        outcome = await _service(store, bus, orchestrate=orch).apply_replace_env(
            {"TAI_BUS_HEARTBEAT_TTL": "7"},
            save_previous=_PrevSpy(),
            build_and_swap=spy,
        )
    assert outcome.recycle is partial
    assert outcome.decision is not None
    assert outcome.decision.writer_needs is True
    assert outcome.self_exit_armed is False
    # The env persisted before the roll ran: the apply landed.
    assert store.env == {"TAI_BUS_HEARTBEAT_TTL": "7"}
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and "did not converge" in r.getMessage()]
    assert len(errors) == 1
    assert "backend-1" in errors[0].getMessage()
    assert status in errors[0].getMessage()


async def test_a_stop_without_a_row_for_the_target_escapes(monkeypatch: pytest.MonkeyPatch) -> None:
    # The bus replied without naming the published target: a bus contract violation,
    # not an outcome of the roll, so it is not folded into the recycle report. It raises
    # after the env persisted, so the door raises the persisted env's error with it as the cause.
    store = FakeConfigStore(env={"TAI_BUS_HEARTBEAT_TTL": "5"})
    spy, _ = _swap_spy()
    error = RecycleError(
        "recycle: worker 'backend-1' did not apply the recycle op (no reply from the target)", RecycleReport()
    )
    orch, _seen = _orchestrate_spy(raises=error)
    _writer_moved(monkeypatch)
    with pytest.raises(FleetBroadcastError) as raised:
        await _service(store, FakeBus(origin="serve-applier"), orchestrate=orch).apply_replace_env(
            {"TAI_BUS_HEARTBEAT_TTL": "7"},
            save_previous=_PrevSpy(),
            build_and_swap=spy,
        )

    assert raised.value.__cause__ is error
    assert "no reply from the target" in str(raised.value.report.error)
    assert store.env == {"TAI_BUS_HEARTBEAT_TTL": "7"}


class _OrderedBus(FakeBus):
    """A fake bus whose census carries a backend pair beside the serve rows and records
    each census read into a shared event list."""

    def __init__(self, events: list[str]) -> None:
        super().__init__(origin="serve-applier", remotes=["serve-b"])
        now = "2026-01-01T00:00:00+00:00"
        self._events = events
        self._backends = [
            WorkerRow(
                name=name,
                kind=WorkerKind.backend,
                pid=3,
                generation=generation,
                joined_at=now,
                beat_at=now,
                state=state,
                deployment_keys=[],
                env_readers=published_env_readers().digest,
                pttl_ms=15000,
            )
            for name, generation, state in (
                ("backend-1", 2, WorkerState.ready),
                ("backend-2", 1, WorkerState.resyncing),
            )
        ]

    async def census(self) -> list[WorkerRow]:
        self._events.append("census")
        return [*await super().census(), *self._backends]


async def test_the_recycle_targets_are_pinned_before_the_swap(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeConfigStore(env={"TAI_BUS_HEARTBEAT_TTL": "5"})
    events: list[str] = []
    bus = _OrderedBus(events)

    async def swap(env: dict[str, str], *, ownership: DeploymentOwnership) -> Epoch:
        events.append("swap")
        return Epoch(number=0)

    orch, seen = _orchestrate_spy()
    await _service(store, bus, orchestrate=orch).apply_replace_env(
        {"TAI_BUS_HEARTBEAT_TTL": "7"},
        save_previous=_PrevSpy(),
        build_and_swap=swap,
    )
    # Every census row of a target kind except the applier — gap rows included.
    assert seen["expected"] == {
        "serve-b": (WorkerKind.serve, 1),
        "backend-1": (WorkerKind.backend, 2),
        "backend-2": (WorkerKind.backend, 1),
    }
    assert "census" in events
    assert events.index("census") < events.index("swap")


async def test_a_stop_with_no_target_in_hand_is_reported_not_re_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = FakeConfigStore(env={"TAI_BUS_HEARTBEAT_TTL": "5"})
    spy, _ = _swap_spy()
    detail = "bus unreachable while reading the census at the start of the backend roll: ConnectionError: Error 111"
    partial = RecycleReport(stopped=RecycleStop(kind="backend", name=None, detail=detail))
    orch, _seen = _orchestrate_spy(raises=RecycleError(f"recycle: {detail}", partial))
    _writer_moved(monkeypatch)
    with caplog.at_level(logging.ERROR):
        outcome = await _service(store, FakeBus(origin="serve-applier"), orchestrate=orch).apply_replace_env(
            {"TAI_BUS_HEARTBEAT_TTL": "7"},
            save_previous=_PrevSpy(),
            build_and_swap=spy,
        )
    assert outcome.recycle is partial
    assert outcome.self_exit_armed is False
    assert store.env == {"TAI_BUS_HEARTBEAT_TTL": "7"}
    errors = [
        r.getMessage() for r in caplog.records if r.levelno == logging.ERROR and "did not converge" in r.getMessage()
    ]
    assert len(errors) == 1
    assert "backend" in errors[0]
    assert "no target in hand" in errors[0]
    assert "bus unreachable" in errors[0]


# ---------------------------------------------------------------------------
# The operation's verdict — workers still awaiting a respawn after the apply
# ---------------------------------------------------------------------------


async def test_a_converged_profile_roll_leaves_nobody_awaiting_a_respawn(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = FakeConfigStore(env={"TAI_BUS_HEARTBEAT_TTL": "5"})
    spy, _ = _swap_spy()
    orch, _seen = _orchestrate_spy()
    with caplog.at_level(logging.ERROR):
        bus = _MovedBus(origin="serve-applier", remotes=["serve-b"])
        monkeypatch.setattr(instance.app, "_bus", bus)
        outcome = await _service(store, bus, orchestrate=orch).apply_replace_env(
            {"TAI_BUS_HEARTBEAT_TTL": "7"}, save_previous=_PrevSpy(), build_and_swap=spy
        )
    assert outcome.awaiting_respawn == []
    assert profile_apply_response(outcome)["awaiting_respawn"] == []
    assert [r for r in caplog.records if "awaiting a respawn" in r.getMessage()] == []


async def test_a_stopped_profile_roll_leaves_the_unreached_awaiting(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = FakeConfigStore(env={"TAI_BUS_HEARTBEAT_TTL": "5"})
    spy, _ = _swap_spy()
    partial = RecycleReport(
        rows=[
            RecycleRow(
                name="serve-b",
                kind="serve",
                generation_before=1,
                status=TIMED_OUT,
                keys=["TAI_BUS_HEARTBEAT_TTL"],
                detail="slow",
            )
        ],
        stopped=RecycleStop(kind="serve", name="serve-b", detail="slow"),
    )
    orch, _seen = _orchestrate_spy(raises=RecycleTimeoutError("serve-b", "slow", partial))
    bus = _MovedBus(origin="serve-applier", remotes=["serve-b", "serve-c"])
    monkeypatch.setattr(instance.app, "_bus", bus)
    with caplog.at_level(logging.ERROR):
        outcome = await _service(store, bus, orchestrate=orch).apply_replace_env(
            {"TAI_BUS_HEARTBEAT_TTL": "7"}, save_previous=_PrevSpy(), build_and_swap=spy
        )
    assert [w.name for w in outcome.awaiting_respawn] == ["serve-b", "serve-c"]
    assert profile_apply_response(outcome)["awaiting_respawn"] == [
        {"name": "serve-b", "keys": ["TAI_BUS_HEARTBEAT_TTL"]},
        {"name": "serve-c", "keys": ["TAI_BUS_HEARTBEAT_TTL"]},
    ]
    assert len([r for r in caplog.records if "awaiting a respawn" in r.getMessage()]) == 1


class _ForkChildBus(_MovedBus):
    """A forked job child's bus: a non-member acting for its member parent ``backend-1``."""

    def __init__(self) -> None:
        super().__init__(origin="backend-1/fork-7", remotes=["backend-1", "serve-1"])
        self._identity = WorkerIdentity(
            name="backend-1/fork-7", kind=WorkerKind.backend, pid=7, generation=0, member=False
        )
        self._member = WorkerIdentity(name="backend-1", kind=WorkerKind.backend, pid=1, generation=3)

    async def census(self) -> list[WorkerRow]:
        rows = await super().census()
        return [rows[1].model_copy(update={"kind": WorkerKind.backend, "generation": 3}), rows[2]]


async def test_a_profile_apply_in_a_forked_job_child_proves_the_build_swaps_nothing_and_acts_for_its_member(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A non-member job child has no serving surface: it builds the epoch and discards it (the
    # proof the env builds), swaps nothing, persists the profile, publishes with no local
    # result, and the member parent converges as a sibling.
    # The applier is that member: excluded from the roll at its generation, reported
    # self-deferred, never left awaiting a respawn.
    store = FakeConfigStore(env={"TAI_BUS_HEARTBEAT_TTL": "5"})
    spy, swapped = _swap_spy()
    discard, discarded = _discard_spy()
    orch, seen = _orchestrate_spy(
        RecycleReport(rows=[RecycleRow(name="serve-1", kind="serve", generation_before=1, status=RECYCLED)])
    )
    bus = _ForkChildBus()
    monkeypatch.setattr(instance.app, "_bus", bus)
    with caplog.at_level(logging.ERROR):
        outcome = await _service(store, bus, orchestrate=orch).apply_replace_env(
            {"TAI_BUS_HEARTBEAT_TTL": "7"}, save_previous=_PrevSpy(), build_and_swap=spy, build_and_discard=discard
        )
    # The child proves the env builds and discards the build; it swaps nothing.
    assert discarded == {"env": {"TAI_BUS_HEARTBEAT_TTL": "7"}}
    assert swapped == {}
    assert store.env == {"TAI_BUS_HEARTBEAT_TTL": "7"}
    assert [call[2] for call in bus.publish_calls] == [None]
    assert seen["decision"].writer.name == "backend-1"
    assert seen["decision"].writer.generation == 3
    assert seen["expected"] == {"serve-1": (WorkerKind.serve, 1)}
    assert outcome.decision is not None
    assert outcome.decision.writer == bus.member_identity
    assert outcome.self_exit_armed is True
    assert outcome.awaiting_respawn == []
    deferred = profile_apply_response(outcome)["recycle"][-1]
    assert deferred == {
        "name": "backend-1",
        "kind": "backend",
        "status": SELF_DEFERRED,
        "generation_before": 3,
        "keys": ["TAI_BUS_HEARTBEAT_TTL"],
        "detail": None,
    }
    assert [r for r in caplog.records if r.levelno == logging.ERROR] == []


async def test_a_failed_build_in_a_forked_job_child_leaves_the_store_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    # The env-write-LAST contract holds on the non-member path too: the child's proof build
    # fails, so nothing is persisted, nothing is broadcast and nothing rolls.
    store = FakeConfigStore(env={"TAI_BUS_HEARTBEAT_TTL": "5"})
    built: list[dict[str, str]] = []

    async def failing_discard(env: dict[str, str], *, ownership: DeploymentOwnership) -> None:
        built.append(dict(env))
        raise _BuildBoomError("the proposed env does not build")

    swap, swapped = _swap_spy()
    orch, seen = _orchestrate_spy()
    bus = _ForkChildBus()
    prev = _PrevSpy()
    with pytest.raises(_BuildBoomError):
        await _service(store, bus, orchestrate=orch).apply_replace_env(
            {"TAI_BUS_HEARTBEAT_TTL": "7"}, save_previous=prev, build_and_swap=swap, build_and_discard=failing_discard
        )
    assert built == [{"TAI_BUS_HEARTBEAT_TTL": "7"}]
    assert swapped == {}
    assert store.env == {"TAI_BUS_HEARTBEAT_TTL": "5"}
    assert store.env_writes == []
    assert bus.publish_calls == []
    assert seen == {}
    assert prev.saved == {"TAI_BUS_HEARTBEAT_TTL": "5"}


async def test_a_member_builds_and_swaps_and_never_discards(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeConfigStore(env={"TAI_BUS_HEARTBEAT_TTL": "5"})
    swap, swapped = _swap_spy()
    discard, discarded = _discard_spy()
    orch, _seen = _orchestrate_spy()
    await _service(store, FakeBus(origin="serve-applier"), orchestrate=orch).apply_replace_env(
        {"TAI_BUS_HEARTBEAT_TTL": "7"}, save_previous=_PrevSpy(), build_and_swap=swap, build_and_discard=discard
    )
    assert swapped == {"env": {"TAI_BUS_HEARTBEAT_TTL": "7"}}
    assert discarded == {}
