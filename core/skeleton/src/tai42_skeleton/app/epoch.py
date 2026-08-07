"""The per-epoch serving surface and the build+swap primitive a profile apply calls.

An **epoch** is the process's live serving generation. A settings-profile apply
builds a NEW epoch off to the side under a proposed env and — only if the build
succeeds — swaps it in atomically (the ASGI dispatch slot plus the current-epoch
pointer) and retires the previous one. A failed build is DISCARDED with zero
mutation of the live epoch and the process env restored exactly, so the old epoch
keeps serving.

The process spine stays process-lifetime and is deliberately NOT rebuilt per epoch:
the ``TaiMCP`` identity, the worker bus + subscription, ``reload_gate``, the boot
sentinel, the prometheus objects, and the route registry (a dedup-once metadata map).
These are the intentional exemptions — process-global by design, not cleanup targets.
What an epoch owns is the serving handle the dispatch slot points at, its fresh
FastMCP + feature collaborators (the ``ServingCore``), the per-epoch in-flight
accounting, the periodic loops that must retire with it, and the pre-apply census.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from tai42_kit.clients import advance_client_epoch, current_client_epoch, drain_epoch
from tai42_kit.settings import reset_all_settings
from tai42_kit.settings.cache_registry import register_settings_reset, sweep_stale_settings

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

    from tai42_skeleton.app.server import ServingCore
    from tai42_skeleton.app.sub_mcp_app import SubAppLifespan

logger = logging.getLogger(__name__)


@dataclass
class Epoch:
    """One serving generation.

    ``number`` is the client epoch the generation was born under
    (``current_client_epoch``), so its retired pools and its stale-settings sweep key
    on it. ``serving_app`` is the ASGI handle the dispatch slot serves for this
    generation. In-flight requests are counted against the epoch that admitted them,
    so a retire drains exactly this generation's live work.
    """

    number: int
    serving_app: ASGIApp | None = None
    # This generation's serving surface: the fresh FastMCP + its feature
    # collaborators. Owned here so a retire drops exactly this generation's core and
    # the process-spine app resolves the live generation's collaborators through it.
    core: ServingCore | None = None
    # The dedicated task holding this generation's FastMCP lifespan (its
    # streamable-http session-manager task group) open. Retiring the generation
    # ``aclose()``s it, terminating this generation's transports.
    supervisor: SubAppLifespan | None = None
    # The pre-apply fleet census this generation was built against, captured by the
    # apply caller and stored here for the fanout diff — never used by the
    # build itself.
    census: frozenset[str] | None = None
    _in_flight: int = 0
    _idle: asyncio.Event = field(default_factory=asyncio.Event)
    _periodic_cancels: list[Callable[[], Awaitable[None]]] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Born idle: no request has entered this generation yet.
        self._idle.set()

    # -- per-epoch in-flight accounting ----------------------------------------

    def admit(self) -> None:
        self._in_flight += 1
        self._idle.clear()

    def release(self) -> None:
        # Never drops below zero: a double release would falsely mark the epoch idle
        # while work is still in flight and let the retire force-close under it.
        self._in_flight = max(0, self._in_flight - 1)
        if self._in_flight == 0:
            self._idle.set()

    @property
    def in_flight(self) -> int:
        return self._in_flight

    def register_periodic_loop(self, cancel: Callable[[], Awaitable[None]]) -> None:
        """Register a cancel-and-await callback for a periodic loop this generation
        owns (the failed-MCP reprobe, the marketplace advisories poll, the
        conversations delivery sweep). Every registered loop is cancelled when the
        epoch retires, so a retired generation leaves no timer running against the
        fresh one."""
        self._periodic_cancels.append(cancel)

    async def _drain_in_flight(self, deadline: float) -> None:
        """Wait, bounded by ``deadline``, for this generation's admitted requests to
        finish. A request still in flight past the budget is logged loudly and the
        retire proceeds (the fresh epoch already serves new traffic)."""
        if self._idle.is_set():
            return
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=deadline)
        except TimeoutError:
            logger.error(
                "epoch %d retire: %d request(s) still in flight after the drain budget",
                self.number,
                self._in_flight,
            )

    async def _cancel_periodic_loops(self) -> None:
        for cancel in self._periodic_cancels:
            try:
                await cancel()
            except Exception:
                logger.exception("epoch %d retire: a periodic-loop cancel failed", self.number)


class EpochAdmissionApp:
    """ASGI wrapper that counts every request against the epoch that served it.

    The swap re-points the dispatch slot to the NEW epoch's wrapper, so a request
    that entered under the old wrapper still finishes there — the count is exact per
    generation and the retire drains only that generation's admitted work.
    Non-HTTP scopes pass straight through uncounted.
    """

    def __init__(self, app: ASGIApp, epoch: Epoch) -> None:
        self._app = app
        self._epoch = epoch

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        self._epoch.admit()
        try:
            await self._app(scope, receive, send)
        finally:
            self._epoch.release()


# -- process serving generation ------------------------------------------------

# The live serving generation and the ASGI dispatch's swap slot the build re-points.
# Established once (the epoch core by ``app_context``, the dispatch slot by the worker
# lifespan) and dropped at worker-lifespan exit.
_current: Epoch | None = None
_serving_slot: dict[str, Any] | None = None

# The epoch a build is populating, for the span of ``build_and_swap_epoch``'s build
# step (``None`` otherwise). A per-epoch startup handler that spawns a periodic loop
# registers its cancel with THIS epoch, so the loop retires with the generation that
# started it rather than leaking onto the next — the build's new epoch is not yet the
# live ``_current`` during the build.
_building_epoch: Epoch | None = None

# The env keys the last successful build loaded, so the next build can drop a key
# that the proposed env removed rather than leaving its stale value in os.environ —
# mirrors ``_loaded_env_keys`` in the in-place reload path.
_loaded_env_keys: set[str] = set()

# Set to the retiring epoch number for the span of the retire's settings reset so
# the registered sweep hook sweeps exactly that generation; ``None`` otherwise.
_retiring_epoch: int | None = None


def current_epoch() -> Epoch:
    """The live serving generation, or a loud error before the worker lifespan
    installs the boot epoch. Epoch-scoped caches key on ``current_epoch().number``."""
    if _current is None:
        raise RuntimeError("no serving epoch is installed — enter the worker lifespan first")
    return _current


def current_epoch_or_none() -> Epoch | None:
    """The live serving generation or ``None`` — the non-raising accessor for a
    caller that runs outside a worker lifespan (an embedded read, a probe)."""
    return _current


def epoch_under_construction() -> Epoch:
    """The epoch a build is populating, else the live epoch — the accessor a per-epoch
    startup handler uses to register a periodic loop's cancel with the generation that
    owns it. During ``build_and_swap_epoch`` this is the new epoch (not yet ``_current``);
    at boot it falls back to the just-installed boot epoch."""
    return _building_epoch if _building_epoch is not None else current_epoch()


def epoch_under_construction_or_none() -> Epoch | None:
    """:func:`epoch_under_construction`, but ``None`` (never raising) when no epoch is
    installed — for a periodic-loop spawn that may run in a loop-less / pre-epoch context
    (a bare unit test), where there is no generation to register the loop with."""
    return _building_epoch if _building_epoch is not None else _current


def install_boot_core(core: ServingCore) -> Epoch:
    """Establish the boot serving generation's core, called once by ``app_context``
    before ``start()`` runs — so ``start()`` and the epoch handlers register into this
    generation's FastMCP, and every process-spine read resolves to it. The dispatch
    slot + serving app are attached later by the worker lifespan
    (:func:`attach_boot_serving_app`); an embedded / pure-``app_context`` caller needs
    no serving app. The boot epoch's number is the current client epoch."""
    global _current, _loaded_env_keys
    _current = Epoch(number=current_client_epoch(), core=core)
    _loaded_env_keys = set()
    return _current


def attach_boot_serving_app(serving_app: ASGIApp, app_state: dict[str, Any]) -> None:
    """Point the ASGI dispatch slot at the boot serving app, called by the worker
    lifespan after it builds the ``http_app`` and enters its FastMCP lifespan. Records
    the slot the build+swap primitive re-points."""
    global _serving_slot
    _serving_slot = app_state
    current_epoch().serving_app = serving_app
    app_state["app"] = serving_app


async def clear_epoch() -> None:
    """Drop the process serving generation, closing the live generation's FastMCP
    lifespan supervisor (terminating its transports) so a later lifespan in the same
    process starts from a clean slate."""
    global _current, _serving_slot, _loaded_env_keys
    current = _current
    if current is not None and current.supervisor is not None:
        await current.supervisor.aclose()
    _current = None
    _serving_slot = None
    _loaded_env_keys = set()


async def capture_census_snapshot() -> frozenset[str]:
    """Capture the pre-apply fleet census as the set of live worker origins.

    The apply flow captures this BEFORE the local swap and diffs it into the fanout
    report: a worker present pre-apply but absent from the expected set reports
    ``departed`` rather than silence. Reads through the one persistent process bus."""
    from tai42_skeleton.app import instance

    origins = await instance.app.bus.census()
    return frozenset(o.origin for o in origins)


@register_settings_reset
def _sweep_retiring_epoch() -> None:
    """Settings-reset hook: when a retire triggers the reset, sweep the retired
    generation for stale-config leaks. A no-op for every other reset (the retire flag
    is unset), so the global reset stays cheap. Never drops anything — a retired-epoch
    settings instance still reachable is reported loudly by ``sweep_stale_settings``."""
    retiring = _retiring_epoch
    if retiring is None:
        return
    sweep_stale_settings(retiring)


def _apply_env(proposed: Mapping[str, str]) -> None:
    """Apply the proposed env to ``os.environ``, dropping any previously loaded key
    the proposed env removed (removed-key reconciliation). Only keys a prior build
    loaded are dropped — unrelated process env (PATH, the launcher's boot identity)
    is untouched."""
    global _loaded_env_keys
    for key in _loaded_env_keys - set(proposed):
        os.environ.pop(key, None)
    os.environ.update(proposed)
    _loaded_env_keys = set(proposed)


def _restore_env(snapshot: Mapping[str, str], loaded: set[str]) -> None:
    """Restore ``os.environ`` exactly to ``snapshot`` on the failure branch — the
    restore-on-failure ONLY counterpart of ``_apply_env`` (never a with-block, which
    would restore on success too and defeat the stays-live contract)."""
    global _loaded_env_keys
    os.environ.clear()
    os.environ.update(snapshot)
    _loaded_env_keys = loaded


def _swap(new_epoch: Epoch) -> Epoch:
    """Publish the new generation atomically: the current-epoch pointer and the ASGI
    dispatch slot flip together, so no request ever sees a half-swapped surface."""
    global _current
    old = _current
    if old is None:
        raise RuntimeError("cannot swap epochs before the boot epoch is installed")
    _current = new_epoch
    if _serving_slot is not None:
        _serving_slot["app"] = new_epoch.serving_app
    return old


def _drain_budget(deadline: float | None) -> float:
    # ``deadline`` here is a relative duration in SECONDS (the drain budget), not an
    # absolute time — mirrors the kit's ``drain_epoch(epoch, deadline)`` vocabulary.
    if deadline is not None:
        return deadline
    from tai42_skeleton.routers.tool_runs_settings import tool_runs_settings

    return tool_runs_settings().shutdown_drain_seconds


async def _retire(old: Epoch, retired: int, deadline: float | None) -> None:
    """Retire the previous generation, bounded by the drain budget.

    Cancels the generation's periodic loops first (no timer outlives its epoch),
    drains its in-flight requests and background supervisors, closes its retired
    client pools, and sweeps its stale settings (via the registered reset hook). Each
    step is independent so one failure cannot skip the rest — the fresh epoch already
    serves new traffic, so a retire fault is loud but never fatal.
    """
    budget = _drain_budget(deadline)
    await old._cancel_periodic_loops()
    await old._drain_in_flight(budget)

    from tai42_skeleton.operations.tool_runs import drain_supervisors

    try:
        # Drain ONLY this generation's in-flight runs — a run admitted on the fresh
        # epoch during the retire is left running.
        await drain_supervisors(
            budget,
            epoch=old.number,
            reason="the serving epoch was retired by a config apply before the tool-run completed",
        )
    except Exception:
        logger.exception("epoch %d retire: background-run drain failed", old.number)

    # Close the retired generation's FastMCP lifespan: its ``__aexit__`` terminates
    # every live transport and drops its session manager, so the fresh epoch serves a
    # NEW session-id space and stateful clients re-initialise — no handoff.
    if old.supervisor is not None:
        try:
            await old.supervisor.aclose()
        except Exception:
            logger.exception("epoch %d retire: serving-lifespan close failed", old.number)

    # Sweep the retired generation's stale settings through the registered reset
    # hook: the flag scopes the sweep to exactly this generation.
    global _retiring_epoch
    _retiring_epoch = retired
    try:
        reset_all_settings()
    finally:
        _retiring_epoch = None

    try:
        await drain_epoch(retired, budget)
    except Exception:
        logger.exception("epoch %d retire: client-pool drain failed", retired)


def _default_rebuild() -> None:
    """Build the fresh serving core OFF TO THE SIDE and re-initialise the process
    registries into it, under the ALREADY applied proposed env (the primitive applied
    it before calling this).

    A fresh ``ServingCore`` (fresh FastMCP under a freshly-read AuthAdapter) is set
    on the app's ``_building`` slot, so ``start()`` and the epoch handlers register
    into it — the live core is NEVER touched. The env is NOT re-read from the store: the
    proposed env is live in ``os.environ`` and the settings caches were cleared, so this
    resolves every settings read under the env about to be persisted. On ANY
    failure the half-built core is discarded (``_building`` dropped) and re-raised, so
    the live epoch keeps serving untouched."""
    from tai42_skeleton.app import instance
    from tai42_skeleton.manifest import Manifest

    app = instance.app
    app._building = app._build_serving_core()
    try:
        manifest = Manifest.model_validate(app.config.config_manager.read_manifest())
        app.lifecycle.reload_registries(manifest)
    except BaseException:
        # Discard the half-built core so every read resolves back to the live epoch.
        app._building = None
        raise


async def _default_build_serving_app(epoch: Epoch) -> ASGIApp:
    """Build this generation's FRESH dispatch handle off the just-built core and enter
    its FastMCP lifespan.

    A fresh ``http_app`` is built off the ``_building`` core's fresh FastMCP, so its
    route table — including a reload-added router — is snapshotted anew and actually
    serves. Its FastMCP lifespan (a fresh streamable-http session manager) is entered
    through a dedicated-task supervisor so the swap task can later close it in the same
    context; the built core is recorded on the epoch (retire drops it), and
    ``_building`` is cleared so post-swap reads resolve through ``current_epoch()``."""
    from tai42_skeleton.app import instance
    from tai42_skeleton.app.sub_mcp_app import SubAppLifespan

    app = instance.app
    core = app._building
    if core is None:
        raise RuntimeError("no core was built for this epoch — the rebuild step must run first")
    try:
        inner = app.http_app()
        lifespan_app = getattr(inner, "mcp_lifespan_app", inner)
        supervisor = SubAppLifespan(lifespan_app)
        # Enter the fresh FastMCP lifespan (fresh session-manager task group); a
        # lifespan-enter failure has already unwound itself when start() re-raises.
        await supervisor.start()
        epoch.core = core
        epoch.supervisor = supervisor
        return EpochAdmissionApp(inner, epoch)
    finally:
        app._building = None


async def _default_establish_background_loops() -> None:
    """Re-establish the swapped-in generation's loop-affine background loops (the
    advisories poll, the conversations delivery sweep) ON the serving loop this primitive
    runs on, registering each with the now-current epoch so it retires with the
    generation. The per-epoch handlers ran on a throwaway build-thread loop and could not
    spawn a loop that survives the build, so these are (re)started here instead. Loud but
    non-fatal on a single establisher's failure — the fresh epoch already serves."""
    from tai42_skeleton.app import instance

    await instance.app._run_post_swap_handlers(raise_on_error=False)


async def build_and_swap_epoch(
    proposed_env: Mapping[str, str],
    *,
    rebuild: Callable[[], Any] | None = None,
    build_serving_app: Callable[[Epoch], Awaitable[ASGIApp]] | None = None,
    establish_background_loops: Callable[[], Awaitable[None]] | None = None,
    drain_deadline: float | None = None,
) -> Epoch:
    """Build a fresh serving epoch under ``proposed_env`` and swap it in atomically.

    The reload primitive the apply flow calls. Env-write-last: the
    caller hands the PROPOSED FULL env explicitly — never a store read — so the build
    resolves every settings read under the env that is about to be persisted.

    Protocol:

    - snapshot ``os.environ``, apply ``proposed_env`` (removed-key reconciled), and
      clear the accessor cache with ``reset_all_settings`` so the build reads it;
    - build the new serving surface OFF TO THE SIDE — ``rebuild`` re-initialises the
      registries, ``build_serving_app`` produces this generation's fresh dispatch
      handle — with ZERO mutation of the live epoch;
    - SUCCESS: advance the client epoch, swap the dispatch slot and the current-epoch
      pointer together, retire the previous generation (periodic loops cancelled,
      in-flight work + supervisors drained, retired pools closed, stale settings swept),
      and establish the new generation's loop-affine background loops on THIS serving
      loop (registered with the new epoch). The applied env STAYS live; the caller
      persists it;
    - FAILURE: the half-built epoch is DISCARDED, ``os.environ`` is restored EXACTLY
      from the snapshot (restore-on-failure only), the old epoch keeps serving
      untouched, and the build failure is re-raised loudly.

    The ``rebuild`` / ``build_serving_app`` / ``establish_background_loops`` seams
    default to the running app; they are injectable so the build/swap/discard contract
    is exercised in isolation.
    """
    from tai42_skeleton.app.registry_staging import abort_staging_all, begin_staging_all, commit_staging_all

    rebuild = rebuild or _default_rebuild
    build_serving_app = build_serving_app or _default_build_serving_app
    establish_background_loops = establish_background_loops or _default_establish_background_loops

    snapshot = dict(os.environ)
    loaded_before = set(_loaded_env_keys)
    _apply_env(proposed_env)
    reset_all_settings()
    # Open a staged generation for every per-generation global, so ``start()`` and the
    # epoch handlers populate the generation being built and the live epoch's globals
    # stay untouched until the atomic commit below.
    begin_staging_all()

    # All fallible work runs before the epoch counter advances, so a failed build
    # leaves the monotonic client epoch — and every piece of live state — untouched.
    global _building_epoch
    new_epoch = Epoch(number=current_client_epoch())
    _building_epoch = new_epoch
    try:
        rebuild()
        serving_app = await build_serving_app(new_epoch)
    except BaseException:
        # Drop every staged generation and re-derive the process caches from the
        # untouched committed state: the staged registrations are discarded, the env is
        # restored exactly, the settings accessor cache is cleared, and the route index
        # is re-derived from the live route registry (a partial reimport may have added
        # routes — the spine route registry dedups and is never rolled back). The
        # live epoch keeps serving with zero mutation.
        from tai42_skeleton.access_control.role_gate import reset_route_index

        abort_staging_all()
        _restore_env(snapshot, loaded_before)
        reset_all_settings()
        reset_route_index()
        logger.error(
            "epoch build failed under the proposed env — discarded; the previous epoch keeps serving",
            exc_info=True,
        )
        raise
    finally:
        _building_epoch = None

    retired = advance_client_epoch()
    new_epoch.number = current_client_epoch()
    new_epoch.serving_app = serving_app
    # Promote every staged generation to committed in the same no-await stretch as the
    # dispatch-slot swap, so no request ever sees a mix of the old and new generations.
    commit_staging_all()
    old_epoch = _swap(new_epoch)
    await _retire(old_epoch, retired, drain_deadline)
    # Post-swap, ON the serving loop: (re)establish this generation's loop-affine
    # background loops and register them with the now-current epoch. Runs AFTER the
    # retire cancelled the previous generation's loops, so there is no cross-generation
    # overlap and no timer outlives its epoch.
    await establish_background_loops()
    return new_epoch
