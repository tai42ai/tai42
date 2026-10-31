"""The per-epoch serving surface and the build+swap primitive a profile apply calls.

An **epoch** is the process's live serving generation. A settings-profile apply
builds a NEW epoch off to the side under a proposed env and — only if the build
succeeds — swaps it in atomically (the ASGI dispatch slot plus the current-epoch
pointer) and retires the previous one. A failed build is DISCARDED with zero
mutation of the live epoch and the process env restored exactly, so the old epoch
keeps serving. The one build has two endings: a bus member swaps it in
(:func:`build_and_swap_epoch`); a process that is not a bus member (a job child a
backend worker forked) has no serving surface to swap into and discards the proven
build (:func:`build_and_discard_epoch`), so the env is proven to build there too. The
stored env a build applies and a discard restores is kept by
:mod:`~tai42_skeleton.app.applied_env`.

The process spine stays process-lifetime and is deliberately NOT rebuilt per epoch:
the ``TaiMCP`` identity, the worker bus + subscription, ``reload_gate``, the boot
sentinel, the prometheus objects, and the route registry (a dedup-once metadata map).
These are the intentional exemptions — process-global by design, not cleanup targets.
What an epoch owns is the serving handle the dispatch slot points at, its fresh
FastMCP + feature collaborators (the ``ServingCore``), the per-epoch in-flight
accounting, and the periodic loops that must retire with it.

FORK INVARIANT (canonical statement in ``tai42_kit.fork_gate``): the build's
``rebuild`` step re-imports the manifest modules (``import_or_reload_package``), so while
a build runs no backend child may be FORKED and no in-process job may RUN — a child
forked mid-reimport deadlocks on an inherited ``importlib`` per-module lock. Both routes
into a build hold ``tai42_kit.fork_gate``'s exclusive side against a backend's job
span: ``reload_gate.run(..., reimports=True)``
(the ``reload_config`` doors, which marshal the build back onto the serving loop) and
``ConfigService.apply_replace_env`` (the profile-apply door, which awaits
``build_and_swap_epoch`` or ``build_and_discard_epoch`` under ``fork_gate.exclusive_async``). A
build reached by any other route must take that same exclusive side.
"""

from __future__ import annotations

import asyncio
import logging
import os
import weakref
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from tai42_kit.clients import advance_client_epoch, current_client_epoch
from tai42_kit.fork_gate import fork_gate
from tai42_kit.settings import reset_all_settings, unbind_respawn_class_env
from tai42_kit.settings.cache_registry import restamp_settings_born_after, settings_birth_mark

from tai42_skeleton.app import epoch_defaults
from tai42_skeleton.app.applied_env import (
    applied_env_keys,
    apply_env_and_reset_settings,
    clear_applied_env,
    restore_env,
)
from tai42_skeleton.app.epoch_retire import retire_epoch
from tai42_skeleton.app.retired_generations import detach_retired_generations

if TYPE_CHECKING:
    from starlette.types import ASGIApp

    from tai42_skeleton.app.admission import RequestAdmission
    from tai42_skeleton.app.server import ServingCore
    from tai42_skeleton.app.sub_mcp_app import SubAppLifespan
    from tai42_skeleton.config.deployment_env import DeploymentOwnership

logger = logging.getLogger(__name__)


@dataclass
class Epoch:
    """One serving generation.

    ``number`` is the client epoch the generation was born under
    (``current_client_epoch``), so its retired pools and its stale-settings sweep key
    on it. ``serving_app`` is the ASGI handle the dispatch slot serves for this
    generation; its serving surface (:meth:`record_serving_surface`) is the object every
    request it serves points at. In-flight requests are counted against the epoch that
    admitted them, so a retire drains exactly this generation's live work.
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
    # The work engaged with the reload gate on behalf of this generation (see
    # :func:`gate_party`): the requests and tool-run supervisors that drive a gated body
    # (until they finish) and those waiting to acquire the gate or another lock whose holder
    # may drive this generation's retire (:func:`hold_recorded`). A retire never counts them
    # against the drain budget and never closes this generation's transports under them.
    # A request or task can wait more than once at a time (two calls of one SSE session share
    # the session stream's record; two child tasks of one supervised run share the run), so
    # ``waiting_tasks`` counts each task's concurrent waits and a request record counts its own.
    driving: set[RequestAdmission] = field(default_factory=set)
    waiting: set[RequestAdmission] = field(default_factory=set)
    driving_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    waiting_tasks: Counter[asyncio.Task[Any]] = field(default_factory=Counter)
    _surface: weakref.ref[object] | None = None
    _in_flight: int = 0
    _idle: asyncio.Event = field(default_factory=asyncio.Event)
    # Set whenever work of this generation starts waiting on a recorded lock, so a
    # synchronous drain stops waiting and the retire defers instead.
    _engagement: asyncio.Event = field(default_factory=asyncio.Event)
    # Set whenever a wait of work of this generation on a recorded lock ends, so the deferred
    # retire tail waits for the generation's waiters without holding the loop.
    _wait_ended: asyncio.Event = field(default_factory=asyncio.Event)
    _periodic_cancels: list[Callable[[], Awaitable[None]]] = field(default_factory=list)
    # Sub-app lifespans that served this generation and were dropped from the sub-MCP cache
    # while it was live (see :meth:`adopt_transport`); closed with ``supervisor``.
    _adopted_transports: list[SubAppLifespan] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Start the generation idle, before any request has entered it."""
        # Born idle: no request has entered this generation yet.
        self._idle.set()

    # -- per-epoch in-flight accounting ----------------------------------------

    def admit(self) -> None:
        """Count one admitted request against this generation."""
        self._in_flight += 1
        self._idle.clear()

    def release(self) -> None:
        """Release one admitted request, marking the generation idle when none remain."""
        # Never drops below zero: a double release would falsely mark the epoch idle
        # while work is still in flight and let the retire force-close under it.
        self._in_flight = max(0, self._in_flight - 1)
        if self._in_flight == 0:
            self._idle.set()

    @property
    def in_flight(self) -> int:
        """The count of requests currently admitted on this generation."""
        return self._in_flight

    def record_serving_surface(self, surface: object) -> None:
        """Record the object every request this generation serves points at (its ``scope["app"]``).

        Held weakly: what a served request leaves behind (a context a runtime timer or task
        keeps, an open stream) reaches the generation through this object, so once it is
        collected nothing a request left behind reaches the generation any more.
        """
        self._surface = weakref.ref(surface)

    def serving_surface(self) -> object | None:
        """This generation's recorded serving surface, or ``None`` when none was recorded or it was collected."""
        return None if self._surface is None else self._surface()

    def drop_serving_surface(self) -> None:
        """Drop this generation's serving handle, core, lifespan supervisor and adopted sub-app lifespans.

        The retire calls it once the generation is drained and closed, so the epoch object
        the retire itself keeps no longer reaches the generation's collaborators (and the
        settings they read) when the retired generation is certified.
        """
        self.serving_app = None
        self.core = None
        self.supervisor = None
        self._adopted_transports.clear()

    def adopt_transport(self, lifespan: SubAppLifespan) -> None:
        """Take ownership of a sub-app lifespan that served this generation, closing it with :meth:`close_transports`.

        A rebuild drops every cached sub-MCP app while this generation still serves; a
        session on such an app (a tool call over ``/app/{slug}`` that drives the reload)
        is this generation's to retire, together with its own session manager.
        """
        self._adopted_transports.append(lifespan)

    async def close_transports(self) -> None:
        """Close this generation's FastMCP lifespan and every adopted sub-app lifespan.

        Each lifespan's close first ends the sessions its transport keeps outside the
        lifespan (an SSE app's, see :meth:`~tai42_skeleton.app.sub_mcp_app.SubAppLifespan.aclose`);
        the streamable-HTTP session manager terminates its own on the lifespan's exit. Each close runs even when another
        fails; a failure is re-raised once every close was attempted (one failure as itself,
        several as an ``ExceptionGroup``).
        """
        lifespans = [self.supervisor] if self.supervisor is not None else []
        lifespans.extend(self._adopted_transports)
        self._adopted_transports.clear()
        errors: list[Exception] = []
        for lifespan in lifespans:
            try:
                await lifespan.aclose()
            except Exception as exc:
                errors.append(exc)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ExceptionGroup(f"epoch {self.number}: transport close failures", errors)

    @property
    def engaged(self) -> bool:
        """Whether any work of this generation drives the reload gate or waits on a recorded lock."""
        return bool(self.driving or self.waiting or self.driving_tasks or self.waiting_tasks)

    @property
    def gate_waiters(self) -> bool:
        """Whether a request or tool-run supervisor of this generation is waiting to acquire a recorded lock."""
        return bool(self.waiting or self.waiting_tasks)

    def note_gate_wait(self) -> None:
        """Signal that work of this generation started waiting on a recorded lock (see :meth:`_drain_in_flight`)."""
        self._engagement.set()

    def begin_task_wait(self, task: asyncio.Task[Any]) -> None:
        """Record one wait of the tool-run supervisor ``task`` on a recorded lock."""
        self.waiting_tasks[task] += 1
        self.note_gate_wait()

    def end_task_wait(self, task: asyncio.Task[Any]) -> None:
        """Record that one wait of ``task`` on a recorded lock ended."""
        self.waiting_tasks[task] -= 1
        if self.waiting_tasks[task] <= 0:
            del self.waiting_tasks[task]
            self.note_wait_ended()

    def note_wait_ended(self) -> None:
        """Signal that a wait of work of this generation on a recorded lock ended (see :meth:`_wait_gate_waiters`)."""
        self._wait_ended.set()

    async def _wait_gate_waiters(self) -> None:
        """Wait until no request or tool-run supervisor of this generation waits on a recorded lock.

        Unbounded here by design, as :meth:`_wait_driving` is: a waiter moves once the lock's
        holder releases it, and the holder's own bounds bound that. Suspends on each wait's
        end, so the loop keeps running the holder meanwhile.
        """
        while self.gate_waiters:
            self._wait_ended.clear()
            await self._wait_ended.wait()

    async def _wait_driving(self) -> None:
        """Wait for every request and tool-run supervisor driving a gated body of this generation to finish.

        The wait has no bound of its own. A driver's remaining work is the reload's own tail,
        which the gated body's own bounds bound (the rebuild, the fleet apply timeout, a
        recycle step), and then the writing of its reply. A call of an SSE session is a
        request of its own here, and its record finishes only once its session stream's
        ASGI ``send`` of the reply has returned; that send waits on the client's reads, so a
        connected SSE client that stops reading holds this wait (and the deferred retire tail
        behind it) until it reads again or its connection drops. On a serve worker's stop,
        uvicorn's ``timeout_graceful_shutdown`` still caps it. The close that ends the session
        therefore comes after the driver's answer is written. The sets are live, so a driver
        that engages during the wait is seen.
        """
        while True:
            requests = list(self.driving)
            tasks = list(self.driving_tasks)
            if not requests and not tasks:
                return
            for state in requests:
                await state.finished.wait()
                self.driving.discard(state)
            for task in tasks:
                await asyncio.wait([task])
                self.driving_tasks.discard(task)

    @property
    def answered(self) -> bool:
        """Whether no request of this generation is in flight and none of its work drives or waits on a lock."""
        return self._idle.is_set() and not self.engaged

    async def wait_answered(self) -> None:
        """Wait until this generation is :attr:`answered`.

        Covers the counted requests, the work driving a gated body and the work waiting on a
        recorded lock (uncounted while it waits, counted again once it moves); a drain-exempt
        stream is not counted, so it never holds the wait. Unbounded here, as
        :meth:`_wait_driving` is: the work's own bounds bound it.
        """
        while not self.answered:
            await self._idle.wait()
            await self._wait_driving()
            await self._wait_gate_waiters()

    def register_periodic_loop(self, cancel: Callable[[], Awaitable[None]]) -> None:
        """Register a cancel-and-await callback for a periodic loop this generation owns.

        Covers the failed-MCP reprobe, the marketplace advisories poll, and the
        conversations delivery sweep. Every registered loop is cancelled when the
        epoch retires, so a retired generation leaves no timer running against the
        fresh one.
        """
        self._periodic_cancels.append(cancel)

    async def _drain_in_flight(self, deadline: float, *, until_gate_wait: bool = False) -> bool:
        """Wait, bounded by ``deadline``, for this generation's admitted requests to finish.

        Counts only the work that can finish on its own: a request driving a gated body has
        released its slot (:func:`gate_party`), as has a drain-exempt stream, and a request
        waiting on a recorded lock is not counted while it waits. A request still in flight past
        the budget is logged loudly and the retire proceeds (the fresh epoch already serves new
        traffic). With ``until_gate_wait`` the wait also ends, unreported, as soon as work of
        this generation starts waiting on a recorded lock, and the call returns ``True``: the drain
        is not finished, and its caller either hands it to the deferred tail or resumes it.
        Returns ``False`` once the generation is idle or the budget is spent.
        """
        if self._idle.is_set():
            return False
        idle = asyncio.ensure_future(self._idle.wait())
        waits = [idle]
        if until_gate_wait:
            self._engagement.clear()
            waits.append(asyncio.ensure_future(self._engagement.wait()))
        try:
            done, _pending = await asyncio.wait(waits, timeout=deadline, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for wait in waits:
                wait.cancel()
        if not done:
            logger.error(
                "epoch %d retire: %d request(s) still in flight after the drain budget",
                self.number,
                self._in_flight,
            )
            return False
        return idle not in done and not self._idle.is_set()

    async def _cancel_periodic_loops(self, deadline: float) -> None:
        """Cancel every periodic loop this generation owns, each BOUNDED by ``deadline``.

        A loop's cancel awaits the loop task's cancellation unwind; if that unwind blocks
        — e.g. an outbound-HTTP poll (the marketplace advisories poll) closing its client
        mid-flight — an unbounded await would wedge this synchronous retire, and with it a
        door-driven reload's own request (an install/apply POST hanging to the client
        timeout). Bound each cancel: a loop that overruns is logged and abandoned (its task
        was already ``.cancel()``ed and dies in the background), so the retire always makes
        progress.
        """
        for cancel in self._periodic_cancels:
            # The cancel runs as its own task, so the bound holds even for a cancel that
            # suppresses ``CancelledError`` around its await of the loop task (it would
            # otherwise swallow the timeout's own cancellation and return as if done).
            unwind = asyncio.ensure_future(cancel())
            try:
                done, _pending = await asyncio.wait([unwind], timeout=deadline)
            except asyncio.CancelledError:
                unwind.cancel()
                raise
            if not done:
                unwind.cancel()
                logger.error(
                    "epoch %d retire: a periodic-loop cancel exceeded the %.1fs budget; abandoning it",
                    self.number,
                    deadline,
                )
                continue
            failure = unwind.exception()
            if failure is not None:
                logger.error("epoch %d retire: a periodic-loop cancel failed", self.number, exc_info=failure)


# -- process serving generation ------------------------------------------------

# The live serving generation and the ASGI dispatch's swap slot the build re-points.
# Established once (the epoch core by ``app_context``, the dispatch slot by the worker
# lifespan) and dropped at worker-lifespan exit.
_current: Epoch | None = None
_serving_slot: dict[str, Any] | None = None


# Builds the serving app on the transport the process serves (recorded by the worker
# lifespan with the boot serving app), so every rebuilt generation serves the transport
# the boot one does.
_serving_app_builder: Callable[[], ASGIApp] = epoch_defaults.default_streamable_http_app

# Whether the worker booted its streamable-HTTP transport stateless, recorded with the
# dispatch slot (``None`` while no boot serving app is attached). A streamable-HTTP sub-MCP
# app is built in the same mode, so a multi-worker deployment's sub-MCP sessions are never
# pinned to one worker.
_boot_stateless_http: bool | None = None

# The epoch a build is populating, for the span of ``build_and_swap_epoch``'s build
# step (``None`` otherwise). A per-epoch startup handler that spawns a periodic loop
# registers its cancel with THIS epoch, so the loop retires with the generation that
# started it rather than leaking onto the next — the build's new epoch is not yet the
# live ``_current`` during the build.
_building_epoch: Epoch | None = None


def current_epoch() -> Epoch:
    """The live serving generation, or a loud error before the worker lifespan installs the boot epoch.

    Epoch-scoped caches key on ``current_epoch().number``.
    """
    if _current is None:
        raise RuntimeError("no serving epoch is installed — enter the worker lifespan first")
    return _current


def current_epoch_or_none() -> Epoch | None:
    """The live serving generation, or ``None`` — the non-raising accessor.

    For a caller that runs outside a worker lifespan (an embedded read, a probe).
    """
    return _current


def epoch_under_construction() -> Epoch:
    """The epoch a build is populating, else the live epoch.

    The accessor a per-epoch startup handler uses to register a periodic loop's
    cancel with the generation that owns it. During ``build_and_swap_epoch``
    this is the new epoch (not yet ``_current``); at boot it falls back to the
    just-installed boot epoch.
    """
    return _building_epoch if _building_epoch is not None else current_epoch()


def epoch_under_construction_or_none() -> Epoch | None:
    """Like :func:`epoch_under_construction`, but ``None`` (never raising) when no epoch is installed.

    For a periodic-loop spawn that may run in a loop-less / pre-epoch context (a
    bare unit test), where there is no generation to register the loop with.
    """
    return _building_epoch if _building_epoch is not None else _current


def is_epoch_rebuild_in_progress() -> bool:
    """True only while ``build_and_swap_epoch`` is populating a new generation.

    True during a RELOAD, and never at cold boot or steady serving.
    (``_building_epoch`` is set only for the span of the build; cold boot
    installs its core through ``install_boot_core`` without it.) Lets a
    build-time step (the MCP viability probe) pick a short reload budget over the
    generous cold-boot one, so an unreachable server can't stall a live reload.
    Read safely from the off-loop build worker: it is a GIL-atomic reference read
    of a process global.
    """
    return _building_epoch is not None


def install_boot_core(core: ServingCore) -> Epoch:
    """Establish the boot serving generation's core, called once by ``app_context`` before ``start()`` runs.

    So ``start()`` and the epoch handlers register into this generation's
    FastMCP, and every process-spine read resolves to it. The dispatch slot +
    serving app are attached later by the worker lifespan
    (:func:`attach_boot_serving_app`); an embedded / pure-``app_context`` caller
    needs no serving app. The boot epoch's number is the current client epoch.
    """
    # The applied stored-env set (``applied_env.applied_env_keys``) is owned by the cold-boot
    # env bridge, which ran BEFORE this and populated it with the stored-env keys it applied;
    # a later reload's removed-key reconciliation depends on those keys, so this must NOT
    # reset it. Between contexts in one process ``clear_epoch`` clears it at teardown.
    global _current
    _current = Epoch(number=current_client_epoch(), core=core)
    return _current


def attach_boot_serving_app(
    serving_app: ASGIApp,
    app_state: dict[str, Any],
    build_serving_app: Callable[[], ASGIApp],
    *,
    stateless_http: bool,
) -> None:
    """Point the ASGI dispatch slot at the boot serving app.

    Called by the worker lifespan after it builds the serving app and enters its FastMCP
    lifespan. Records the slot the build+swap primitive re-points,
    ``build_serving_app``, which builds a serving app on the same transport (SSE, or
    stateful or stateless streamable HTTP) for every rebuilt generation, and whether that
    transport is stateless (:func:`boot_stateless_http`).
    """
    global _serving_slot, _serving_app_builder, _boot_stateless_http
    _serving_slot = app_state
    _serving_app_builder = build_serving_app
    _boot_stateless_http = stateless_http
    current_epoch().serving_app = serving_app
    app_state["app"] = serving_app


def boot_stateless_http() -> bool | None:
    """Whether the worker booted its streamable-HTTP transport stateless.

    ``None`` while no boot serving app is attached (an embedded ``app_context`` caller
    serves no transport), which leaves a streamable-HTTP build to fastmcp's own
    ``stateless_http`` setting.
    """
    return _boot_stateless_http


def serving_app_builder() -> Callable[[], ASGIApp]:
    """The builder every rebuilt generation builds its serving app with: the boot one's transport.

    The default streamable-HTTP builder while no boot serving app is attached.
    """
    return _serving_app_builder


async def clear_epoch() -> None:
    """Drop the process serving generation, closing its FastMCP lifespan and adopted sub-app lifespans.

    Terminating its transports so a later lifespan in the same process starts
    from a clean slate.
    """
    global _current, _serving_slot, _serving_app_builder, _boot_stateless_http
    current = _current
    if current is not None:
        await current.close_transports()
    _current = None
    _serving_slot = None
    _serving_app_builder = epoch_defaults.default_streamable_http_app
    _boot_stateless_http = None
    clear_applied_env()
    unbind_respawn_class_env()
    detach_retired_generations()


def _swap(new_epoch: Epoch) -> Epoch:
    """Publish the new generation atomically.

    The current-epoch pointer and the ASGI dispatch slot flip together, so no
    request ever sees a half-swapped surface.
    """
    global _current
    old = _current
    if old is None:
        raise RuntimeError("cannot swap epochs before the boot epoch is installed")
    _current = new_epoch
    if _serving_slot is not None:
        _serving_slot["app"] = new_epoch.serving_app
    return old


async def _outside_any_request[T](step: Callable[[], Awaitable[T]]) -> T:
    """Await ``step`` in a task that starts from no request's context.

    A task copies the context it is created in. A door-driven reload runs inside the
    request that drove it, whose context carries that request (and through its scope the
    retiring generation's app) and its caller's identity. The new generation's long-lived
    tasks — its FastMCP lifespan supervisor and its periodic loops — are created in the
    steps run through here, so they start from no request's context, the same on every
    door. The step is still the re-import's own work, so it starts from the fork gate's
    :meth:`~tai42_kit.fork_gate.ForkGate.rebuild_context`: a fork it makes while the rebuild
    holds the gate is the rebuild's own and does not wait on it. The tasks it creates outlive
    the rebuild and the job that may have driven it, so the context carries no job span:
    a span or a fork they make later is one of their own.
    """

    async def _step() -> T:
        return await step()

    return await asyncio.create_task(_step(), context=fork_gate.rebuild_context())


@dataclass
class _EpochBuild:
    """A generation built off to the side: the epoch, its serving handle, and what restores the live state."""

    epoch: Epoch
    serving_app: ASGIApp
    settings_mark: Any
    env_snapshot: dict[str, str]
    loaded_before: set[str]


async def _build_epoch(
    proposed_env: Mapping[str, str],
    ownership: DeploymentOwnership,
    rebuild: Callable[[], Any] | None,
    build_serving_app: Callable[[Epoch], Awaitable[ASGIApp]] | None,
) -> _EpochBuild:
    """Build a fresh generation under ``proposed_env`` OFF TO THE SIDE, with zero mutation of the live epoch.

    Snapshots ``os.environ``, applies ``proposed_env`` (removed-key reconciled) and clears the
    settings accessor cache so the build reads it, opens a staged generation for every
    per-generation global, then runs ``rebuild`` and ``build_serving_app``. All of it runs
    before the client epoch advances. A failure is discarded (:func:`_discard_build`) with the
    ERROR line and re-raised; the applied env stays live on success, for the caller's ending.
    """
    from tai42_skeleton.app.registry_staging import begin_staging_all

    rebuild = rebuild or epoch_defaults.default_rebuild
    build_serving_app = build_serving_app or epoch_defaults.default_build_serving_app

    snapshot = dict(os.environ)
    loaded_before = set(applied_env_keys())

    global _building_epoch
    new_epoch = Epoch(number=current_client_epoch())
    _building_epoch = new_epoch
    try:
        apply_env_and_reset_settings(proposed_env, ownership)
        # Every settings instance read from here on is read under the proposed env for the
        # generation being built; the mark lets a commit stamp them with its epoch.
        settings_mark = settings_birth_mark()
        # Open a staged generation for every per-generation global, so ``start()`` and the
        # epoch handlers populate the generation being built and the live epoch's globals
        # stay untouched until a commit.
        begin_staging_all()
        rebuild()
        serving_app = await _outside_any_request(lambda: build_serving_app(new_epoch))
        if new_epoch._surface is None:
            new_epoch.record_serving_surface(serving_app)
    except BaseException:
        logger.error(
            "epoch build failed under the proposed env — discarded; the previous epoch keeps serving",
            exc_info=True,
        )
        await _discard_build(new_epoch, snapshot, loaded_before)
        raise
    finally:
        _building_epoch = None
    return _EpochBuild(new_epoch, serving_app, settings_mark, snapshot, loaded_before)


async def _discard_build(new_epoch: Epoch, snapshot: dict[str, str], loaded_before: set[str]) -> None:
    """Discard a generation built off to the side; the live epoch keeps serving with zero mutation.

    Drops every staged generation and re-derives the process caches from the untouched
    committed state: the staged registrations are discarded, the env is restored
    (:func:`~tai42_skeleton.app.applied_env.restore_env`), the settings accessor cache is cleared,
    and the route index is re-derived from the live route registry (a partial reimport may have
    added routes — the spine route registry dedups and is never rolled back). A lifespan the
    build entered belongs to the discarded generation and is closed with it.
    """
    from tai42_skeleton.access_control.role_gate import reset_route_index
    from tai42_skeleton.app.registry_staging import abort_staging_all
    from tai42_skeleton.tools.binding.surface import bump_tool_surface_generation

    abort_staging_all()
    # The discarded build's core is dropped, so the listed tool set reverts to the live
    # core's: a cache filled from the building core during the build never matches again.
    bump_tool_surface_generation()
    restore_env(snapshot, loaded_before)
    reset_all_settings()
    reset_route_index()
    await new_epoch.close_transports()


async def build_and_discard_epoch(
    proposed_env: Mapping[str, str],
    *,
    ownership: DeploymentOwnership,
    rebuild: Callable[[], Any] | None = None,
    build_serving_app: Callable[[Epoch], Awaitable[ASGIApp]] | None = None,
) -> None:
    """Prove ``proposed_env`` builds a serving epoch, then discard the build; the live epoch is untouched.

    The ending a process that is not a bus member gives a profile apply's build (a job child a
    backend worker forked has no serving surface to swap into): the same build
    :func:`build_and_swap_epoch` runs, discarded on success exactly as a failed build is, so the
    env is proven to build before the caller persists it. A failure is discarded, logged at
    ERROR and re-raised; a success is logged at INFO.
    """
    build = await _build_epoch(proposed_env, ownership, rebuild, build_serving_app)
    await _discard_build(build.epoch, build.env_snapshot, build.loaded_before)
    logger.info("epoch built under the proposed env and discarded: this process is not a bus member and swaps nothing")


async def build_and_swap_epoch(
    proposed_env: Mapping[str, str],
    *,
    ownership: DeploymentOwnership,
    bring_credentials_up_to_date: Callable[[], Awaitable[Any]] | None = None,
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

    - first, on the still-free serving loop and under the live settings, bring up to date the credentials
      the build's listing needs (``bring_credentials_up_to_date``): the build blocks the loop it refreshes on;
    - snapshot ``os.environ``, apply ``proposed_env`` (removed-key reconciled), and
      clear the accessor cache with ``reset_all_settings`` so the build reads it;
    - build the new serving surface OFF TO THE SIDE — ``rebuild`` re-initialises the
      registries, ``build_serving_app`` produces this generation's fresh dispatch
      handle and may record its serving surface on the epoch (when it records none, the
      handle it returns is the surface) — with ZERO mutation of the live epoch;
    - SUCCESS: advance the client epoch, stamp the settings the build read with it, swap
      the dispatch slot and the current-epoch pointer together, retire the previous
      generation (:func:`~tai42_skeleton.app.epoch_retire.retire_epoch`: periodic loops
      cancelled, in-flight work + supervisors drained, transports closed once the work
      that drives or waits on the reload gate has answered, retired pools closed, and its
      settings swept for stale-config leaks once its serving surface is collected),
      and establish the new generation's loop-affine background loops on THIS serving
      loop (registered with the new epoch). The applied env STAYS live; the caller
      persists it;
    - FAILURE: the half-built epoch is DISCARDED (a lifespan it entered is closed),
      ``os.environ`` is restored EXACTLY from the snapshot (restore-on-failure only),
      the old epoch keeps serving untouched, and the build failure is re-raised loudly.

    The ``bring_credentials_up_to_date`` / ``rebuild`` / ``build_serving_app`` / ``establish_background_loops``
    seams default to the running app; injectable so the build/swap/discard contract is exercised in isolation.
    """
    from tai42_skeleton.app.registry_staging import commit_staging_all

    await (bring_credentials_up_to_date or epoch_defaults.default_bring_credentials_up_to_date)()

    establish_background_loops = establish_background_loops or epoch_defaults.default_establish_background_loops
    build = await _build_epoch(proposed_env, ownership, rebuild, build_serving_app)
    new_epoch, serving_app, settings_mark = build.epoch, build.serving_app, build.settings_mark

    retired = advance_client_epoch()
    new_epoch.number = current_client_epoch()
    # The build read its settings before its epoch was current; they belong to the new
    # generation, so the retire's sweep of ``retired`` judges only the previous generation's.
    restamp_settings_born_after(settings_mark)
    new_epoch.serving_app = serving_app
    # Promote every staged generation to committed in the same no-await stretch as the
    # dispatch-slot swap, so no request ever sees a mix of the old and new generations.
    commit_staging_all()
    old_epoch = _swap(new_epoch)
    await retire_epoch(old_epoch, retired, drain_deadline)
    # Post-swap, ON the serving loop: (re)establish this generation's loop-affine
    # background loops and register them with the now-current epoch. Runs AFTER the
    # retire cancelled the previous generation's loops, so there is no cross-generation
    # overlap and no timer outlives its epoch.
    await _outside_any_request(establish_background_loops)
    return new_epoch
