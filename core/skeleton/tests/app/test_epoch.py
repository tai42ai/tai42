"""The per-epoch serving surface + the build/swap primitive a profile apply calls.

These drive the epoch spine in isolation through its injectable ``rebuild`` /
``build_serving_app`` seams, so the build/swap/discard contract is asserted
without booting a full server. The production defaults wire the same seams to the
running app.
"""

from __future__ import annotations

import asyncio
import contextvars
import gc
import os
from collections.abc import Awaitable, Callable, Iterator
from contextvars import ContextVar

import pytest
from pydantic_settings import BaseSettings
from tai42_contract.app import tai42_app
from tai42_kit.clients.base import advance_client_epoch, current_client_epoch
from tai42_kit.settings import reset_all_settings
from tai42_kit.settings.cache_registry import (
    _stamp_settings,
    settings_cache,
    sweep_stale_settings,
)

from tai42_skeleton.app import admission as admission_mod
from tai42_skeleton.app import epoch as epoch_mod
from tai42_skeleton.app import epoch_retire as epoch_retire_mod
from tai42_skeleton.app import retired_generations as retired_mod
from tai42_skeleton.app.admission import EpochAdmissionApp, mark_current_request_drain_exempt
from tai42_skeleton.app.epoch import (
    Epoch,
    build_and_swap_epoch,
    current_epoch,
    current_epoch_or_none,
)
from tai42_skeleton.app.instance import build_app
from tai42_skeleton.app.lifecycle import TaiMCPLifecycleMixin
from tai42_skeleton.app.reload_gate import hold_recorded, reload_gate

from ._served_app import RELOAD_SWAP_BOUND_S

# Bind the process app handle so the retire path's ``tool_runs`` import (which
# registers an ``on_shutdown`` handler on the bound handle) resolves.
tai42_app.bind(build_app())


class _Serving:
    """A distinct ASGI-callable stand-in for a built serving app — identity + name
    let a test assert which generation the dispatch slot points at. ``core`` is the
    generation core it serves, as a real serving surface reaches its FastMCP."""

    def __init__(self, name: str, core: object | None = None) -> None:
        self.name = name
        self.core = core

    async def __call__(self, scope, receive, send) -> None:  # pragma: no cover
        raise AssertionError("sentinel serving app is never dispatched in these tests")


def _serve(name: str):
    """An async ``build_serving_app`` seam returning a named sentinel serving app."""

    async def _build(_epoch: Epoch) -> _Serving:
        return _Serving(name)

    return _build


def _time_drains(monkeypatch: pytest.MonkeyPatch, epoch: Epoch) -> list[float]:
    """Record how long each in-flight drain of ``epoch`` waits, in seconds.

    The drain is timed apart from the rest of the retire (settings reset, pool drain), whose
    duration depends on the host: a drain that waits on a request runs out its whole budget,
    one that finds the generation idle returns at once.
    """
    durations: list[float] = []
    drain = epoch._drain_in_flight

    async def _timed(deadline: float, **kwargs: bool) -> bool:
        loop = asyncio.get_running_loop()
        start = loop.time()
        try:
            return await drain(deadline, **kwargs)
        finally:
            durations.append(loop.time() - start)

    monkeypatch.setattr(epoch, "_drain_in_flight", _timed)
    return durations


@pytest.fixture(autouse=True)
def _reset_epoch_state() -> Iterator[None]:
    """Reset the process serving generation between tests. The client epoch is a
    monotonic process counter (never reset), so assertions compare relative flips.

    These tests drive ``build_and_swap_epoch`` with INJECTED no-op ``rebuild`` seams, so
    a successful build promotes EMPTY generations over the per-generation registries;
    the suite-wide ``_preserve_generation_globals`` fixture restores them after each test."""
    for name in ("_current", "_serving_slot"):
        setattr(epoch_mod, name, None)
    epoch_mod._loaded_env_keys = set()
    try:
        yield
    finally:
        for name in ("_current", "_serving_slot", "_building_epoch"):
            setattr(epoch_mod, name, None)
        epoch_mod._loaded_env_keys = set()
        retired_mod.detach_retired_generations()


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


async def test_route_audit_failure_in_rebuild_keeps_old_epoch_serving(monkeypatch: pytest.MonkeyPatch) -> None:
    # A rebuild whose route re-import dropped a plugin still declared in the manifest
    # fails the epoch build LOUDLY through the route audit, so the atomic swap never
    # installs a route-dropping epoch — the previous epoch keeps serving.
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response

    from tai42_skeleton.app import registry_staging
    from tai42_skeleton.app.route_registry import EpochRouteAuditError, RouteOwner, RouteRegistry

    registry = RouteRegistry()
    monkeypatch.setattr(registry_staging, "route_registry", registry)
    owner = RouteOwner(kind="plugin", owner_ref="acme/one", item_name="web")

    async def _plugin_handler(request: Request) -> Response:
        return JSONResponse({"data": {}})

    # The plugin's route serves in the live (committed) generation.
    registry.record(
        path="/api/acme/one/ping",
        methods=["GET"],
        name=None,
        handler=_plugin_handler,
        summary="s",
        tags=["t"],
        authed=False,
        request_model=None,
        response_model=None,
        no_body_reason="test fixture: response body not under test",
        owner=owner,
        public=True,
    )

    app_state = _install_boot("boot-app")
    boot_epoch = current_epoch()

    def _rebuild() -> None:
        # The re-import left the plugin's route module cached, so the staged generation
        # dropped its route while the manifest still declares it — the audit raises.
        registry.reset_shape_index()
        registry.audit_plugin_routes_preserved({owner})

    with pytest.raises(EpochRouteAuditError, match="acme/one:web"):
        await build_and_swap_epoch(
            {"K": "v"},
            rebuild=_rebuild,
            build_serving_app=_serve("should-not-build"),
        )

    # Old surface still serving: the slot and the current-epoch pointer are untouched.
    assert app_state["app"].name == "boot-app"
    assert current_epoch() is boot_epoch


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
        deadline: float | None = None, *, epoch: int | None = None, reason: str = "", exclude: object = ()
    ) -> None:
        drained_supervisors.append(deadline if deadline is not None else -1.0)

    monkeypatch.setattr(epoch_retire_mod, "drain_epoch", _fake_drain_epoch)
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


async def test_a_retire_whose_three_drains_each_use_most_of_the_budget_fits_the_drain_floor(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The retire's legal spend is three sequential drains, each bounded by the drain budget.

    An in-flight request, the supervisors' terminal writes and the retired pool's leases each
    finish at 0.8 of the budget: every drain finishes legally, in sequence, and the whole retire
    fits ``retire_drain_floor_seconds(budget)`` with no overrun logged. A drain bounded tighter
    than the budget would log an overrun; a fourth drain would overrun the floor.
    """
    import logging

    from tai42_skeleton.app.epoch_retire import retire_drain_floor_seconds

    budget = 1.0
    step = 0.8 * budget
    _install_boot("boot-app")
    boot = current_epoch()
    boot.admit()  # an ordinary in-flight request of the retiring generation

    async def _finish_the_request() -> None:
        await asyncio.sleep(step)
        boot.release()

    finishers: list[asyncio.Task[None]] = []
    drain_in_flight = boot._drain_in_flight

    async def _request_finishes_inside_its_drain(deadline: float, **kwargs: bool) -> bool:
        # The request runs 0.8 of the budget from the moment its drain starts waiting.
        if not finishers:
            finishers.append(asyncio.create_task(_finish_the_request()))
        return await drain_in_flight(deadline, **kwargs)

    monkeypatch.setattr(boot, "_drain_in_flight", _request_finishes_inside_its_drain)

    async def _supervisors_write_their_terminals(
        deadline: float | None = None, *, epoch: int | None = None, reason: str = "", exclude: object = ()
    ) -> None:
        assert deadline == budget
        await asyncio.sleep(step)

    async def _pool_leases_return(number: int, deadline: float) -> None:
        assert deadline == budget
        await asyncio.sleep(step)

    monkeypatch.setattr(epoch_retire_mod, "drain_epoch", _pool_leases_return)
    monkeypatch.setattr("tai42_skeleton.operations.tool_runs.drain_supervisors", _supervisors_write_their_terminals)

    retire_durations: list[float] = []
    retire = epoch_mod.retire_epoch

    async def _timed_retire(old: Epoch, retired: int, deadline: float | None) -> None:
        loop = asyncio.get_running_loop()
        start = loop.time()
        try:
            await retire(old, retired, deadline)
        finally:
            retire_durations.append(loop.time() - start)

    monkeypatch.setattr(epoch_mod, "retire_epoch", _timed_retire)

    with caplog.at_level(logging.WARNING):
        await build_and_swap_epoch(
            {"K": "v"},
            rebuild=lambda: None,
            build_serving_app=_serve("new-app"),
            drain_deadline=budget,
        )
    await asyncio.gather(*finishers)

    assert len(retire_durations) == 1
    assert retire_durations[0] >= 3 * step, "the three drains did not run in sequence"
    assert retire_durations[0] <= retire_drain_floor_seconds(budget)
    assert retire_drain_floor_seconds(budget) == 3 * budget
    assert boot.in_flight == 0
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []


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


async def test_a_drain_exempt_stream_does_not_block_the_retire_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stream that exempted itself from its generation's in-flight drain (the interactions
    inbox SSE — it never completes on its own) does not hold the generation in-flight, so a
    bus-driven retire reads it idle and returns promptly, and the session-manager close still
    fires SYNCHRONOUSLY. (This test admits then exempts, so the epoch is idle by the
    retire — it pins that the exemption RELEASED the slot; it does NOT by itself distinguish a
    synchronous drain from a deferred one. That ordering — a NON-exempt request is WAITED for
    before ``aclose`` — is pinned by ``test_a_non_exempt_in_flight_request_is_drained_before_aclose``.)"""
    _install_boot("boot-app")
    boot = current_epoch()
    closed: list[str] = []

    class _FakeSupervisor:
        async def aclose(self) -> None:
            closed.append("closed")

    boot.supervisor = _FakeSupervisor()  # type: ignore[assignment]
    boot.admit()  # the SSE request, admitted on this generation
    token = admission_mod._admission.set(admission_mod.RequestAdmission(epoch=boot))
    try:
        state = admission_mod._admission.get()
        admission_mod.mark_current_request_drain_exempt()
    finally:
        admission_mod._admission.reset(token)
    # Exempting released the request's in-flight slot, so the generation reads idle to the drain.
    assert state is not None
    assert state.slot_released is True
    assert boot.in_flight == 0

    drains = _time_drains(monkeypatch, boot)
    await build_and_swap_epoch(
        {"K": "v"},
        rebuild=lambda: None,
        build_serving_app=_serve("new-app"),
        drain_deadline=5.0,
    )
    # The retire's drain read the generation idle (the exempt slot was released) and returned
    # well inside its budget; a still-counted in-flight request would instead be WAITED for
    # until the budget ran out (next test). The session-manager close fired sync.
    assert len(drains) == 1
    assert drains[0] < 5.0, "the drain-exempt stream stalled the retire's drain"
    assert closed == ["closed"], "the retire did not close the old lifespan synchronously"


async def test_a_non_exempt_in_flight_request_is_drained_before_aclose() -> None:
    """A NORMAL in-flight request (an MCP tool call / a sync REST tool run) is NOT exempt: the
    bus-driven retire DRAINS it — waits for it to finish — BEFORE ``aclose``ing the lifespan, so
    its transport is never severed mid-response. Proven by ``aclose`` running only AFTER it
    releases (the "in-flight sync tool run completes on the old epoch" contract)."""
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
        admission_mod.mark_current_request_drain_exempt()  # request B (the SSE) exempts itself

    await EpochAdmissionApp(_sse_app, boot)({"type": "http"}, None, None)  # type: ignore[arg-type]
    # B released its slot exactly once; the wrapper did NOT release again, so A's slot survives.
    assert boot.in_flight == 1
    boot.release()  # cleanup A


async def test_exemption_takes_effect_through_a_real_streaming_response() -> None:
    """PIN the fix against the REAL production path. The other exemption tests hand-set the
    ``_admission`` ContextVar or call the exempt in the wrapper's own frame — but in production
    the exempt call runs INSIDE a ``StreamingResponse`` body, which Starlette iterates in a
    CHILD task (uvicorn's ASGI spec_version 2.3 branch). The whole fix therefore rests on (a) the
    admission ContextVar propagating INTO that child task and (b) the shared ``RequestAdmission``
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


def _served_request(
    epoch: Epoch,
    body: Callable[[], Awaitable[None]],
    *,
    context: contextvars.Context | None = None,
    surface: object | None = None,
) -> asyncio.Task[None]:
    """Serve ``body`` as one HTTP request admitted on ``epoch`` by the real admission wrapper.

    ``surface`` is the serving surface the request's scope points at (``scope["app"]``).
    """

    async def _app(scope, receive, send) -> None:
        await body()

    scope = {"type": "http"} if surface is None else {"type": "http", "app": surface}
    served = EpochAdmissionApp(_app, epoch)(scope, None, None)  # type: ignore[arg-type]
    return asyncio.get_running_loop().create_task(served, context=context)


class _ClosingSupervisor:
    """A stand-in serving lifespan recording its close into ``log``."""

    def __init__(self, log: list[str]) -> None:
        self.log = log

    async def aclose(self) -> None:
        self.log.append("closed")


# The drain budget of the reload that defers its retire to a background tail.
_DEFERRED_DRAIN_BUDGET_S = 5.0
# How long the deferred tail may take once its driver answered: twice the drain budget, the one
# phase of the tail that can wait (the gate turnover, the wait on the finished driver and the
# close do not). The tail's slowest run measures 5.8 s with the host at load 200; the bound
# leaves a margin for a loaded CI runner. A tail that never ends (a driver or gate waiter never
# released) fails here.
_DEFERRED_TAIL_BOUND_S = 2 * _DEFERRED_DRAIN_BUDGET_S


async def test_reload_over_session_manager_defers_supervisor_close(monkeypatch: pytest.MonkeyPatch) -> None:
    """A door-driven reload — its own request still admitted on the retiring epoch, e.g. an
    MCP tool call served by that epoch's FastMCP session manager — must NOT ``aclose`` the
    old lifespan while that request is in flight: doing so tears down the transport
    delivering the tool's own response and the client hangs. ``build_and_swap`` must return
    promptly (the drain + close are DEFERRED, not awaited), the old session manager must
    stay live while the driver is in flight, and the deferred task closes it only once the
    driver's request has finished (its response flushed)."""
    _install_boot("boot-app")
    boot = current_epoch()
    closed: list[str] = []
    boot.supervisor = _ClosingSupervisor(closed)  # type: ignore[assignment]
    swapped = asyncio.Event()
    answer = asyncio.Event()
    drains = _time_drains(monkeypatch, boot)

    async def _driver() -> None:
        async with reload_gate.driving():
            await build_and_swap_epoch(
                {"TAI_EPOCH_DRIVER": "v"},
                rebuild=lambda: None,
                build_serving_app=_serve("new-app"),
                drain_deadline=_DEFERRED_DRAIN_BUDGET_S,
            )
        swapped.set()
        await answer.wait()

    request = _served_request(boot, _driver)
    try:
        swap = asyncio.ensure_future(swapped.wait())
        await asyncio.wait({swap, request}, timeout=RELOAD_SWAP_BOUND_S, return_when=asyncio.FIRST_COMPLETED)
        swap.cancel()
        assert swapped.is_set(), f"the driven reload did not swap: {request}"
        # Returned with the drain + close deferred to a task still running, not awaited on the
        # driving request...
        [deferred] = epoch_retire_mod._deferred_retire_tasks
        assert not deferred.done(), "reload awaited the drain on its own driving request"
        # ...and did NOT close the session manager serving the still-in-flight driver.
        assert closed == [], "reload aclose()d the session manager serving its own driving request"
        answer.set()  # the driver finishes (its response flushed) -> the deferred task closes
        try:
            async with asyncio.timeout(_DEFERRED_TAIL_BOUND_S):
                await request
                await deferred
        except TimeoutError:
            pytest.fail(
                f"the deferred retire did not finish within {_DEFERRED_TAIL_BOUND_S} s of the driver answering: "
                f"in flight {boot.in_flight}, engaged {boot.engaged}, gate waiters {boot.gate_waiters}"
            )
        assert closed == ["closed"]
        assert drains
        assert max(drains) < _DEFERRED_DRAIN_BUDGET_S, "the deferred drain ran out its budget"
    finally:
        answer.set()
        os.environ.pop("TAI_EPOCH_DRIVER", None)


async def test_a_driver_answering_past_the_drain_budget_keeps_its_transport(caplog: pytest.LogCaptureFixture) -> None:
    """The driving request answers long after the drain budget (a fleet reload waits for its
    siblings' replies after its own swap): its session manager stays open until it has
    answered, and the drain never reports it as a straggler."""
    import logging

    _install_boot("boot-app")
    boot = current_epoch()
    log: list[str] = []
    boot.supervisor = _ClosingSupervisor(log)  # type: ignore[assignment]
    budget = 0.05

    async def _driver() -> None:
        async with reload_gate.driving():
            await build_and_swap_epoch(
                {"TAI_EPOCH_DRIVER": "v"},
                rebuild=lambda: None,
                build_serving_app=_serve("new-app"),
                drain_deadline=budget,
            )
        await asyncio.sleep(budget * 10)  # the fleet wait, ten drain budgets long
        log.append("answered")

    try:
        with caplog.at_level(logging.ERROR):
            await _served_request(boot, _driver)
            async with asyncio.timeout(2.0):
                await asyncio.gather(*epoch_retire_mod._deferred_retire_tasks)
    finally:
        os.environ.pop("TAI_EPOCH_DRIVER", None)
    assert log == ["answered", "closed"], f"the retire closed the transport under its driver: {log}"
    assert "still in flight after the drain budget" not in caplog.text


async def test_a_request_waiting_on_the_gate_is_never_severed() -> None:
    """A door request of the retiring generation that waits on the gate while a bus-delivered
    reload holds it (it passed the gate's entry check just before) is not severed by that
    reload's retire: the retire defers its close, lets the gate turn over, and the waiter
    runs its own gated body and answers before the transports close."""
    _install_boot("boot-app")
    boot = current_epoch()
    log: list[str] = []
    boot.supervisor = _ClosingSupervisor(log)  # type: ignore[assignment]

    async def _waiter() -> None:
        async with reload_gate.driving():
            log.append("waiter ran its gated body")
        await asyncio.sleep(0.05)
        log.append("waiter answered")

    async with reload_gate.synchronized():  # the bus-delivered reload, no request of its own
        request = _served_request(boot, _waiter)
        while not boot.waiting:
            await asyncio.sleep(0)
        await build_and_swap_epoch({}, rebuild=lambda: None, build_serving_app=_serve("new-app"), drain_deadline=0.05)
        assert log == [], "the retire closed (or let run) before the gate turned over"
    await request
    async with asyncio.timeout(2.0):
        await asyncio.gather(*epoch_retire_mod._deferred_retire_tasks)
    assert log == ["waiter ran its gated body", "waiter answered", "closed"]


async def test_a_request_waiting_on_a_recorded_lock_held_outside_the_generation_is_never_severed() -> None:
    """A request of the retiring generation waits on a recorded lock whose holder is no request
    of that generation, so nothing of the generation is in flight or driving once the gate turns
    over: the deferred tail waits for that wait to end, without holding the loop, and closes only
    after the waiter answered."""
    _install_boot("boot-app")
    boot = current_epoch()
    log: list[str] = []
    boot.supervisor = _ClosingSupervisor(log)  # type: ignore[assignment]
    lock = asyncio.Lock()

    async def _waiter() -> None:
        async with hold_recorded(lock):
            log.append("waiter held the lock")
        log.append("waiter answered")

    await lock.acquire()  # held by work that is no request of the boot generation
    try:
        async with reload_gate.synchronized():  # the bus-delivered reload, no request of its own
            request = _served_request(boot, _waiter)
            while not boot.waiting:
                await asyncio.sleep(0)
            await build_and_swap_epoch(
                {}, rebuild=lambda: None, build_serving_app=_serve("new-app"), drain_deadline=0.05
            )
        for _ in range(20):
            await asyncio.sleep(0)
        assert log == [], "the retire closed (or let run) before the lock was released"
    finally:
        lock.release()
    async with asyncio.timeout(2.0):
        await request
        await asyncio.gather(*epoch_retire_mod._deferred_retire_tasks)
    assert log == ["waiter held the lock", "waiter answered", "closed"]


@pytest.mark.parametrize("held_past_budget", [False, True], ids=["within-budget", "past-budget"])
async def test_a_pass_through_waiter_is_ordinary_work_once_the_gate_turns_over(
    held_past_budget: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """A request that only waits a reload out (``synchronized``) is never severed while it
    waits; once it holds the gate it is ordinary in-flight work, which the drain budget bounds."""
    import logging

    _install_boot("boot-app")
    boot = current_epoch()
    log: list[str] = []
    boot.supervisor = _ClosingSupervisor(log)  # type: ignore[assignment]
    budget = 0.1
    release = asyncio.Event()

    async def _lookup() -> None:
        async with reload_gate.synchronized():
            log.append("waited the reload out")
        await release.wait()
        log.append("answered")

    with caplog.at_level(logging.ERROR):
        async with reload_gate.synchronized():
            request = _served_request(boot, _lookup)
            while not boot.waiting:
                await asyncio.sleep(0)
            await build_and_swap_epoch(
                {}, rebuild=lambda: None, build_serving_app=_serve("new-app"), drain_deadline=budget
            )
        if held_past_budget:
            async with asyncio.timeout(2.0):
                await asyncio.gather(*epoch_retire_mod._deferred_retire_tasks)
            release.set()
            await request
            assert log == ["waited the reload out", "closed", "answered"]
            assert "1 request(s) still in flight after the drain budget" in caplog.text
        else:
            await asyncio.sleep(0)
            release.set()
            await request
            async with asyncio.timeout(2.0):
                await asyncio.gather(*epoch_retire_mod._deferred_retire_tasks)
            assert log == ["waited the reload out", "answered", "closed"]
            assert "still in flight after the drain budget" not in caplog.text


async def test_the_supervised_run_driving_a_reload_survives_its_own_retire() -> None:
    """A background run (a tool-run supervisor) that dispatches the reload is not cancelled by
    the supervisor drain of the retire it drives: it completes and records its real outcome."""
    from tai42_skeleton.operations.tool_runs import supervisor as supervisor_mod

    _install_boot("boot-app")
    boot = current_epoch()
    outcome: list[str] = []

    async def _supervised_reload() -> None:
        await reload_gate.run(lambda: None, reimports=False)  # an unrelated gated body first
        async with reload_gate.driving():
            await build_and_swap_epoch(
                {}, rebuild=lambda: None, build_serving_app=_serve("new-app"), drain_deadline=0.5
            )
        await asyncio.sleep(0)  # the reload's tail (its fleet wait) runs after the swap
        outcome.append("completed")

    task = asyncio.create_task(_supervised_reload())
    supervisor_mod._enroll_supervisor(task)
    task.add_done_callback(supervisor_mod._discard_supervisor)
    try:
        await task
    except asyncio.CancelledError as exc:
        outcome.append(f"cancelled: {exc}")
    assert current_epoch() is not boot
    assert outcome == ["completed"]
    async with asyncio.timeout(2.0):
        await asyncio.gather(*epoch_retire_mod._deferred_retire_tasks)
    assert not boot.driving_tasks


async def test_a_task_holding_a_finished_requests_context_drives_nothing() -> None:
    """A task that copied a request's context and outlived it (an MCP session's server task, a
    run spawned from a request) acts for no request: entering the gate releases no slot a
    second time and records nothing as driving, so the in-flight count stays exact."""
    _install_boot("boot-app")
    boot = current_epoch()
    copied: list[contextvars.Context] = []

    async def _request_body() -> None:
        copied.append(contextvars.copy_context())

    await _served_request(boot, _request_body)
    boot.admit()  # another request, still in flight
    entered: list[bool] = []

    async def _later() -> None:
        async with reload_gate.driving():
            entered.append(True)

    await asyncio.get_running_loop().create_task(_later(), context=copied[0])
    assert entered == [True]
    assert boot.driving == set()
    assert boot.in_flight == 1
    boot.release()


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


async def test_a_reload_cancelled_after_the_build_finished_closes_the_new_generations_lifespan() -> None:
    """The reload is cancelled after the new generation's build finished (its lifespan
    entered) but before the reload resumed: the discard closes that lifespan, so no started
    generation is left running behind the still-serving previous one."""
    import asyncio
    from contextlib import asynccontextmanager

    from tai42_skeleton.app.sub_mcp_app import SubAppLifespan

    app_state = _install_boot("boot-app")
    boot_epoch = current_epoch()
    built = asyncio.Event()
    lifespan_exited = asyncio.Event()

    class _LifespanApp:
        @asynccontextmanager
        async def lifespan(self, _app):
            try:
                yield
            finally:
                lifespan_exited.set()

    async def _build(epoch: Epoch) -> _Serving:
        supervisor = SubAppLifespan(_LifespanApp())  # type: ignore[arg-type]
        await supervisor.start()
        epoch.supervisor = supervisor
        # The waiter below wakes before the reload does: the build sets the event and
        # then finishes, so the reload's wake-up is queued behind the test's.
        built.set()
        return _Serving("new-app")

    reload = asyncio.create_task(
        build_and_swap_epoch({"TAI_EPOCH_CANCELLED": "v"}, rebuild=lambda: None, build_serving_app=_build)
    )
    await built.wait()
    reload.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reload

    assert lifespan_exited.is_set()
    assert app_state["app"].name == "boot-app"
    assert current_epoch() is boot_epoch
    assert "TAI_EPOCH_CANCELLED" not in os.environ


async def test_a_build_failing_after_its_lifespan_entered_closes_that_lifespan() -> None:
    """A build that entered its generation's lifespan and then failed: the discard closes it."""
    app_state = _install_boot("boot-app")
    closed: list[str] = []

    class _FakeSupervisor:
        async def aclose(self) -> None:
            closed.append("closed")

    async def _build(epoch: Epoch) -> _Serving:
        epoch.supervisor = _FakeSupervisor()  # type: ignore[assignment]
        raise RuntimeError("deliberate failure after the lifespan entered")

    with pytest.raises(RuntimeError, match="after the lifespan entered"):
        await build_and_swap_epoch({"K": "v"}, rebuild=lambda: None, build_serving_app=_build)

    assert closed == ["closed"]
    assert app_state["app"].name == "boot-app"


# -- the proof build a non-member gives a profile apply -------------------------


async def test_a_proven_build_is_discarded_with_the_live_epoch_untouched(caplog: pytest.LogCaptureFixture) -> None:
    from tai42_skeleton.app.epoch import build_and_discard_epoch

    app_state = _install_boot("boot-app")
    boot_epoch = current_epoch()
    client_epoch = current_client_epoch()
    os.environ["TAI_EPOCH_TEST_EXISTING"] = "old"
    env_before = dict(os.environ)
    loaded_before = set(epoch_mod._loaded_env_keys)
    seen: list[str] = []
    closed: list[str] = []

    class _FakeSupervisor:
        async def aclose(self) -> None:
            closed.append("closed")

    def _rebuild() -> None:
        seen.append(os.environ["TAI_EPOCH_TEST_KEY"])

    async def _build(epoch: Epoch) -> _Serving:
        epoch.supervisor = _FakeSupervisor()  # type: ignore[assignment]
        return _Serving("proven")

    with caplog.at_level("ERROR"):
        await build_and_discard_epoch({"TAI_EPOCH_TEST_KEY": "proposed"}, rebuild=_rebuild, build_serving_app=_build)

    assert seen == ["proposed"]
    assert app_state["app"].name == "boot-app"
    assert current_epoch() is boot_epoch
    assert current_client_epoch() == client_epoch
    assert dict(os.environ) == env_before
    assert epoch_mod._loaded_env_keys == loaded_before
    assert epoch_mod._building_epoch is None
    assert closed == ["closed"]
    assert [r for r in caplog.records if r.levelname == "ERROR"] == []

    del os.environ["TAI_EPOCH_TEST_EXISTING"]


async def test_a_proof_build_that_fails_raises_and_discards_like_the_swap_form(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from tai42_skeleton.app.epoch import build_and_discard_epoch

    app_state = _install_boot("boot-app")
    boot_epoch = current_epoch()
    env_before = dict(os.environ)

    def _rebuild() -> None:
        raise RuntimeError("deliberate proof failure")

    with caplog.at_level("ERROR"), pytest.raises(RuntimeError, match="deliberate proof failure"):
        await build_and_discard_epoch(
            {"TAI_EPOCH_TEST_KEY": "proposed"}, rebuild=_rebuild, build_serving_app=_serve("x")
        )

    assert app_state["app"].name == "boot-app"
    assert current_epoch() is boot_epoch
    assert dict(os.environ) == env_before
    assert any("epoch build failed under the proposed env" in r.getMessage() for r in caplog.records)


# -- stale-settings sweep ------------------------------------------------------


async def test_a_clean_build_and_swap_cycle_leaves_no_stale_settings() -> None:
    _install_boot("boot-app")

    # The retire resets the settings and then sweeps the retired generation; a clean
    # cycle (no holder pins a retired-epoch settings instance) reports zero.
    retired_number = current_epoch().number
    await build_and_swap_epoch(
        {"K": "v"},
        rebuild=lambda: None,
        build_serving_app=_serve("new-app"),
    )
    assert sweep_stale_settings(retired_number) == []


def _install_boot_generation(rebuild) -> None:
    """Install a boot epoch whose core the given ``rebuild`` seam builds, holding no local reference to it."""
    from tai42_skeleton.app import instance

    rebuild()
    epoch_mod._current = Epoch(number=current_client_epoch(), core=instance.app._building, serving_app=_Serving("boot"))
    instance.app._building = None


def _serving_core_rebuild() -> None:
    """The default rebuild's serving-core step plus the identity provider a build instantiates per generation.

    The core carries the access-control adapter chain read under the applied env, and the
    redis identity provider is built against the same settings the way the build's
    provider probe does, so both settings families the generation reads are held by it.
    """
    from tai42_identity_redis.redis_api_key_provider import RedisApiKeyProvider

    from tai42_skeleton.access_control.settings import access_control_settings
    from tai42_skeleton.app import instance

    core = instance.app._build_serving_core()
    core.active_auth_providers["redis"] = RedisApiKeyProvider(access_control_settings())
    instance.app._building = core


async def _record_built_core(epoch: Epoch) -> _Serving:
    """The default build's bookkeeping: the built core recorded on its epoch, the build slot cleared.

    The returned serving surface reaches the core, as a real one reaches its FastMCP.
    """
    from tai42_skeleton.app import instance

    epoch.core = instance.app._building
    instance.app._building = None
    return _Serving("new-app", core=epoch.core)


_KEPT_GENERATIONS: list[object] = []


@pytest.fixture
def _access_control_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Access control and the redis identity provider on, the app's build slot restored after the test."""
    from tai42_skeleton.app import instance

    monkeypatch.setenv("ACCESS_CONTROL_ENABLE", "true")
    monkeypatch.setenv("TAI_IDENTITY_REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(instance.app, "_building", instance.app._building)
    reset_all_settings()
    # The app bound at import holds its own scaffold core's settings for the process life;
    # move past the epoch it was read under so only this test's generations are swept.
    advance_client_epoch()


@pytest.mark.usefixtures("_access_control_on")
async def test_reloads_with_access_control_on_report_no_stale_settings(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    _install_boot_generation(_serving_core_rebuild)
    with caplog.at_level(logging.ERROR, logger="tai42_kit.settings.cache_registry"):
        for _ in range(3):
            await build_and_swap_epoch(
                {"ACCESS_CONTROL_ENABLE": "true", "TAI_IDENTITY_REDIS_URL": "redis://127.0.0.1:1/0"},
                rebuild=_serving_core_rebuild,
                build_serving_app=_record_built_core,
                drain_deadline=0.1,
            )
    # Each generation's settings carry its own epoch, so the live generation's adapter chain
    # and identity provider are never reported as holders of the generation it replaced.
    assert "Stale settings instance" not in caplog.text


_ACCESS_CONTROL_ENV = {"ACCESS_CONTROL_ENABLE": "true", "TAI_IDENTITY_REDIS_URL": "redis://127.0.0.1:1/0"}


async def _reload_access_control_generation() -> None:
    await build_and_swap_epoch(
        _ACCESS_CONTROL_ENV,
        rebuild=_serving_core_rebuild,
        build_serving_app=_record_built_core,
        drain_deadline=0.1,
    )


class _RequestLike:
    """A served request as the runtime keeps it: its scope points at the generation's serving surface."""

    def __init__(self, app: object) -> None:
        self.scope = {"app": app}


# The request a context snapshot carries, as a web framework's request variable does.
_held_request: ContextVar[_RequestLike | None] = ContextVar("test_held_request", default=None)


def _context_holding_a_request_to(surface: object) -> contextvars.Context:
    """A context snapshot of a request the given surface served, the hold a runtime timer or task keeps."""
    context = contextvars.Context()
    context.run(_held_request.set, _RequestLike(surface))
    return context


def _stale_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "Stale settings instance" in r.getMessage()]


def _pending(number: int) -> list[retired_mod.RetiredGeneration]:
    return [generation for generation in retired_mod.retired_generations() if generation.number == number]


@pytest.mark.usefixtures("_access_control_on")
async def test_a_retired_generation_still_reachable_from_a_request_context_is_not_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import asyncio
    import gc
    import logging

    _install_boot_generation(_serving_core_rebuild)
    with caplog.at_level(logging.ERROR, logger="tai42_kit.settings.cache_registry"):
        await _reload_access_control_generation()
        served = current_epoch()
        retired = served.number
        held_context = _context_holding_a_request_to(served.serving_app)
        del served
        await _reload_access_control_generation()
        await asyncio.sleep(0)
        # A request the retired generation served is still kept by the runtime: the
        # generation is legitimately reachable and nothing is reported yet.
        assert _stale_lines(caplog) == []
        assert [generation.surface_alive for generation in _pending(retired)] == [True]
        del held_context
        gc.collect()
        await asyncio.sleep(0)
    # Once its serving surface is collected the generation is certified, and nothing of it remains.
    assert _pending(retired) == []
    assert _stale_lines(caplog) == []


_CAPTURED: list[object] = []


@pytest.mark.usefixtures("_access_control_on")
async def test_a_captured_settings_singleton_is_reported_when_its_generation_is_collected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import asyncio
    import gc
    import logging

    _install_boot_generation(_serving_core_rebuild)
    try:
        with caplog.at_level(logging.ERROR, logger="tai42_kit.settings.cache_registry"):
            await _reload_access_control_generation()
            served = current_epoch()
            retired = served.number
            assert served.core is not None
            _CAPTURED.append(served.core._fast_mcp.auth)  # a module global keeps the generation's adapter
            held_context = _context_holding_a_request_to(served.serving_app)
            del served
            await _reload_access_control_generation()
            await asyncio.sleep(0)
            assert _stale_lines(caplog) == []  # judged only once the generation is released
            del held_context
            gc.collect()
            await asyncio.sleep(0)
        lines = _stale_lines(caplog)
        assert len(lines) == 1, lines
        assert f"AccessControlSettings (epoch {retired})" in lines[0]
        assert "AuthAdapter" in lines[0]
    finally:
        _CAPTURED.clear()


@pytest.mark.usefixtures("_access_control_on")
async def test_a_reload_still_reports_a_retired_generation_something_keeps(caplog: pytest.LogCaptureFixture) -> None:
    import asyncio
    import gc
    import logging

    _install_boot_generation(_serving_core_rebuild)
    try:
        with caplog.at_level(logging.ERROR, logger="tai42_kit.settings.cache_registry"):
            await _reload_access_control_generation()
            _KEPT_GENERATIONS.append(current_epoch().core)  # the whole core outlives its surface
            await _reload_access_control_generation()
            gc.collect()
            await asyncio.sleep(0)
    finally:
        _KEPT_GENERATIONS.clear()
    stale_lines = _stale_lines(caplog)
    assert any("AccessControlSettings" in line and "AuthAdapter" in line for line in stale_lines), stale_lines
    assert any("RedisIdentitySettings" in line and "RedisApiKeyProvider" in line for line in stale_lines), stale_lines


async def test_a_surface_collected_on_another_thread_is_certified_on_the_serving_loop() -> None:
    import asyncio
    import threading

    _install_boot("boot-app")
    held: list[object] = []

    async def _build(_epoch: Epoch) -> _Serving:
        return _Serving("new-app")

    await build_and_swap_epoch({}, rebuild=lambda: None, build_serving_app=_build, drain_deadline=0.1)
    retired = current_epoch().number
    held.append(current_epoch().serving_app)
    await build_and_swap_epoch({}, rebuild=lambda: None, build_serving_app=_build, drain_deadline=0.1)
    assert [generation.surface_alive for generation in _pending(retired)] == [True]

    collector = threading.Thread(target=held.clear)
    collector.start()
    collector.join()
    assert [generation.surface_alive for generation in _pending(retired)] == [False]
    await asyncio.sleep(0)
    assert _pending(retired) == []


def test_no_certification_is_scheduled_on_a_closed_loop() -> None:
    import asyncio

    loop = asyncio.new_event_loop()
    loop.close()
    retired_mod._schedule_certification(loop, 0)  # the process stopped serving: nothing to schedule


async def test_clear_epoch_disarms_the_pending_certifications() -> None:
    _install_boot("boot-app")
    await build_and_swap_epoch({}, rebuild=lambda: None, build_serving_app=_serve("served-app"), drain_deadline=0.1)
    surface = current_epoch().serving_app
    retired = current_epoch().number
    await build_and_swap_epoch({}, rebuild=lambda: None, build_serving_app=_serve("new-app"), drain_deadline=0.1)
    assert [generation.number for generation in retired_mod.retired_generations()] == [retired]

    await epoch_mod.clear_epoch()
    assert retired_mod.retired_generations() == ()
    del surface  # collected after the process left its generations: nothing is scheduled


class _RequestScopedSettings(BaseSettings):
    value: int = 1


@settings_cache
def _request_scoped_settings() -> _RequestScopedSettings:
    return _RequestScopedSettings()


async def _door_reload_with_a_concurrent_request(release_within_budget: bool) -> list[str]:
    """A door-driven reload while a concurrent request holds a settings instance across an await.

    The driving request and the concurrent one are both admitted on the retiring
    generation; the concurrent one read its settings before the reload and finishes
    on the old generation (within the drain budget, or past it). The driver answers
    after the concurrent request's drain is decided. Returns the order of the driver's
    answer and the transport close.
    """
    _install_boot("boot-app")
    await build_and_swap_epoch({}, rebuild=lambda: None, build_serving_app=_serve("served-app"), drain_deadline=0.1)
    boot = current_epoch()
    order: list[str] = []
    boot.supervisor = _ClosingSupervisor(order)  # type: ignore[assignment]
    boot.admit()  # the concurrent request
    answered = asyncio.get_running_loop().create_future()
    swapped = asyncio.Event()
    driver_answers = asyncio.Event()

    async def _concurrent_request() -> None:
        # The request runs inside the generation's serving surface and reads its settings.
        scope = {"app": boot.serving_app}
        settings = _request_scoped_settings()
        await answered
        assert settings.value == 1
        assert scope["app"] is not None
        boot.release()

    async def _driver() -> None:
        async with reload_gate.driving():
            await build_and_swap_epoch(
                {"TAI_EPOCH_DOOR_RELOAD": "v"},
                rebuild=lambda: None,
                build_serving_app=_serve("new-app"),
                drain_deadline=0.5,
            )
        swapped.set()
        await driver_answers.wait()
        order.append("driver answered")

    request = asyncio.create_task(_concurrent_request())
    await asyncio.sleep(0)
    driver = _served_request(boot, _driver)
    try:
        await swapped.wait()
    finally:
        os.environ.pop("TAI_EPOCH_DOOR_RELOAD", None)
    if release_within_budget:
        answered.set_result(None)
        await request
    else:
        # The concurrent request outlives the budget: wait until the tail has reported it.
        await asyncio.sleep(0.7)
    driver_answers.set()
    await driver
    async with asyncio.timeout(5.0):
        await asyncio.gather(*epoch_retire_mod._deferred_retire_tasks)
    if not release_within_budget:
        answered.set_result(None)
        await request
    del request, driver
    await asyncio.sleep(0)
    return order


async def test_a_door_driven_reload_sweeps_after_the_in_flight_requests_finish(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    with caplog.at_level(logging.ERROR):
        order = await _door_reload_with_a_concurrent_request(release_within_budget=True)
    # The request finished on the old generation inside the drain budget, before the sweep.
    assert "Stale settings instance" not in caplog.text
    assert "still in flight after the drain budget" not in caplog.text
    assert order == ["driver answered", "closed"]


async def test_a_door_driven_reload_reports_a_request_past_the_drain_budget_and_no_settings_it_releases(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    with caplog.at_level(logging.ERROR):
        order = await _door_reload_with_a_concurrent_request(release_within_budget=False)
    # The drain reports the concurrent request it could not wait for — never the driver, which
    # it does not count — and the close waits for the driver's answer. The request releases its
    # settings when it ends, so nothing is reported while it runs on the retired generation or
    # once that generation is certified.
    assert "1 request(s) still in flight after the drain budget" in caplog.text
    assert order == ["driver answered", "closed"]
    assert "Stale settings instance" not in caplog.text


_request_context_state: ContextVar[object | None] = ContextVar("test_request_context_state", default=None)


async def test_a_door_driven_reload_starts_the_new_generations_tasks_outside_the_driving_request(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The driving request's context reaches the retiring generation; nothing the new one keeps copies it."""
    import asyncio
    import logging

    _install_boot("boot-app")
    boot = current_epoch()
    retired_number = boot.number
    # The surface every request of the generation points at: it stays reachable while the
    # driving request (its task and context) lives, so the generation is judged after it.
    surface = _Serving("boot-surface")
    boot.record_serving_surface(surface)
    generation_tasks: list[asyncio.Task[bool]] = []

    def _spawn_generation_task() -> None:
        generation_tasks.append(asyncio.get_running_loop().create_task(asyncio.Event().wait()))

    async def _build(_epoch: Epoch) -> _Serving:
        _spawn_generation_task()  # the generation's lifespan supervisor
        return _Serving("new-app")

    async def _establish() -> None:
        _spawn_generation_task()  # a periodic loop of the generation

    async def _driving_request() -> None:
        # The request carries an object of the retiring generation in its context.
        _request_context_state.set(_request_scoped_settings())
        try:
            async with reload_gate.driving():
                await build_and_swap_epoch(
                    {"TAI_EPOCH_DOOR_RELOAD": "v"},
                    rebuild=lambda: None,
                    build_serving_app=_build,
                    establish_background_loops=_establish,
                    drain_deadline=1.0,
                )
        finally:
            os.environ.pop("TAI_EPOCH_DOOR_RELOAD", None)

    try:
        with caplog.at_level(logging.ERROR, logger="tai42_kit.settings.cache_registry"):
            driver = _served_request(boot, _driving_request, context=contextvars.Context(), surface=surface)
            await driver
            async with asyncio.timeout(5.0):
                await asyncio.gather(*epoch_retire_mod._deferred_retire_tasks)
            del driver, surface
            gc.collect()
            async with asyncio.timeout(5.0):
                while any(generation.number == retired_number for generation in retired_mod.retired_generations()):
                    await asyncio.sleep(0)
        assert len(generation_tasks) == 2
        assert all(_request_context_state not in task.get_context() for task in generation_tasks)
        assert "Stale settings instance" not in caplog.text
    finally:
        for task in generation_tasks:
            task.cancel()
        await asyncio.gather(*generation_tasks, return_exceptions=True)


def test_the_sweep_flags_a_leaked_retired_settings_instance() -> None:
    from pydantic_settings import BaseSettings

    class _Leaky(BaseSettings):
        pass

    # Stamp an instance under the current epoch, then advance so it is retired.
    leaked = _Leaky()
    _stamp_settings(leaked)
    retired = current_client_epoch()
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
