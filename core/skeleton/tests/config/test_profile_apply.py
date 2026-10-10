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

import json
import logging
import os
import threading
from collections.abc import Awaitable, Callable, Iterator
from typing import Any, cast

import pytest
from tai42_kit.fork_gate import fork_gate

# ``bus_settings`` is imported for its registration side effect — the apply's recycle
# classification reflects only IMPORTED settings classes, and TAI_BUS_* are recycle-class.
import tai42_skeleton.app.bus_settings  # noqa: F401
from tai42_skeleton.app import epoch as epoch_mod
from tai42_skeleton.app import instance
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
)
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.config.service import ConfigService, ProfileApplyOutcome
from tai42_skeleton.operations._broadcast import SELF_DEFERRED, profile_apply_response

from .._fakes.bus import FakeBus
from .fake_pipeline import FakeConfigStore, FakeReloadAdmin

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _service(store: FakeConfigStore, bus: FakeBus | None = None) -> ConfigService:
    return ConfigService(config_manager=store, admin=FakeReloadAdmin(), bus=cast("Any", bus or FakeBus()))


class _PrevSpy:
    """Records the stored env handed to the ``@previous`` snapshot callback."""

    def __init__(self) -> None:
        self.saved: dict[str, str] | None = None

    async def __call__(self, stored_env: dict[str, str]) -> None:
        self.saved = dict(stored_env)


def _swap_spy() -> tuple[Callable[..., Awaitable[Epoch]], dict[str, Any]]:
    """A ``build_and_swap`` seam that records its call and swaps nothing."""
    calls: dict[str, Any] = {}

    async def spy(env: dict[str, str], *, drain_tolerate_driver: bool) -> Epoch:
        calls["env"] = dict(env)
        calls["driven"] = drain_tolerate_driver
        return Epoch(number=0)

    return spy, calls


def _orchestrate_spy(
    report: RecycleReport | None = None,
    *,
    raises: RecycleError | None = None,
) -> tuple[Callable[..., Awaitable[RecycleReport]], dict[str, Any]]:
    """An ``orchestrate`` seam that records its kwargs and returns a crafted report, or
    raises ``raises`` (the roll stopping)."""
    seen: dict[str, Any] = {}

    async def spy(
        bus: Any,
        *,
        excluded_name: str,
        applier_generation: int,
        target_kinds: Any,
        applier_self_deferred: bool,
        step_timeout: float,
        expected: Any = None,
    ) -> RecycleReport:
        seen["expected"] = None if expected is None else dict(expected)
        seen["excluded_name"] = excluded_name
        seen["applier_generation"] = applier_generation
        seen["target_kinds"] = list(target_kinds)
        seen["applier_self_deferred"] = applier_self_deferred
        seen["step_timeout"] = step_timeout
        if raises is not None:
            raise raises
        out = report or RecycleReport(
            rows=[RecycleRow(name="serve-b", kind="serve", generation_before=1, status=RECYCLED)],
            fresh=[FreshLife(name="serve-b", kind="serve", generation=2)],
        )
        return out

    return spy, seen


# ---------------------------------------------------------------------------
# Ordering / failure — env-write-LAST
# ---------------------------------------------------------------------------


@pytest.fixture
def _epoch_state() -> Iterator[None]:
    """Reset the epoch spine globals so a REAL ``build_and_swap_epoch`` failure path runs
    in isolation; the suite-wide ``_preserve_generation_globals`` fixture restores the
    per-generation registries the build stages."""
    loaded_before = set(epoch_mod._loaded_env_keys)
    try:
        yield
    finally:
        for name in ("_current", "_serving_slot", "_building_epoch"):
            setattr(epoch_mod, name, None)
        epoch_mod._loaded_env_keys = loaded_before


class _BuildBoomError(RuntimeError):
    pass


@pytest.mark.usefixtures("_epoch_state")
async def test_failed_build_leaves_store_untouched_and_restores_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAI_SUPERVISED", raising=False)
    store = FakeConfigStore(env={"APP_KEY": "stored", "OLD_ONLY": "keep"})
    prev = _PrevSpy()
    env_before = dict(os.environ)

    async def _fail_serve(_epoch: Epoch) -> Any:
        raise _BuildBoomError("deliberate build failure under the proposed env")

    async def _noop() -> None:
        return None

    async def real_build(env: dict[str, str], *, drain_tolerate_driver: bool) -> Epoch:
        # Drive the REAL primitive with a failing build_serving_app seam so its
        # os.environ restore-on-failure + staged-abort actually run.
        return await build_and_swap_epoch(
            env,
            rebuild=lambda: None,
            build_serving_app=_fail_serve,
            establish_background_loops=_noop,
            drain_tolerate_driver=drain_tolerate_driver,
        )

    with pytest.raises(_BuildBoomError):
        await _service(store).apply_replace_env(
            {"APP_KEY": "proposed", "NEW_KEY": "x"},
            driven=True,
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

    monkeypatch.delenv("TAI_SUPERVISED", raising=False)
    for key in _DATABASE_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    stored = {"TAI_DATABASE_DEFAULT_PG_PASSWORD": "pw", "TAI_DATABASE_DEFAULT_PG_DB": "first"}
    (tmp_path / ".env").write_text("".join(f"{key}={value}\n" for key, value in stored.items()))
    store = FakeConfigStore(env=dict(stored))
    assert states_store_settings().pg_db == "first"

    seen: dict[str, str | None] = {}

    def _read_during_build() -> None:
        seen["during"] = states_store_settings().pg_db

    async def _fail_serve(_epoch: Epoch) -> Any:
        raise _BuildBoomError("deliberate build failure after the settings read")

    async def _noop() -> None:
        return None

    async def real_build(env: dict[str, str], *, drain_tolerate_driver: bool) -> Epoch:
        return await build_and_swap_epoch(
            env,
            rebuild=_read_during_build,
            build_serving_app=_fail_serve,
            establish_background_loops=_noop,
            drain_tolerate_driver=drain_tolerate_driver,
        )

    with pytest.raises(_BuildBoomError):
        await _service(store).apply_replace_env(
            {**stored, "TAI_DATABASE_DEFAULT_PG_DB": "second"},
            driven=True,
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


async def test_apply_passes_drain_tolerate_driver_from_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAI_SUPERVISED", raising=False)  # bare — a hot-only diff is fine on bare
    store = FakeConfigStore(env={"MY_APP_FLAG": "old"})
    spy, calls = _swap_spy()
    outcome = await _service(store).apply_replace_env(
        {"MY_APP_FLAG": "new"}, driven=True, save_previous=_PrevSpy(), build_and_swap=spy
    )
    # A door-driven apply MUST excuse its own admitted request from the retire drain.
    assert calls["driven"] is True
    assert calls["env"] == {"MY_APP_FLAG": "new"}
    # env-write-LAST landed after a successful build.
    assert store.env == {"MY_APP_FLAG": "new"}
    # A hot-only diff neither recycles nor self-exits.
    assert outcome.hot == ["MY_APP_FLAG"]
    assert outcome.recycle is None
    assert outcome.serve_affecting is False


async def test_apply_releases_llm_pools_before_the_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """The apply MUST close the loop-bound checkpoint/store pools BEFORE the build's
    settings reset drops their per-loop registries — the build's reset refuses to drop a
    registry still holding live resources on a running loop (an apply following any LLM run
    otherwise 500s). Mirrors AppLifecycle._reload_config, the other build_and_swap_epoch
    caller."""
    monkeypatch.delenv("TAI_SUPERVISED", raising=False)  # bare — a hot-only diff is fine on bare
    store = FakeConfigStore(env={"MY_APP_FLAG": "old"})
    order: list[str] = []

    async def _release() -> None:
        order.append("release")

    async def _build(env: dict[str, str], *, drain_tolerate_driver: bool) -> Epoch:
        order.append("build")
        return Epoch(number=0)

    await _service(store).apply_replace_env(
        {"MY_APP_FLAG": "new"},
        driven=True,
        save_previous=_PrevSpy(),
        build_and_swap=_build,
        release_llm_pools=_release,
    )
    # Release strictly precedes the build (the ordering the kit reset contract requires).
    assert order == ["release", "build"]


def test_apply_releases_through_the_kit_call_by_default() -> None:
    import inspect

    from tai42_kit.llm import release_loop_bound_resources

    default = inspect.signature(ConfigService.apply_replace_env).parameters["release_llm_pools"].default
    assert default is release_loop_bound_resources


async def test_apply_closes_the_store_when_the_checkpoint_close_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing checkpoint close never skips the store close; the apply fails loudly before the build."""
    from tai42_kit.llm.checkpoint.checkpoint_registry import checkpoint_registry
    from tai42_kit.llm.store.store_registry import store_registry

    monkeypatch.delenv("TAI_SUPERVISED", raising=False)
    closed: list[str] = []

    async def _failing_close() -> None:
        closed.append("checkpoint")
        raise RuntimeError("checkpoint close failed")

    async def _store_close() -> None:
        closed.append("store")

    async def _checkpoint_resource():
        return object(), _failing_close

    async def _store_resource():
        return object(), _store_close

    await checkpoint_registry()._get_or_init_resource("k", _checkpoint_resource)
    await store_registry()._get_or_init_resource("k", _store_resource)
    built: list[bool] = []

    async def _build(env: dict[str, str], *, drain_tolerate_driver: bool) -> Epoch:
        built.append(True)
        return Epoch(number=0)

    store = FakeConfigStore(env={"MY_APP_FLAG": "old"})
    with pytest.raises(ExceptionGroup, match="errors releasing loop-bound kit resources"):
        await _service(store).apply_replace_env(
            {"MY_APP_FLAG": "new"}, driven=True, save_previous=_PrevSpy(), build_and_swap=_build
        )
    assert sorted(closed) == ["checkpoint", "store"]
    assert built == []
    assert store.env == {"MY_APP_FLAG": "old"}


async def test_apply_holds_the_fork_gate_across_the_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """This door drives ``build_and_swap`` directly instead of through
    ``reload_gate.run``, so it must take the fork gate itself: its rebuild re-imports the
    manifest modules, and a backend work loop forking a job child mid-reimport leaves the
    child deadlocked on an inherited ``importlib`` per-module lock."""
    monkeypatch.delenv("TAI_SUPERVISED", raising=False)  # bare — a hot-only diff is fine on bare
    store = FakeConfigStore(env={"MY_APP_FLAG": "old"})
    seen: dict[str, Any] = {}

    async def _build(env: dict[str, str], *, drain_tolerate_driver: bool) -> Epoch:
        seen["blocked"] = fork_gate.blocked
        seen["owner"] = fork_gate._owner
        return Epoch(number=0)

    assert not fork_gate.blocked
    await _service(store).apply_replace_env(
        {"MY_APP_FLAG": "new"}, driven=True, save_previous=_PrevSpy(), build_and_swap=_build
    )
    assert seen["blocked"] is True
    # Ownership sits on the façade's dedicated holder thread, never the serving loop's.
    assert seen["owner"] is not None
    assert seen["owner"] != threading.get_ident()
    assert not fork_gate.blocked


async def test_apply_releases_the_fork_gate_when_the_build_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed apply must not leave the gate held — that would block every subsequent
    job child for the process lifetime."""
    monkeypatch.delenv("TAI_SUPERVISED", raising=False)
    store = FakeConfigStore(env={"MY_APP_FLAG": "old"})

    async def _build(env: dict[str, str], *, drain_tolerate_driver: bool) -> Epoch:
        raise _BuildBoomError("deliberate build failure")

    with pytest.raises(_BuildBoomError):
        await _service(store).apply_replace_env(
            {"MY_APP_FLAG": "new"}, driven=True, save_previous=_PrevSpy(), build_and_swap=_build
        )
    assert not fork_gate.blocked
    assert not reload_gate.locked


# ---------------------------------------------------------------------------
# Serve-affecting recycle — orchestrate rolls, applier self-defers
# ---------------------------------------------------------------------------


async def test_recycle_class_diff_orchestrates_and_flags_serve_affecting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_SUPERVISED", "harness")  # recycle-supported; TIER-1-only refusals
    store = FakeConfigStore(env={"TAI_BUS_NAMESPACE": "old"})
    bus = FakeBus(origin="serve-applier", remotes=["serve-b"])
    spy, _ = _swap_spy()
    orch, seen = _orchestrate_spy()
    outcome = await _service(store, bus).apply_replace_env(
        {"TAI_BUS_NAMESPACE": "new"},
        driven=True,
        save_previous=_PrevSpy(),
        build_and_swap=spy,
        orchestrate=orch,
    )
    # TAI_BUS_NAMESPACE is recycle-class → a recycle rolls, excluding the applier, and
    # the conservative default treats it as serve-affecting.
    assert outcome.serve_affecting is True
    assert seen["excluded_name"] == "serve-applier"
    assert seen["applier_generation"] == 1
    assert seen["applier_self_deferred"] is True
    assert [k.value for k in seen["target_kinds"]] == ["backend", "serve"]
    assert outcome.recycle is not None
    assert [row.name for row in outcome.recycle.rows] == ["serve-b"]
    assert store.env == {"TAI_BUS_NAMESPACE": "new"}


# ---------------------------------------------------------------------------
# Refused-keys branches — each aborts upfront, naming the key, nothing persisted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("shape", "key"),
    [
        ("compose", "STORAGE_S3_ENDPOINT"),  # declared pinned on compose (Tier-2)
        ("k8s", "SUB_MCP_REDIS_URL"),  # declared pinned on k8s (Tier-2)
        ("harness", "TAI_BUS_REDIS_URL"),  # bus-reaching (Tier-1, every shape)
    ],
)
async def test_apply_refuses_pinned_key_upfront(monkeypatch: pytest.MonkeyPatch, shape: str, key: str) -> None:
    monkeypatch.setenv("TAI_SUPERVISED", shape)
    if shape != "harness":
        # The supervised deployment declares the keys it pins.
        monkeypatch.setenv("TAI_SUPERVISED_PINNED_KEYS", f'["{key}"]')
    store = FakeConfigStore(env={})
    spy, calls = _swap_spy()
    prev = _PrevSpy()
    with pytest.raises(ValueError, match=key):
        await _service(store).apply_replace_env(
            {key: "some-value"}, driven=True, save_previous=prev, build_and_swap=spy
        )
    # Aborted BEFORE the census / @previous snapshot / build / persist.
    assert store.env_writes == []
    assert prev.saved is None
    assert "env" not in calls


async def test_apply_refuses_recycle_class_diff_on_bare_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAI_SUPERVISED", raising=False)  # bare — no supervisor to recycle
    store = FakeConfigStore(env={"TAI_BUS_NAMESPACE": "old"})
    spy, calls = _swap_spy()
    with pytest.raises(ValueError, match="TAI_BUS_NAMESPACE"):
        await _service(store).apply_replace_env(
            {"TAI_BUS_NAMESPACE": "new"}, driven=True, save_previous=_PrevSpy(), build_and_swap=spy
        )
    assert store.env_writes == []
    assert "env" not in calls


# ---------------------------------------------------------------------------
# NAMES-ONLY — a sentinel secret VALUE never reaches the report
# ---------------------------------------------------------------------------


async def test_apply_report_carries_names_never_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAI_SUPERVISED", raising=False)
    sentinel = "s3cr3t-sentinel-VALUE-must-not-leak"
    store = FakeConfigStore(env={"APP_SECRET": "old"})
    bus = FakeBus(origin="serve-applier")
    # ``fleet_fanout`` reads the process bus origin to decide local-only vs fleet; drive
    # the pipeline through that same bus, installed as ``instance.app.bus``.
    monkeypatch.setattr(instance.app, "_bus", bus)
    spy, _ = _swap_spy()
    outcome = await _service(store, bus).apply_replace_env(
        {"APP_SECRET": sentinel}, driven=True, save_previous=_PrevSpy(), build_and_swap=spy
    )
    response = profile_apply_response(outcome)
    blob = json.dumps(response)
    assert sentinel not in blob  # the VALUE never rides the report
    assert "APP_SECRET" in blob  # the NAME does (a hot diff)
    # The whole serialized outcome (report material) is names-only too.
    material = json.dumps(
        {
            "hot": outcome.hot,
            "self_identity": outcome.self_identity.model_dump(),
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


def test_profile_apply_response_shape() -> None:
    outcome = ProfileApplyOutcome(
        hot=["HOT_A", "HOT_B"],
        recycle=RecycleReport(
            rows=[
                RecycleRow(name="serve-b", kind="serve", generation_before=1, status=RECYCLED),
                RecycleRow(name="backend-z", kind="backend", generation_before=4, status=RECYCLED),
            ],
            fresh=[FreshLife(name="serve-b", kind="serve", generation=2)],
        ),
        self_identity=_identity("serve-applier", 7),
        serve_affecting=True,
        fleet=FleetResult(op="reload_config", results=[]),
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
        "detail": None,
    } in response["recycle"]
    assert {
        "name": "backend-z",
        "kind": "backend",
        "status": "recycled",
        "generation_before": 4,
        "detail": None,
    } in response["recycle"]
    # The per-kind fresh list surfaces the new lives — informational, never a successor.
    assert response["fresh"] == [{"name": "serve-b", "kind": "serve", "generation": 2}]
    # The applier's OWN line — always kind "serve", carrying its own current generation.
    applier = [entry for entry in response["recycle"] if entry["status"] == SELF_DEFERRED]
    assert applier == [
        {"name": "serve-applier", "kind": "serve", "status": SELF_DEFERRED, "generation_before": 7, "detail": None}
    ]
    # No generation_after / names-only replacements anywhere in the surfaced report.
    blob = json.dumps(response)
    assert "generation_after" not in blob
    assert "replacements" not in blob
    assert "fanout" in response
    assert response["recycle_stopped"] is None


def test_profile_apply_response_omits_applier_when_not_serve_affecting() -> None:
    outcome = ProfileApplyOutcome(
        hot=["HOT_A"],
        recycle=None,
        self_identity=_identity("serve-applier", 1),
        serve_affecting=False,
        fleet=FleetResult(op="reload_config", results=[]),
    )
    response = profile_apply_response(outcome)
    assert response["recycle"] == []  # no applier line, no siblings
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
    )
    outcome = ProfileApplyOutcome(
        hot=[],
        recycle=RecycleReport(
            rows=[RecycleRow(name=row.name, kind=row.kind.value, generation_before=row.generation, status=RECYCLED)],
            fresh=[],
        ),
        self_identity=bus.identity,
        serve_affecting=False,
        fleet=FleetResult(op="reload_config", results=[]),
    )
    (entry,) = profile_apply_response(outcome)["recycle"]
    assert entry == {
        "name": "backend-1",
        "kind": "backend",
        "status": "recycled",
        "generation_before": 1,
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
                RecycleRow(name="backend-1", kind="backend", generation_before=1, status=RECYCLED),
                RecycleRow(name="backend-2", kind="backend", generation_before=3, status=TIMED_OUT, detail=detail),
            ],
            stopped=RecycleStop(kind="backend", name="backend-2", detail=detail),
        ),
        self_identity=_identity("serve-applier", 7),
        serve_affecting=True,
        fleet=FleetResult(op="reload_config", results=[]),
    )
    assert outcome.self_exit_armed is False
    response = profile_apply_response(outcome)
    assert response["recycle"] == [
        {"name": "backend-1", "kind": "backend", "status": "recycled", "generation_before": 1, "detail": None},
        {"name": "backend-2", "kind": "backend", "status": "timed-out", "generation_before": 3, "detail": detail},
    ]
    assert response["recycle_stopped"] == {"kind": "backend", "name": "backend-2", "detail": detail}


def test_a_roll_stopped_with_no_target_in_hand_yields_the_stop_and_no_self_deferred_entry() -> None:
    # Every row recycled, but the bus could not be read for the next step: the stop has
    # no row, and the response says where the roll stopped instead of reading complete.
    detail = "bus unreachable while reading the fresh capacity of the backend roll: ConnectionError: Error 111"
    outcome = ProfileApplyOutcome(
        hot=[],
        recycle=RecycleReport(
            rows=[RecycleRow(name="backend-1", kind="backend", generation_before=1, status=RECYCLED)],
            stopped=RecycleStop(kind="backend", name=None, detail=detail),
        ),
        self_identity=_identity("serve-applier", 7),
        serve_affecting=True,
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
    monkeypatch.setenv("TAI_SUPERVISED", "harness")
    store = FakeConfigStore(env={"TAI_BUS_NAMESPACE": "old"})
    bus = FakeBus(origin="serve-applier", remotes=["serve-b"])
    sibling = next(row for row in bus.rows if row.name == "serve-b")

    async def fading_swap(env: dict[str, str], *, drain_tolerate_driver: bool) -> Epoch:
        sibling.pttl_ms = 0
        return Epoch(number=0)

    orch, _seen = _orchestrate_spy()
    await _service(store, bus).apply_replace_env(
        {"TAI_BUS_NAMESPACE": "new"},
        driven=True,
        save_previous=_PrevSpy(),
        build_and_swap=fading_swap,
        orchestrate=orch,
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
    monkeypatch.setenv("TAI_SUPERVISED", "harness")
    store = FakeConfigStore(env={"TAI_BUS_NAMESPACE": "old"})
    bus = FakeBus(origin="serve-applier", remotes=["serve-b"])
    spy, _ = _swap_spy()
    partial = _stopped(status, detail)
    orch, _seen = _orchestrate_spy(raises=make_error(partial))
    with caplog.at_level(logging.ERROR):
        outcome = await _service(store, bus).apply_replace_env(
            {"TAI_BUS_NAMESPACE": "new"},
            driven=True,
            save_previous=_PrevSpy(),
            build_and_swap=spy,
            orchestrate=orch,
        )
    assert outcome.recycle is partial
    assert outcome.serve_affecting is True
    assert outcome.self_exit_armed is False
    # The env persisted before the roll ran: the apply landed.
    assert store.env == {"TAI_BUS_NAMESPACE": "new"}
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and "recycle" in r.getMessage()]
    assert len(errors) == 1
    assert "backend-1" in errors[0].getMessage()
    assert status in errors[0].getMessage()


async def test_a_stop_without_a_row_for_the_target_escapes(monkeypatch: pytest.MonkeyPatch) -> None:
    # The bus replied without naming the published target: a bus contract violation,
    # not an outcome of the roll, so it is not folded into the report.
    monkeypatch.setenv("TAI_SUPERVISED", "harness")
    store = FakeConfigStore(env={"TAI_BUS_NAMESPACE": "old"})
    spy, _ = _swap_spy()
    error = RecycleError(
        "recycle: worker 'backend-1' did not apply the recycle op (no reply from the target)", RecycleReport()
    )
    orch, _seen = _orchestrate_spy(raises=error)
    with pytest.raises(RecycleError, match="no reply from the target"):
        await _service(store, FakeBus(origin="serve-applier")).apply_replace_env(
            {"TAI_BUS_NAMESPACE": "new"},
            driven=True,
            save_previous=_PrevSpy(),
            build_and_swap=spy,
            orchestrate=orch,
        )


def test_self_exit_is_armed_only_on_a_converged_serve_affecting_roll() -> None:
    def outcome(recycle: RecycleReport | None, *, serve_affecting: bool) -> ProfileApplyOutcome:
        return ProfileApplyOutcome(
            hot=[],
            recycle=recycle,
            self_identity=_identity("serve-applier", 1),
            serve_affecting=serve_affecting,
            fleet=FleetResult(op="reload_config", results=[]),
        )

    converged = RecycleReport(rows=[RecycleRow(name="b", kind="backend", generation_before=1, status=RECYCLED)])
    assert outcome(converged, serve_affecting=True).self_exit_armed is True
    assert outcome(converged, serve_affecting=False).self_exit_armed is False
    assert outcome(_stopped(TIMED_OUT, "x"), serve_affecting=True).self_exit_armed is False
    assert outcome(None, serve_affecting=False).self_exit_armed is False


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
    monkeypatch.setenv("TAI_SUPERVISED", "harness")
    store = FakeConfigStore(env={"TAI_BUS_NAMESPACE": "old"})
    events: list[str] = []
    bus = _OrderedBus(events)

    async def swap(env: dict[str, str], *, drain_tolerate_driver: bool) -> Epoch:
        events.append("swap")
        return Epoch(number=0)

    orch, seen = _orchestrate_spy()
    await _service(store, bus).apply_replace_env(
        {"TAI_BUS_NAMESPACE": "new"},
        driven=True,
        save_previous=_PrevSpy(),
        build_and_swap=swap,
        orchestrate=orch,
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
    monkeypatch.setenv("TAI_SUPERVISED", "harness")
    store = FakeConfigStore(env={"TAI_BUS_NAMESPACE": "old"})
    spy, _ = _swap_spy()
    detail = "bus unreachable while reading the census at the start of the backend roll: ConnectionError: Error 111"
    partial = RecycleReport(stopped=RecycleStop(kind="backend", name=None, detail=detail))
    orch, _seen = _orchestrate_spy(raises=RecycleError(f"recycle: {detail}", partial))
    with caplog.at_level(logging.ERROR):
        outcome = await _service(store, FakeBus(origin="serve-applier")).apply_replace_env(
            {"TAI_BUS_NAMESPACE": "new"},
            driven=True,
            save_previous=_PrevSpy(),
            build_and_swap=spy,
            orchestrate=orch,
        )
    assert outcome.recycle is partial
    assert outcome.self_exit_armed is False
    assert store.env == {"TAI_BUS_NAMESPACE": "new"}
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR and "recycle" in r.getMessage()]
    assert len(errors) == 1
    assert "backend" in errors[0]
    assert "no target in hand" in errors[0]
    assert "bus unreachable" in errors[0]
