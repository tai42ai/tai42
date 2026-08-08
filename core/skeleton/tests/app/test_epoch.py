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
    current_epoch,
    current_epoch_or_none,
    mark_current_request_drain_exempt,
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


async def test_a_drain_exempt_stream_does_not_block_the_retire_drain() -> None:
    """A stream that exempted itself from its generation's in-flight drain (the interactions
    inbox SSE — it never completes on its own) does not hold the generation in-flight, so a
    bus-driven retire reads it idle and returns promptly, and the session-manager close still
    fires SYNCHRONOUSLY (D13a). (This test admits then exempts, so the epoch is idle by the
    retire — it pins that the exemption RELEASED the slot; it does NOT by itself distinguish a
    synchronous drain from a deferred one. That ordering — a NON-exempt request is WAITED for
    before ``aclose`` — is pinned by ``test_a_non_exempt_in_flight_request_is_drained_before_aclose``.)"""
    import asyncio

    _install_boot("boot-app")
    boot = current_epoch()
    closed: list[str] = []

    class _FakeSupervisor:
        async def aclose(self) -> None:
            closed.append("closed")

    boot.supervisor = _FakeSupervisor()  # type: ignore[assignment]
    boot.admit()  # the SSE request, admitted on this generation
    token = epoch_mod._admission.set(epoch_mod._AdmissionState(epoch=boot))
    try:
        state = epoch_mod._admission.get()
        epoch_mod.mark_current_request_drain_exempt()
    finally:
        epoch_mod._admission.reset(token)
    # Exempting released the request's in-flight slot, so the generation reads idle to the drain.
    assert state is not None
    assert state.drain_exempt is True
    assert boot.in_flight == 0

    loop = asyncio.get_running_loop()
    start = loop.time()
    await build_and_swap_epoch(
        {"K": "v"},
        rebuild=lambda: None,
        build_serving_app=_serve("new-app"),
        drain_deadline=5.0,
        drain_tolerate_driver=False,  # bus-driven (fleet sibling)
    )
    # The retire read the generation idle (the exempt slot was released) and returned promptly;
    # a still-counted in-flight request would instead be WAITED for (next test). D13a fired sync.
    assert loop.time() - start < 1.0, "the drain-exempt stream stalled the retire"
    assert closed == ["closed"], "the retire did not close the old lifespan synchronously"


async def test_a_non_exempt_in_flight_request_is_drained_before_aclose() -> None:
    """A NORMAL in-flight request (an MCP tool call / a sync REST tool run) is NOT exempt: the
    bus-driven retire DRAINS it — waits for it to finish — BEFORE ``aclose``ing the lifespan, so
    its transport is never severed mid-response. Proven by ``aclose`` running only AFTER it
    releases (the F6 "in-flight sync tool run completes on the old epoch" contract)."""
    import asyncio

    _install_boot("boot-app")
    boot = current_epoch()
    order: list[str] = []

    class _FakeSupervisor:
        async def aclose(self) -> None:
            order.append("aclose")

    boot.supervisor = _FakeSupervisor()  # type: ignore[assignment]
    boot.admit()  # a normal request, in flight and NOT exempt

    async def _finish_shortly() -> None:
        await asyncio.sleep(0.2)
        order.append("released")
        boot.release()

    finisher = asyncio.create_task(_finish_shortly())
    await build_and_swap_epoch(
        {"K": "v"},
        rebuild=lambda: None,
        build_serving_app=_serve("new-app"),
        drain_deadline=5.0,
        drain_tolerate_driver=False,
    )
    await finisher
    assert order == ["released", "aclose"], f"a non-exempt request was not drained before aclose: {order}"


async def test_admission_wrapper_releases_an_exempt_stream_exactly_once() -> None:
    """The admission wrapper must NOT release an exempt stream's slot a second time at request
    end (the exemption already released it): a double release would drop the count below the
    real in-flight work and let a retire force-close under it."""
    _install_boot("boot-app")
    boot = current_epoch()
    boot.admit()  # request A: a normal request still in flight

    async def _sse_app(scope, receive, send) -> None:
        epoch_mod.mark_current_request_drain_exempt()  # request B (the SSE) exempts itself

    await EpochAdmissionApp(_sse_app, boot)({"type": "http"}, None, None)  # type: ignore[arg-type]
    # B released its slot exactly once; the wrapper did NOT release again, so A's slot survives.
    assert boot.in_flight == 1
    boot.release()  # cleanup A


async def test_exemption_takes_effect_through_a_real_streaming_response() -> None:
    """PIN the fix against the REAL production path. The other exemption tests hand-set the
    ``_admission`` ContextVar or call the exempt in the wrapper's own frame — but in production
    the exempt call runs INSIDE a ``StreamingResponse`` body, which Starlette iterates in a
    CHILD task (uvicorn's ASGI spec_version 2.3 branch). The whole fix therefore rests on (a) the
    admission ContextVar propagating INTO that child task and (b) the shared ``_AdmissionState``
    mutation flowing back to the wrapper's frame. A future Starlette/uvicorn/middleware change
    that ran the body in a context the mark could not reach would silently no-op the exemption
    and every hand-set test would still pass — this one would fail. It drives a REAL
    ``StreamingResponse`` through ``EpochAdmissionApp`` and asserts the generation reads IDLE
    (drain returns at once) while the stream is STILL open."""
    import asyncio
    import contextlib

    from starlette.responses import StreamingResponse

    ep = Epoch(number=current_client_epoch())
    exempted = asyncio.Event()
    unblock = asyncio.Event()

    async def _body():
        yield b"data: backlog\n\n"  # a first (backlog) frame
        # Commit to the never-completing tail: exempt from the retire drain. THIS is the call
        # whose ContextVar reach + shared-object write-back the test pins on the real path.
        mark_current_request_drain_exempt()
        exempted.set()
        await unblock.wait()  # the infinite tail — held open while the assertions run
        yield b": keepalive\n\n"

    async def _app(scope, receive, send) -> None:
        await StreamingResponse(_body(), media_type="text/event-stream")(scope, receive, send)

    async def _receive():
        await asyncio.Event().wait()  # never disconnects; cancelled when the body finishes
        return {"type": "http.disconnect"}

    async def _send(_message) -> None:
        return None

    scope = {"type": "http", "method": "GET", "path": "/api/interactions/stream", "http_version": "1.1", "headers": []}
    served = asyncio.create_task(EpochAdmissionApp(_app, ep)(scope, _receive, _send))  # type: ignore[arg-type]
    try:
        await asyncio.wait_for(exempted.wait(), timeout=3.0)
        # The stream is STILL open (blocked on ``unblock``) yet the generation reads IDLE — the
        # exempt reached the epoch THROUGH THE REAL CHILD-TASK PATH. A no-op exempt (ContextVar
        # not propagating) would leave ``in_flight == 1`` and this would fail.
        assert ep.in_flight == 0, "the exemption did not reach the epoch through the real streaming path"
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await ep._drain_in_flight(5.0)  # returns at once (idle), not after the 5s budget
        assert loop.time() - t0 < 1.0, "the retire drain waited on the exempt stream"
    finally:
        unblock.set()
        served.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await served


async def test_cancel_periodic_loops_bounded_by_budget() -> None:
    """A periodic loop whose cancel-await WEDGES (e.g. an outbound-HTTP poll blocking on
    its client close during the cancellation unwind) must NOT hang the synchronous retire
    — and with it a door-driven reload's own request. Each cancel is bounded by the drain
    budget; an overrun is abandoned so the retire makes progress."""
    import asyncio

    ep = Epoch(number=current_client_epoch())

    async def _wedged_cancel() -> None:
        await asyncio.Event().wait()  # never set — a cancel-await that never completes

    ep.register_periodic_loop(_wedged_cancel)
    loop = asyncio.get_running_loop()
    start = loop.time()
    # Bounded: returns after the budget, abandoning the wedged cancel, rather than
    # wedging the retire forever.
    await ep._cancel_periodic_loops(0.05)
    assert loop.time() - start < 1.0, "a wedged periodic-loop cancel wedged the retire"


async def test_reload_over_session_manager_defers_supervisor_close() -> None:
    """A door-driven reload — its own request still admitted on the retiring epoch, e.g. an
    MCP tool call served by that epoch's FastMCP session manager — must NOT ``aclose`` the
    old lifespan while that request is in flight: doing so tears down the transport
    delivering the tool's own response and the client hangs. ``build_and_swap`` must return
    promptly (the drain + close are DEFERRED, not awaited), the old session manager must
    stay live while the driver is in flight, and the deferred task closes it only once the
    driver's request drains (its response flushed)."""
    import asyncio

    _install_boot("boot-app")
    boot = current_epoch()
    closed: list[str] = []

    class _FakeSupervisor:
        async def aclose(self) -> None:
            closed.append("closed")

    boot.supervisor = _FakeSupervisor()  # type: ignore[assignment]
    boot.admit()  # the driving MCP tool call, still admitted on the epoch being retired
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
        # Returned promptly (drain + close deferred, not awaited on the driving request)...
        assert loop.time() - start < 1.0, "reload self-waited the drain budget on its own driving request"
        # ...and did NOT close the session manager serving the still-in-flight driver.
        assert closed == [], "reload aclose()d the session manager serving its own driving request"
        # The driver finishes (its response flushed) -> the deferred task drains + closes.
        boot.release()
        async with asyncio.timeout(2.0):
            while closed != ["closed"]:
                await asyncio.sleep(0.01)
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


# -- accessors -----------------------------------------------------------------


def test_current_epoch_raises_before_boot() -> None:
    assert current_epoch_or_none() is None
    with pytest.raises(RuntimeError, match="no serving epoch is installed"):
        current_epoch()


async def test_is_epoch_rebuild_in_progress_true_only_during_a_build() -> None:
    """The MCP viability probe reads this to pick the SHORT reload budget during a rebuild and
    the generous cold-boot one otherwise: it is True only while ``build_and_swap_epoch`` is
    populating the new generation, and False at steady serve (and after the swap)."""
    _install_boot("boot-app")
    assert epoch_mod.is_epoch_rebuild_in_progress() is False  # steady serve, no build

    seen: list[bool] = []

    def _rebuild() -> None:
        seen.append(epoch_mod.is_epoch_rebuild_in_progress())

    await build_and_swap_epoch({"K": "v"}, rebuild=_rebuild, build_serving_app=_serve("new-app"))
    assert seen == [True]  # True while the build populated the new generation
    assert epoch_mod.is_epoch_rebuild_in_progress() is False  # cleared once the swap completed


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
