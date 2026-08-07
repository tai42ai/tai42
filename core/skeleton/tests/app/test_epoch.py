"""The per-epoch serving surface + the build/swap primitive a profile apply calls.

These drive the epoch spine in isolation through its injectable ``rebuild`` /
``build_serving_app`` seams, so the build/swap/discard contract is asserted
without booting a full server. The production defaults wire the same seams to the
running app.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from tai42_contract.app import tai42_app
from tai42_kit.clients.base import current_client_epoch
from tai42_kit.settings.cache_registry import (
    _stamp_settings,
    settings_cache,
    sweep_stale_settings,
)

from tai42_skeleton.app import epoch as epoch_mod
from tai42_skeleton.app.epoch import (
    Epoch,
    EpochAdmissionApp,
    build_and_swap_epoch,
    capture_census_snapshot,
    current_epoch,
    current_epoch_or_none,
)
from tai42_skeleton.app.instance import build_app
from tai42_skeleton.app.lifecycle import TaiMCPLifecycleMixin

# Bind the process app handle so the retire path's ``tool_runs`` import (which
# registers an ``on_shutdown`` handler on the bound handle) resolves.
tai42_app.bind(build_app())


class _Serving:
    """A distinct ASGI-callable stand-in for a built serving app — identity + name
    let a test assert which generation the dispatch slot points at."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def __call__(self, scope, receive, send) -> None:  # pragma: no cover
        raise AssertionError("sentinel serving app is never dispatched in these tests")


def _serve(name: str):
    """An async ``build_serving_app`` seam returning a named sentinel serving app."""

    async def _build(_epoch: Epoch) -> _Serving:
        return _Serving(name)

    return _build


@pytest.fixture(autouse=True)
def _reset_epoch_state() -> Iterator[None]:
    """Reset the process serving generation between tests. The client epoch is a
    monotonic process counter (never reset), so assertions compare relative flips.

    These tests drive ``build_and_swap_epoch`` with INJECTED no-op ``rebuild`` seams,
    so its ``begin/commit_staging_all`` would promote an EMPTY staged generation and
    wipe the committed generation registries for later suites — snapshot and restore
    the committed generation globals around each test."""
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
    for name in ("_current", "_serving_slot", "_retiring_epoch"):
        setattr(epoch_mod, name, None)
    epoch_mod._loaded_env_keys = set()
    try:
        yield
    finally:
        for name in ("_current", "_serving_slot", "_retiring_epoch", "_building_epoch"):
            setattr(epoch_mod, name, None)
        epoch_mod._loaded_env_keys = set()
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


def _install_boot(name: str = "boot-app") -> dict:
    """Install a boot epoch over a fake dispatch slot and return that slot."""
    app_state: dict = {}
    serving = _Serving(name)
    epoch_mod._current = Epoch(number=current_client_epoch(), serving_app=serving)
    epoch_mod._serving_slot = app_state
    app_state["app"] = serving
    return app_state


# -- side-effect-free build ---------------------------------------------------


async def test_failed_build_keeps_old_surface_and_restores_env_and_raises_loudly() -> None:
    app_state = _install_boot("boot-app")
    boot_epoch = current_epoch()
    os.environ["TAI_EPOCH_TEST_EXISTING"] = "old"
    env_before = dict(os.environ)

    def _rebuild() -> None:
        # By now the primitive has applied the proposed env; the failure must leave
        # zero live-state mutation once it unwinds.
        assert os.environ["TAI_EPOCH_TEST_KEY"] == "proposed"
        raise RuntimeError("deliberate build failure")

    with pytest.raises(RuntimeError, match="deliberate build failure"):
        await build_and_swap_epoch(
            {"TAI_EPOCH_TEST_KEY": "proposed"},
            rebuild=_rebuild,
            build_serving_app=_serve("should-not-build"),
        )

    # Old surface still serving: the slot + the current-epoch pointer are untouched.
    assert app_state["app"].name == "boot-app"
    assert current_epoch() is boot_epoch
    # os.environ restored EXACTLY, including the key the proposed env introduced.
    assert dict(os.environ) == env_before
    assert "TAI_EPOCH_TEST_KEY" not in os.environ

    del os.environ["TAI_EPOCH_TEST_EXISTING"]


async def test_failed_build_does_not_advance_the_live_epoch_pointer() -> None:
    _install_boot("boot-app")
    boot_number = current_epoch().number

    with pytest.raises(ValueError, match="boom"):
        await build_and_swap_epoch(
            {"K": "v"},
            rebuild=lambda: (_ for _ in ()).throw(ValueError("boom")),
            build_serving_app=_serve("x"),
        )

    assert current_epoch().number == boot_number


# -- successful build + swap --------------------------------------------------


async def test_successful_build_flips_slot_and_epoch_pointer() -> None:
    app_state = _install_boot("boot-app")
    boot_epoch = current_epoch()

    new = await build_and_swap_epoch(
        {"TAI_EPOCH_LIVE": "yes"},
        rebuild=lambda: None,
        build_serving_app=_serve("new-app"),
    )

    assert current_epoch() is new
    assert current_epoch() is not boot_epoch
    assert app_state["app"].name == "new-app"
    # The new generation's number advanced past the boot generation.
    assert new.number == boot_epoch.number + 1


async def test_env_applied_on_build_stays_live_on_success() -> None:
    _install_boot("boot-app")
    os.environ.pop("TAI_EPOCH_STAYS", None)

    await build_and_swap_epoch(
        {"TAI_EPOCH_STAYS": "live-value"},
        rebuild=lambda: None,
        build_serving_app=_serve("new-app"),
    )

    assert os.environ["TAI_EPOCH_STAYS"] == "live-value"
    del os.environ["TAI_EPOCH_STAYS"]


async def test_removed_key_reconciled_across_builds() -> None:
    _install_boot("boot-app")
    os.environ.pop("TAI_EPOCH_DROPPED", None)

    await build_and_swap_epoch(
        {"TAI_EPOCH_DROPPED": "first"},
        rebuild=lambda: None,
        build_serving_app=_serve("a"),
    )
    assert os.environ["TAI_EPOCH_DROPPED"] == "first"

    # A second build whose proposed env omits the previously-loaded key drops it.
    await build_and_swap_epoch(
        {"TAI_EPOCH_OTHER": "second"},
        rebuild=lambda: None,
        build_serving_app=_serve("b"),
    )
    assert "TAI_EPOCH_DROPPED" not in os.environ
    assert os.environ["TAI_EPOCH_OTHER"] == "second"
    del os.environ["TAI_EPOCH_OTHER"]


# -- per-epoch drain -----------------------------------------------------------


async def test_retire_cancels_periodic_loops_and_calls_drain_epoch(monkeypatch: pytest.MonkeyPatch) -> None:
    app_state = _install_boot("boot-app")
    boot_epoch = current_epoch()

    cancelled: list[str] = []

    async def _cancel_reprobe() -> None:
        cancelled.append("reprobe")

    async def _cancel_advisories() -> None:
        cancelled.append("advisories")

    boot_epoch.register_periodic_loop(_cancel_reprobe)
    boot_epoch.register_periodic_loop(_cancel_advisories)

    drained_epochs: list[int] = []

    async def _fake_drain_epoch(number: int, deadline: float) -> None:
        drained_epochs.append(number)

    drained_supervisors: list[float] = []

    async def _fake_drain_supervisors(
        deadline: float | None = None, *, epoch: int | None = None, reason: str = ""
    ) -> None:
        drained_supervisors.append(deadline if deadline is not None else -1.0)

    monkeypatch.setattr(epoch_mod, "drain_epoch", _fake_drain_epoch)
    monkeypatch.setattr("tai42_skeleton.operations.tool_runs.drain_supervisors", _fake_drain_supervisors)

    retired_number = boot_epoch.number
    await build_and_swap_epoch(
        {"K": "v"},
        rebuild=lambda: None,
        build_serving_app=_serve("new-app"),
        drain_deadline=0.01,
    )

    assert app_state["app"].name == "new-app"
    assert set(cancelled) == {"reprobe", "advisories"}
    assert drained_epochs == [retired_number]
    assert drained_supervisors == [0.01]


async def test_admission_counter_tracks_in_flight() -> None:
    ep = Epoch(number=current_client_epoch())
    assert ep.in_flight == 0

    served: list[str] = []

    async def _inner(scope, receive, send) -> None:
        # An HTTP request counts one in-flight mid-dispatch; a non-HTTP scope passes
        # through uncounted.
        assert ep.in_flight == (1 if scope["type"] == "http" else 0)
        served.append(scope["type"])

    wrapper = EpochAdmissionApp(_inner, ep)  # type: ignore[arg-type]
    await wrapper({"type": "http"}, None, None)  # type: ignore[arg-type]
    assert ep.in_flight == 0
    assert served == ["http"]

    # A non-HTTP scope passes through uncounted.
    await wrapper({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
    assert ep.in_flight == 0


async def test_drain_in_flight_bounded_by_budget() -> None:
    ep = Epoch(number=current_client_epoch())
    ep.admit()  # a request that never releases within the budget
    assert ep.in_flight == 1
    # Bounded: returns after the budget rather than blocking forever.
    await ep._drain_in_flight(0.01)
    assert ep.in_flight == 1


async def test_drain_in_flight_tolerates_the_driving_request() -> None:
    import asyncio

    ep = Epoch(number=current_client_epoch())
    ep.admit()  # the request DRIVING the reload — stays admitted across the swap
    assert ep.in_flight == 1
    # residual=1 excuses that single driver and returns PROMPTLY — it must NOT self-wait
    # the (generous) budget for the request that can only release once the reload returns.
    loop = asyncio.get_running_loop()
    start = loop.time()
    await ep._drain_in_flight(5.0, residual=1)
    assert loop.time() - start < 1.0, "residual-tolerant drain self-waited the budget on its own driver"
    assert ep.in_flight == 1


async def test_reload_driven_by_request_does_not_self_wait() -> None:
    """A door-driven reload (its own request still admitted on the retiring epoch) must
    NOT self-wait the drain budget for that request — the regression that made every
    door reload take ~10s. With the driving-request signal set, ``build_and_swap_epoch``
    excuses exactly that one admitted request and returns near-instantly."""
    import asyncio

    _install_boot("boot-app")
    boot = current_epoch()
    boot.admit()  # the driving request, admitted on the epoch about to be retired
    token = epoch_mod._reload_driven_by_request.set(True)
    loop = asyncio.get_running_loop()
    try:
        start = loop.time()
        await build_and_swap_epoch(
            {"TAI_EPOCH_DRIVER": "v"},
            rebuild=lambda: None,
            build_serving_app=_serve("new-app"),
            drain_deadline=5.0,
            drain_tolerate_driver=True,
        )
        assert loop.time() - start < 1.0, "reload self-waited the drain budget on its own driving request"
    finally:
        epoch_mod._reload_driven_by_request.reset(token)
        os.environ.pop("TAI_EPOCH_DRIVER", None)


# -- stateful-session termination at swap --------------------------------------


async def test_retire_closes_the_previous_generations_serving_lifespan() -> None:
    """The swap retires the old generation by ``aclose()``-ing its FastMCP lifespan
    supervisor, which terminates every live transport — no handoff."""
    app_state = _install_boot("boot-app")
    boot_epoch = current_epoch()

    closed: list[str] = []

    class _FakeSupervisor:
        async def aclose(self) -> None:
            closed.append("closed")

    boot_epoch.supervisor = _FakeSupervisor()  # type: ignore[assignment]

    await build_and_swap_epoch(
        {"K": "v"},
        rebuild=lambda: None,
        build_serving_app=_serve("new-app"),
        drain_deadline=0.01,
    )

    # The retired generation's serving lifespan was closed (its transports terminated).
    assert closed == ["closed"]
    assert app_state["app"].name == "new-app"


# -- stale-settings sweep ------------------------------------------------------


async def test_sweep_hook_registered_and_returns_zero_after_clean_cycle() -> None:
    _install_boot("boot-app")

    # The retire's settings reset sweeps the retired generation through the
    # registered hook; a clean cycle (no holder pins a retired-epoch settings
    # instance) reports zero.
    retired_number = current_epoch().number
    await build_and_swap_epoch(
        {"K": "v"},
        rebuild=lambda: None,
        build_serving_app=_serve("new-app"),
    )
    assert sweep_stale_settings(retired_number) == []


def test_sweep_hook_flags_a_leaked_retired_settings_instance() -> None:
    from pydantic_settings import BaseSettings

    class _Leaky(BaseSettings):
        pass

    # Stamp an instance under the current epoch, then advance so it is retired.
    leaked = _Leaky()
    _stamp_settings(leaked)
    retired = current_client_epoch()
    from tai42_kit.clients.base import advance_client_epoch

    advance_client_epoch()
    # A still-reachable retired-epoch instance is reported (never dropped).
    stale = sweep_stale_settings(retired)
    assert any(h.settings_type.endswith("_Leaky") for h in stale)


# -- unified per-epoch handler list --------------------------------------------


def test_epoch_handlers_dedup_and_preserve_order() -> None:
    def schema_gate() -> None: ...
    def seed_roles() -> None: ...
    def rehydrate_presets() -> None: ...
    def rehydrate_sub_mcp() -> None: ...
    def reset_reserved_paths() -> None: ...

    class _App:
        def __init__(self) -> None:
            # Presets registered before sub-MCP on startup (the order that must
            # hold), both also on reload; reset_reserved_paths is reload-only.
            self._startup_handlers = {
                "schema_gate": schema_gate,
                "seed_roles": seed_roles,
                "rehydrate_presets": rehydrate_presets,
                "rehydrate_sub_mcp": rehydrate_sub_mcp,
            }
            self._reload_handlers = {
                "reset_reserved_paths": reset_reserved_paths,
                "rehydrate_presets": rehydrate_presets,
                "rehydrate_sub_mcp": rehydrate_sub_mcp,
            }

    handlers = TaiMCPLifecycleMixin._epoch_handlers(_App())  # type: ignore[arg-type]

    # Each handler once, startup order preserved (presets before sub-MCP), the
    # reload-only handler appended after.
    assert handlers == [schema_gate, seed_roles, rehydrate_presets, rehydrate_sub_mcp, reset_reserved_paths]


# -- census snapshot -----------------------------------------------------------


async def test_capture_census_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Origin:
        def __init__(self, origin: str) -> None:
            self.origin = origin

    class _Bus:
        async def census(self):
            return [_Origin("serve-a"), _Origin("backend-b")]

    class _App:
        bus = _Bus()

    monkeypatch.setattr("tai42_skeleton.app.instance.app", _App())
    snapshot = await capture_census_snapshot()
    assert snapshot == frozenset({"serve-a", "backend-b"})


# -- accessors -----------------------------------------------------------------


def test_current_epoch_raises_before_boot() -> None:
    assert current_epoch_or_none() is None
    with pytest.raises(RuntimeError, match="no serving epoch is installed"):
        current_epoch()


def test_settings_cache_is_used_by_the_sweep_roster() -> None:
    # A sanity anchor that the sweep roster the epoch retire depends on is populated
    # by the standard cached-accessor path.
    calls: list[int] = []

    @settings_cache
    def _accessor():
        calls.append(1)
        from pydantic_settings import BaseSettings

        return BaseSettings()

    _accessor()
    assert calls == [1]
