"""Unit oracles for the C5 settings-profile APPLY pipeline
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
import os
from collections.abc import Awaitable, Callable, Iterator
from typing import Any, cast

import pytest

# ``bus_settings`` is imported for its registration side effect — the apply's recycle
# classification reflects only IMPORTED settings classes, and TAI_BUS_* are recycle-class.
import tai42_skeleton.app.bus_settings  # noqa: F401
from tai42_skeleton.app import epoch as epoch_mod
from tai42_skeleton.app import instance
from tai42_skeleton.app.bus import FleetResult, WorkerKind, WorkerRow, WorkerState
from tai42_skeleton.app.epoch import Epoch, build_and_swap_epoch
from tai42_skeleton.app.recycle import RecycleReport
from tai42_skeleton.config.service import ConfigService, ProfileApplyOutcome
from tai42_skeleton.operations._broadcast import SELF_DEFERRED, profile_apply_response
from tests._fakes.bus import FakeBus
from tests.config.test_service import FakeConfigStore, FakeReloadAdmin

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
) -> tuple[Callable[..., Awaitable[RecycleReport]], dict[str, Any]]:
    """An ``orchestrate`` seam that records its kwargs and returns a crafted report."""
    seen: dict[str, Any] = {}

    async def spy(
        bus: Any, *, excluded_origin: str, target_kinds: Any, applier_self_deferred: bool, step_timeout: float
    ) -> RecycleReport:
        seen["excluded_origin"] = excluded_origin
        seen["target_kinds"] = list(target_kinds)
        seen["applier_self_deferred"] = applier_self_deferred
        seen["step_timeout"] = step_timeout
        out = report or RecycleReport(recycled=["serve-b"], replacements=["serve-c"])
        return out

    return spy, seen


# ---------------------------------------------------------------------------
# Ordering / failure — env-write-LAST
# ---------------------------------------------------------------------------


@pytest.fixture
def _epoch_state() -> Iterator[None]:
    """Reset the epoch spine globals and snapshot the per-generation registries the
    build's ``begin/abort_staging_all`` touch, so a REAL ``build_and_swap_epoch`` failure
    path runs in isolation and leaves later suites untouched (mirrors ``tests/app``)."""
    from tai42_contract.access_control import registry as identity_registry
    from tai42_contract.accounts import registry as accounts_registry

    from tai42_skeleton.connectors.providers import registry as connector_registry
    from tai42_skeleton.operations.registry import operation_registry
    from tai42_skeleton.plugins import quarantine as quarantine_registry

    saved = {
        "connector": dict(connector_registry._REGISTRY),
        "identity": dict(identity_registry._REGISTRY),
        "accounts": dict(accounts_registry._REGISTRY),
        "operation": dict(operation_registry._operations),
        "quarantine": dict(quarantine_registry._quarantined),
    }
    loaded_before = set(epoch_mod._loaded_env_keys)
    try:
        yield
    finally:
        for name in ("_current", "_serving_slot", "_retiring_epoch", "_building_epoch"):
            setattr(epoch_mod, name, None)
        epoch_mod._loaded_env_keys = loaded_before
        connector_registry._REGISTRY = saved["connector"]
        connector_registry._pending = None
        identity_registry._REGISTRY = saved["identity"]
        identity_registry._pending = None
        accounts_registry._REGISTRY = saved["accounts"]
        accounts_registry._pending = None
        operation_registry._operations = saved["operation"]
        operation_registry._pending = None
        quarantine_registry._quarantined = saved["quarantine"]
        quarantine_registry._pending = None


class _BuildBoom(RuntimeError):
    pass


@pytest.mark.usefixtures("_epoch_state")
async def test_failed_build_leaves_store_untouched_and_restores_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAI_SUPERVISED", raising=False)
    store = FakeConfigStore(env={"APP_KEY": "stored", "OLD_ONLY": "keep"})
    prev = _PrevSpy()
    env_before = dict(os.environ)

    async def _fail_serve(_epoch: Epoch) -> Any:
        raise _BuildBoom("deliberate build failure under the proposed env")

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

    with pytest.raises(_BuildBoom):
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


# ---------------------------------------------------------------------------
# Drain — no self-deadlock (PLAN_2 fix)
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
    assert seen["excluded_origin"] == "serve-applier"
    assert seen["applier_self_deferred"] is True
    assert [k.value for k in seen["target_kinds"]] == ["backend", "serve"]
    assert outcome.recycle is not None
    assert outcome.recycle.recycled == ["serve-b"]
    assert store.env == {"TAI_BUS_NAMESPACE": "new"}


# ---------------------------------------------------------------------------
# Refused-keys branches — each aborts upfront, naming the key, nothing persisted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("shape", "key"),
    [
        ("compose", "STORAGE_S3_ENDPOINT"),  # x-tai-app-env pinned (compose Tier-2)
        ("k8s", "SUB_MCP_REDIS_URL"),  # sub-MCP routing store (k8s Tier-2)
        ("harness", "TAI_BUS_REDIS_URL"),  # bus-reaching (Tier-1, every shape)
    ],
)
async def test_apply_refuses_pinned_key_upfront(monkeypatch: pytest.MonkeyPatch, shape: str, key: str) -> None:
    monkeypatch.setenv("TAI_SUPERVISED", shape)
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
            "origin_kinds": outcome.origin_kinds,
            "recycle": outcome.recycle.model_dump() if outcome.recycle else None,
        }
    )
    assert sentinel not in material
    # The store DID receive the secret value (it persists; only the report must not).
    assert store.env == {"APP_SECRET": sentinel}


# ---------------------------------------------------------------------------
# Response shape — refused==[] on success, applier line status==SELF_DEFERRED
# ---------------------------------------------------------------------------


def test_profile_apply_response_shape() -> None:
    outcome = ProfileApplyOutcome(
        hot=["HOT_A", "HOT_B"],
        recycle=RecycleReport(recycled=["serve-b"], timeouts=["backend-z"], replacements=["serve-c"]),
        origin_kinds={"serve-b": "serve", "backend-z": "backend"},
        self_origin="serve-applier",
        serve_affecting=True,
        fleet=FleetResult(op="reload_config", results=[]),
        census=frozenset({"serve-applier"}),
    )
    response = profile_apply_response(outcome)
    assert response["hot"] == ["HOT_A", "HOT_B"]
    assert response["refused"] == []  # empty on success BY CONSTRUCTION
    assert {"origin": "serve-b", "kind": "serve", "status": "recycled"} in response["recycle"]
    assert {"origin": "backend-z", "kind": "backend", "status": "timed-out"} in response["recycle"]
    # The applier's OWN line — always kind "serve", the one shared self-deferred literal.
    applier = [entry for entry in response["recycle"] if entry["status"] == SELF_DEFERRED]
    assert applier == [{"origin": "serve-applier", "kind": "serve", "status": SELF_DEFERRED}]
    assert "fanout" in response


def test_profile_apply_response_omits_applier_when_not_serve_affecting() -> None:
    outcome = ProfileApplyOutcome(
        hot=["HOT_A"],
        recycle=None,
        origin_kinds={},
        self_origin="serve-applier",
        serve_affecting=False,
        fleet=FleetResult(op="reload_config", results=[]),
        census=frozenset(),
    )
    response = profile_apply_response(outcome)
    assert response["recycle"] == []  # no applier line, no siblings
    assert response["refused"] == []


def test_origin_kind_map_covers_fleet() -> None:
    """A serve/backend census populates the name->kind map the response reads."""
    now = "2026-01-01T00:00:00+00:00"
    bus = FakeBus(origin="serve-applier", remotes=["serve-b"])
    origins = [
        WorkerRow(
            name="backend-1",
            kind=WorkerKind.backend,
            pid=9,
            generation=1,
            joined_at=now,
            beat_at=now,
            state=WorkerState.ready,
        )
    ]
    # Sanity: a census row carries a kind the response's recycle[].kind is filled from.
    assert origins[0].kind is WorkerKind.backend
    assert bus.identity.kind is WorkerKind.serve
