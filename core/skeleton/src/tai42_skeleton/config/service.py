"""The single manifest-mutation pipeline.

Every manifest / env mutation crosses one pipeline so no writer can forget a step:

    transaction → read → mutate → VALIDATE → SEAL → persist → local reload → broadcast

:class:`ConfigService` exposes three entrypoints — :meth:`apply_change` (a
read-modify-write through the config manager's transaction), :meth:`apply_replace`
(a whole-document replace), and :meth:`apply_env_change` (an env override) — and
each returns a structured :class:`ApplyResult` carrying the persisted document
(where applicable), the local reload result, and the awaited per-worker fleet
report.

Validation runs on the RESOLVED projection of the change (``!ENV`` markers
materialized in memory for validation ONLY — the PRESERVED document is what
persists) and rejects an invalid change before anything is persisted. The
backend-needs-bus invariant runs in the same step, in both directions and in
agreement with the boot/reload-time rule.

The SEAL step (:meth:`ConfigService.apply_change` /
:meth:`ConfigService.apply_replace`, never the env change) then guarantees no
resolved secret bakes to disk: because the only read surface for the manifest's
``mcp`` section is the RESOLVED view, a natural round-trip (read the resolved view,
edit, post it back) hands the pipeline resolved secret values. Before the document
persists, :func:`~tai42_skeleton.config.secret_seal.seal_resolved_secrets` retags any
leaf that still equals a currently-resolved secret back to its ``!ENV`` marker, and
refuses (a :class:`~tai42_skeleton.config.secret_seal.ResolvedSecretError`, a
``ValueError`` the operations layer maps to a 400) any stranded resolved secret with
no marker origin — so a resolved round-trip preserves the operator's markers and a
plaintext secret never reaches the store.

The broadcast tail is the whole fleet, always: ``publish({"op": "reload_config"},
targets=None, local=<local reload result>)`` — a persisted change reaches every
worker, and each subscriber re-reads the persisted store itself. An unconfirmed
worker is a loud ERROR log and an explicit entry in the report, but the call still
succeeds because persist + local reload landed; recovery is the fleet-reload door.
Once the persist has committed the contract is structural: EVERY subsequent failure
surfaces as a :class:`~tai42_skeleton.operations._broadcast.FleetBroadcastError`
carrying a fleet report, never a raw exception. If the LOCAL reload raises after the
persist landed, the broadcast STILL goes out (siblings converge on the persisted
state) and the call re-raises the local failure with the fleet report attached; if
the broadcast itself raises anything the bus does not fold into a returned
bus-unreachable report, that too is re-raised as a ``FleetBroadcastError`` with the
unreachable report shape — a stranded fleet never hides behind a local error, and a
committed persist never escapes as a raw broadcast fault.
"""

from __future__ import annotations

import asyncio
import copy
import os
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, cast

from pyaml_env import parse_config
from tai42_kit.fork_gate import fork_gate
from tai42_kit.llm.checkpoint.checkpoint_registry import checkpoint_registry
from tai42_kit.llm.store.store_registry import store_registry
from tai42_kit.utils.data import dump_manifest

from tai42_skeleton.app.boot_rules import check_backend_needs_bus
from tai42_skeleton.app.bus import FleetResult, LocalApplyResult, OpOutcome, WorkerBus, WorkerIdentity
from tai42_skeleton.app.bus_settings import BusSettings, bus_settings
from tai42_skeleton.app.epoch import Epoch, build_and_swap_epoch
from tai42_skeleton.app.recycle import RecycleReport, orchestrate_recycle
from tai42_skeleton.app.reload_gate import FORK_QUIESCE_SECONDS, reload_gate
from tai42_skeleton.config.boundary import (
    refuse_dangling_env_markers,
    refuse_incomplete_admin_pair,
    refuse_key_material,
    refuse_x_band,
    reload_class_by_env_var,
    x_band_env_keys,
)
from tai42_skeleton.config.recycle_policy import CENSUS_TARGET_KINDS, CapabilityReport, capability_report
from tai42_skeleton.config.secret_seal import seal_resolved_secrets
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.operations._broadcast import FleetBroadcastError, fleet_fanout, log_non_convergence

if TYPE_CHECKING:
    from collections.abc import Awaitable, Iterator

    from ruamel.yaml.comments import CommentedMap


class OrphanEnvWriteError(RuntimeError):
    """A combined env+manifest op whose manifest persist FAILED after the env
    write already landed.

    Env-first/manifest-second ordering: on a manifest-persist failure there is NO
    rollback — the env write STANDS as an inert, re-runnable orphan (rolling it back
    could leave a still-referenced marker resolving silently to ``"N/A"``). The message
    NAMES the orphan env key(s) and the manifest pointer they now reference no persisted
    marker at, and states the env write stands / re-run to complete. A ``RuntimeError``
    (not a ``ValueError``) so the op layer does not fold it into a 400 — a partial
    failure is loud, not a client input error."""


class _ManifestStore(Protocol):
    """The config-manager surface the pipeline drives — the transactional seams plus
    the reads the env-change validation needs. The concrete provider is the active
    :class:`~tai42_contract.config.manager.ConfigManager`."""

    def mutate_manifest(self, mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]: ...

    def replace_manifest(self, document: dict[str, Any]) -> dict[str, Any]: ...

    def write_env(self, config: dict[str, str]) -> None: ...

    def replace_env(self, config: dict[str, str]) -> None: ...

    def read_env(self) -> dict[str, str]: ...

    def read_manifest_preserved(self) -> dict[str, Any]: ...


class _ReloadAdmin(Protocol):
    """The reload seam the pipeline drives on the local worker after a persist."""

    def reload_config(self) -> dict[str, Any]: ...


class _FleetPublisher(Protocol):
    """The worker-bus publish surface the pipeline broadcasts through."""

    async def publish(
        self, op: dict[str, Any], targets: list[str] | None, local: LocalApplyResult | None
    ) -> FleetResult: ...


@dataclass(frozen=True)
class ApplyResult:
    """The structured outcome of one pipeline run.

    ``document`` is the persisted PRESERVED-view manifest (``!ENV`` markers intact)
    for :meth:`ConfigService.apply_change` / :meth:`ConfigService.apply_replace`, and
    ``None`` for :meth:`ConfigService.apply_env_change` (no manifest document changed).
    ``local`` is this worker's local reload result. ``fleet`` is the awaited per-worker
    broadcast report, which carries the two honest failure shapes itself — a reachable
    bus with an unconfirmed worker named in ``fleet.results`` (``fleet.ok`` is then
    ``False``), or a bus-unreachable result (``fleet.reachable`` is ``False`` with only
    ``fleet.error``). Both are returned successes: persist + local reload landed, and
    recovery is a fleet reload.
    """

    fleet: FleetResult
    local: dict[str, Any]
    document: dict[str, Any] | None = None

    @property
    def fanout(self) -> dict[str, Any]:
        """The mode-wrapped fan-out summary of this run's broadcast — the same shape
        :func:`~tai42_skeleton.operations._broadcast.apply_response` embeds under
        ``fanout`` (local-only / fleet / unreachable). A writer that returns a bare
        result (not the ``apply_response`` merge) embeds this value directly."""
        return fleet_fanout(self.fleet)


@dataclass(frozen=True)
class ProfileApplyOutcome:
    """The structured outcome of one profile-APPLY pipeline run — the raw material the
    operations layer folds into the dedicated ``profileApplyResponse``.

    ``hot`` is the hot-class diff key NAMES (names only — never values). ``recycle`` is
    the fleet recycle report (``None`` when the diff carried no recycle-class key, so no
    recycle rolled), each row carrying its own kind. ``self_identity`` is the applying
    worker's own identity (name + generation) — the source for the self-deferred row's
    ``generation_before``. ``serve_affecting`` is whether the applier must self-exit (arm
    the post-response graceful exit). ``fleet`` is the reload broadcast report.
    """

    hot: list[str]
    recycle: RecycleReport | None
    self_identity: WorkerIdentity
    serve_affecting: bool
    fleet: FleetResult


async def _release_llm_pools() -> None:
    """Close the loop-bound langgraph checkpoint + store resource pools so a following
    ``build_and_swap_epoch`` can reset settings: its resource-registry reset REFUSES to
    drop a per-loop registry still holding live resources on a running loop. The default
    ``release_llm_pools`` seam of :meth:`ConfigService.apply_replace_env`, mirroring
    :meth:`AppLifecycle._reload_config` (the other ``build_and_swap_epoch`` caller). Must
    run on the serving loop that owns the registries (the apply holds the reload gate on
    that loop)."""
    await checkpoint_registry().close_all()
    await store_registry().close_all()


class ConfigService:
    """The one pipeline every manifest / env mutation crosses.

    Constructed per call site from the running app via :meth:`from_app`, or directly
    with the config manager, the reload seam, and the worker bus for testing.
    """

    # Process-wide env-write lock. ``from_app`` builds a FRESH ConfigService per
    # call, so this is CLASS-level (not per-instance) — otherwise concurrent combined-op
    # calls would each hold their own lock and never serialize. It serializes the combined
    # op's READ→derive→WRITE span so the secret-marks append (and the generated-key
    # collision check) cannot race op-vs-op — the lost-append the provider's per-call lock
    # cannot close (``read_env`` sits OUTSIDE that lock). Lazily bound to the running serving
    # loop and rebound on a loop swap (mirrors :class:`ReloadGate`), so it is correct across
    # the process's single serving loop and across per-test loops. It is HELD strictly before
    # ``_reload_and_broadcast`` (which acquires ``reload_gate``) and released first, so the
    # two locks never nest — no deadlock.
    _env_write_lock: ClassVar[asyncio.Lock | None] = None
    _env_write_loop: ClassVar[asyncio.AbstractEventLoop | None] = None

    def __init__(self, config_manager: _ManifestStore, admin: _ReloadAdmin, bus: _FleetPublisher) -> None:
        self._config_manager = config_manager
        self._admin = admin
        self._bus = bus

    @classmethod
    def _env_lock(cls) -> asyncio.Lock:
        """The process-wide env-write lock bound to the running serving loop, created on
        first use and rebound only if the running loop differs from the bound one (a loop
        swap — there is never a second live loop in the same process). Mirrors
        :meth:`ReloadGate._serving_lock`, keeping the singleton correct across loops rather
        than raising the cross-loop ``RuntimeError`` a bare module-level ``asyncio.Lock``
        would across test loops."""
        loop = asyncio.get_running_loop()
        if cls._env_write_lock is None or cls._env_write_loop is not loop:
            cls._env_write_lock = asyncio.Lock()
            cls._env_write_loop = loop
        return cls._env_write_lock

    @classmethod
    def from_app(cls) -> ConfigService:
        """Wire the pipeline from the running app: the active config manager, the admin
        reload seam, and this process's worker bus."""
        from tai42_contract.app import tai42_app

        from tai42_skeleton.app import instance

        return cls(config_manager=tai42_app.config.config_manager, admin=tai42_app.admin, bus=instance.app.bus)

    async def apply_change(self, mutator: Callable[[dict[str, Any]], None]) -> ApplyResult:
        """Read-modify-write: run ``mutator`` on the PRESERVED manifest inside the
        config-manager transaction, VALIDATE the resolved projection of the mutated
        document, persist, locally reload, and broadcast to the whole fleet.

        ``mutator`` edits the passed document IN PLACE and must be pure / re-runnable
        (the transaction may re-run it on a concurrency conflict). An invalid mutation
        raises inside the transaction, so nothing is persisted. The mutated document
        is SEALED against the pre-mutation document before it persists, so a mutator
        that ingests resolved secret values (a resolved round-trip) cannot bake a
        secret to disk — see :meth:`_seal_secrets`."""

        def guarded(document: dict[str, Any]) -> None:
            # Snapshot the preserved document the mutator received BEFORE it runs, so
            # the seal can retag the mutated document against the current manifest's
            # markers and their resolved values.
            current = copy.deepcopy(document)
            mutator(document)
            self._validate_manifest(document)
            self._seal_secrets(document, current)

        persisted = self._config_manager.mutate_manifest(guarded)
        return await self._reload_and_broadcast(document=persisted)

    async def apply_replace(self, document: dict[str, Any]) -> ApplyResult:
        """Replace the whole manifest: VALIDATE the resolved projection of ``document``
        BEFORE it is persisted (a replace has no mutator to abort), then replace,
        locally reload, and broadcast to the whole fleet.

        The caller supplies the PRESERVED-view document (``!ENV`` markers, never
        resolved values) — the seam persists it verbatim. The document is SEALED
        against the CURRENT persisted manifest before it persists, so a replacement
        carrying a resolved secret (a resolved round-trip) is retagged or refused —
        see :meth:`_seal_secrets`."""
        self._validate_manifest(document)
        self._seal_secrets(document, self._read_preserved_manifest())
        persisted = self._config_manager.replace_manifest(document)
        return await self._reload_and_broadcast(document=persisted)

    async def apply_env_change(self, changes: dict[str, str]) -> ApplyResult:
        """Apply env overrides: VALIDATE the effective/resolved config (the manifest's
        ``!ENV`` markers materialized against the post-change env) through the SAME
        backend-needs-bus gate, then merge the overrides, locally reload, and broadcast
        to the whole fleet. An invalid effective config raises before anything is
        written."""
        self._validate_env(changes)
        self._config_manager.write_env(changes)
        return await self._reload_and_broadcast(document=None)

    async def apply_env_and_change(
        self,
        prepare: Callable[[dict[str, str]], Awaitable[tuple[dict[str, str], Callable[[dict[str, Any]], None]]]],
        *,
        manifest_pointer: str | None = None,
    ) -> ApplyResult:
        """Combined env-write + manifest-mutate as ONE consistent unit.

        The ``set_mcp_secret_env`` door writes a secret VALUE to the env store and a
        matching ``!ENV ${KEY}`` MARKER into the manifest; the two must stay consistent.
        ``prepare`` receives the CURRENT stored env (read once under the lock) and
        returns ``(changes, mutator)`` — it derives the generated/explicit key, the
        appended secret marks, and the marker-writing mutator FROM that snapshot. Running
        it inside the lock is what makes the read→derive→write span atomic.

        Ordering keeps env + manifest consistent, all under the env-write lock so
        concurrent combined ops cannot interleave their read→write (a lost secret-marks
        append, or two ops minting the same generated key):

        0. Acquire the process-wide env-write lock, read the stored env ONCE, and call
           ``prepare(stored)`` to build the changes + mutator against that snapshot.
        1. Build the candidate mutated manifest and validate it against the effective
           env — a failure raises before any write.
        2. Write the env FIRST. Env-first (not manifest-first) guarantees there is never
           a persisted ``!ENV`` marker referencing an env key that is not yet stored (a
           marker that would silently resolve to ``"N/A"``); the reverse ordering would.
        3. Persist the manifest with the PURE ``mutator`` — it only edits the passed
           document, so the k8s optimistic-concurrency REPLAY (re-read + re-run on a
           409) is side-effect-free and never double-writes the env, which is not
           inside the replayed span. Partial-failure contract: a manifest-persist
           failure (retries exhausted) performs **NO rollback** — the env write STANDS
           (inert, re-runnable orphan) — and raises :class:`OrphanEnvWriteError` naming
           the orphan env key(s) and the manifest pointer. Rolling the env back would
           risk leaving a still-referenced marker resolving silently to ``"N/A"``;
           re-running the op completes the manifest write against the standing env.
        4. RELEASE the env-write lock, THEN locally reload and broadcast to the whole
           fleet. The reload acquires ``reload_gate``; the env-write lock is released
           first so the two never nest (no deadlock) — the read→write span is entirely
           before the reload gate.

        ``manifest_pointer`` is the caller's target pointer, named in the orphan report
        when it is known (``set_mcp_secret_env`` supplies it).
        """
        # STEP 0-3 run under the env-write lock: the stored read, the caller's derive,
        # and both persists are one atomic span so a concurrent combined op cannot read the
        # same marks/keys and clobber this op's write. The reload tail (STEP 4) is OUTSIDE
        # the lock so ``reload_gate`` is never acquired while this lock is held.
        async with self._env_lock():
            # STEP 0 — read the stored env ONCE under the lock; the caller derives the
            # changes + mutator from it (the key generation and the secret-marks append are
            # both read-then-write against this snapshot). It is also the snapshot the
            # orphan-key report diffs against on a manifest failure.
            stored = self._read_stored_env()
            changes, mutator = await prepare(stored)

            # STEP 1 — validate the combined change against the post-change effective env
            # (mutated manifest, X-band payload, dangling markers, backend-needs-bus). The
            # candidate is built OUTSIDE the store so the effective-env-dependent checks run
            # BEFORE anything persists (the k8s transaction cannot resolve markers against the
            # not-yet-live env, so validation cannot move inside it).
            candidate = copy.deepcopy(self._read_preserved_manifest())
            mutator(candidate)
            self._validate_env_and_manifest(changes, candidate)

            # STEP 2 — env-write-FIRST. Env-first (not manifest-first) guarantees there is
            # never a persisted ``!ENV`` marker referencing an env key not yet stored.
            self._config_manager.write_env(changes)

            # STEP 3 — persist the manifest with a guarded, PURE, re-runnable mutator: it seals
            # the mutated document against its pre-mutation snapshot before persist (parity with
            # ``apply_change`` — a mutator can never bake a resolved secret to disk), and it edits
            # only the passed document, so the k8s optimistic-concurrency REPLAY (re-read + re-run
            # on a 409) is side-effect-free and never double-writes the env, which is outside the
            # replayed span. A persist failure performs NO rollback — the env write STANDS as
            # an inert, re-runnable orphan — and raises loudly naming the orphan key(s) + pointer.
            def guarded(document: dict[str, Any]) -> None:
                current = copy.deepcopy(document)
                mutator(document)
                self._seal_secrets(document, current)

            try:
                persisted = self._config_manager.mutate_manifest(guarded)
            except Exception as exc:
                # The orphans are the keys whose written value differs from the pre-write
                # snapshot; on an idempotent re-send (nothing actually changed) fall back to
                # the full written key set so the report is NEVER blank.
                orphans = sorted(key for key, value in changes.items() if stored.get(key) != value)
                named = orphans or sorted(changes)
                label = ", ".join(named) if named else "the just-written env keys"
                pointer = f" (intended manifest pointer {manifest_pointer!r})" if manifest_pointer else ""
                raise OrphanEnvWriteError(
                    "Combined env+manifest op: the env write LANDED but the manifest persist FAILED "
                    "(retries exhausted). NO rollback performed — the env write STANDS (inert, "
                    f"re-runnable). Orphan env key(s) now referencing no persisted manifest marker: "
                    f"{label}{pointer}. Re-run the op to complete the manifest write."
                ) from exc

        # STEP 4 — lock RELEASED; reload + broadcast (acquires reload_gate) runs outside it.
        return await self._reload_and_broadcast(document=persisted)

    # -- Profile apply (C5) ----------------------------------------------------

    async def apply_replace_env(
        self,
        profile_env: dict[str, str],
        *,
        driven: bool,
        save_previous: Callable[[dict[str, str]], Awaitable[None]],
        build_and_swap: Callable[..., Awaitable[Epoch]] = build_and_swap_epoch,
        orchestrate: Callable[..., Awaitable[RecycleReport]] = orchestrate_recycle,
        release_llm_pools: Callable[[], Awaitable[None]] = _release_llm_pools,
    ) -> ProfileApplyOutcome:
        """Apply a settings profile: the C5 env-write-LAST reload pipeline.

        Ordered so a failed build leaves the STORE untouched and the old surface serving
        (``os.environ`` restored exactly by :func:`build_and_swap_epoch`):

        1. ``_validate_replace`` (X-band payload refusal + dangling ``!ENV`` + backend-
           needs-bus); diff the proposed env against the stored env; refuse a recycle-
           class diff the deployment shape cannot carry; read the applier's own identity.
        2. Snapshot the current stored env as the reserved ``@previous`` version.
        3. ``build_and_swap_epoch(profile_env, drain_tolerate_driver=driven)`` holding the
           reload gate — ``driven`` excuses THIS door's own still-admitted request from
           the retire drain (else it self-deadlocks); a build failure re-raises and
           this method returns nothing persisted.
        4. ``replace_env`` — the LAST env write, reached only after a successful build.
        5. Broadcast the reload to the fleet, then roll a recycle across it for the
           recycle-class diff (the applier's own recycle is a deferred self-exit).

        ``build_and_swap`` / ``orchestrate`` default to the running primitives and are
        injectable so the ordering / drain / recycle contract is exercised in isolation.
        """
        # STEP 1 — validate the whole-env replace (payload-scoped X-band refusal, dangling
        # !ENV, backend-needs-bus). A failure raises before anything is snapshotted/built.
        self._validate_replace(profile_env)

        # STEP 2 — diff the proposed env vs the current stored env; classify each diff key
        # by its live-registry reload_class (names only — never values).
        stored = self._read_stored_env()
        diff_keys = _replace_diff_keys(stored, profile_env)
        reload_classes = reload_class_by_env_var()
        hot = sorted(key for key in diff_keys if reload_classes.get(key, "hot") == "hot")
        recycle_diff_keys = sorted(key for key in diff_keys if reload_classes.get(key) == "recycle")

        # STEP 1b — refuse a recycle-class diff this deployment shape cannot carry, upfront
        # and naming the key, so an unappliable profile never reaches the build.
        report = capability_report()
        _refuse_unrecyclable(diff_keys, recycle_diff_keys, report)

        # "Serve-affecting" is UNDEFINED in code (design seam 2): the CONSERVATIVE default
        # is that ANY recycle-class diff on a recycle-supported shape affects the serve
        # surface, so the applier self-exits. This over-fires on a purely backend-only diff
        # (an unnecessary serve respawn) but always converges. (Bare shape was refused above,
        # so a non-empty recycle diff here is always on a supported shape.)
        serve_affecting = bool(recycle_diff_keys)

        # STEP 1c — the applying worker's own identity (name + generation), the source
        # for the self-deferred row's ``generation_before`` and the recycle exclusion.
        bus = cast("WorkerBus", self._bus)
        self_identity = bus.identity

        # STEP 2 (persist reserved snapshot) — save the CURRENT stored env as @previous
        # BEFORE the swap. A later failed build leaves it harmlessly reflecting the
        # unchanged live state.
        await save_previous(stored)

        # STEP 3 — build a fresh epoch under the proposed env and swap it in atomically.
        # HOLD the reload gate across the swap: the op is reload_gated (which only rejects
        # a CONCURRENT reload at the route edge), but a direct build_and_swap needs the
        # serving-loop-owned lock HELD. ``drain_tolerate_driver=driven`` excuses this
        # door's own admitted request from the retire's in-flight drain; a
        # build failure re-raises here (env restored, store untouched) — STEP 4 never runs.
        #
        # HOLD THE FORK GATE TOO. This door drives ``build_and_swap`` directly rather than
        # through ``reload_gate.run``, so it does not inherit that path's fork exclusion —
        # yet its rebuild re-imports the manifest modules exactly the same way. A backend
        # work loop that forks a job child mid-reimport leaves the child deadlocked on an
        # inherited ``importlib`` per-module lock. The async façade holds the gate on a
        # dedicated thread so ownership stays single-threaded and this loop is never
        # blocked while job spans drain.
        async with reload_gate.lock, fork_gate.exclusive_async(timeout=FORK_QUIESCE_SECONDS):
            # Release the loop-bound langgraph checkpoint/store pools BEFORE the build's
            # settings reset drops their per-loop registries: build_and_swap_epoch opens
            # with ``reset_all_settings()``, whose resource-registry reset REFUSES to drop
            # a registry still holding live resources on a running loop (a prior agent run
            # leaves a cached checkpoint saver behind). This is the same release
            # AppLifecycle._reload_config performs before ITS build_and_swap_epoch; the
            # profile-apply path missed it, so an apply following any LLM run 500'd.
            await release_llm_pools()
            await build_and_swap(profile_env, drain_tolerate_driver=driven)

        # STEP 4 — env-write-LAST: persist the proposed env as the WHOLE stored map only
        # after the build succeeded. A failed build never reaches this line.
        self._config_manager.replace_env(profile_env)

        # STEP 5 — broadcast the reload to the fleet (each sibling re-reads the persisted
        # store), then roll a recycle for the recycle-class diff. The applier's own recycle
        # is deferred to the post-response self-exit, reported as the applier self-entry.
        fleet = await self._broadcast_profile_reload()
        recycle_report: RecycleReport | None = None
        if recycle_diff_keys:
            recycle_report = await orchestrate(
                bus,
                excluded_name=self_identity.name,
                applier_generation=self_identity.generation,
                target_kinds=CENSUS_TARGET_KINDS,
                applier_self_deferred=serve_affecting,
                step_timeout=_recycle_step_timeout(),
            )

        return ProfileApplyOutcome(
            hot=hot,
            recycle=recycle_report,
            self_identity=self_identity,
            serve_affecting=serve_affecting,
            fleet=fleet,
        )

    async def _broadcast_profile_reload(self) -> FleetResult:
        """Broadcast the profile apply's reload to the whole fleet AFTER the local swap
        already landed (the swap IS this worker's local apply, so no second local reload
        runs here). Mirrors the post-persist contract: a raw broadcast failure surfaces as
        a :class:`FleetBroadcastError` carrying the honest bus-unreachable report, never a
        raw exception, so the committed env change is never hidden behind a broadcast
        fault."""
        local = LocalApplyResult(outcome=OpOutcome.applied, payload={"status": "ok"})
        try:
            report = await self._bus.publish({"op": "reload_config"}, None, local)
        except Exception as broadcast_error:
            error = f"{type(broadcast_error).__name__}: {broadcast_error}"
            report = FleetResult(op="reload_config", reachable=False, error=error)
            raise FleetBroadcastError("reload_config", report, broadcast_error) from broadcast_error
        log_non_convergence(report)
        return report

    # -- Validation ------------------------------------------------------------

    def _validate_manifest(self, document: Mapping[str, Any]) -> None:
        """Validate the resolved projection of a manifest change: the pydantic
        ``Manifest`` schema plus the backend-needs-bus invariant, and refuse any
        ``!ENV`` marker left dangling against the current env. The env is unchanged
        by a manifest mutation, so markers resolve against the current process env and
        the bus configuration is the current one."""
        manifest = self._validated_projection(document)
        check_backend_needs_bus(backend_module=manifest.backend_module, bus_configured=bus_settings().enabled)
        refuse_dangling_env_markers(document, dict(os.environ))

    def _validate_env(self, changes: dict[str, str]) -> None:
        """Validate the effective config an env change produces: refuse an X-band key
        in the change PAYLOAD, resolve the persisted manifest's ``!ENV`` markers
        against the post-change env (so a marker that materializes a backend
        participates, and a dropped reference is caught as dangling), and evaluate the
        backend-needs-bus invariant against the post-change bus configuration (so
        removing the bus while a backend remains is rejected too)."""
        refuse_x_band(changes.keys())
        # Change-aware: refuse a key-material key only when the payload SETS it to a value
        # different from the current stored value (rotation-via-editor), never an unchanged
        # carry — the stored env is the real values a read-modify-write round-trip re-sends.
        refuse_key_material(changes, self._read_stored_env())
        effective = self._effective_env(changes)
        refuse_incomplete_admin_pair(effective)
        with _environ(effective):
            preserved = self._read_preserved_manifest()
            refuse_dangling_env_markers(preserved, effective)
            manifest = self._validated_projection(preserved)
            # Resolve the bus through its settings against the effective env (a fresh
            # read, NOT the cached singleton) so a bus configured only via
            # ``TAI_DEFAULT_REDIS_URL`` — not ``TAI_BUS_REDIS_URL`` — is still seen as
            # enabled; a raw ``TAI_BUS_REDIS_URL`` read would falsely reject every
            # Studio env edit on a default-only deployment.
            bus_configured = BusSettings().enabled
        check_backend_needs_bus(backend_module=manifest.backend_module, bus_configured=bus_configured)

    def _validate_env_and_manifest(self, changes: dict[str, str], document: Mapping[str, Any]) -> None:
        """Validate a combined env-write + manifest-mutate before it persists.

        Refuses an X-band key in the env change PAYLOAD, resolves the MUTATED manifest's
        ``!ENV`` markers against the post-change effective env (so the marker the mutator
        just wrote resolves against the value the env write supplies, and a dropped
        reference is caught as dangling), validates the resolved projection, and
        evaluates backend-needs-bus against the post-change bus configuration."""
        refuse_x_band(changes.keys())
        # Change-aware key-material refusal (see :meth:`_validate_env`): a CHANGE to a KEK /
        # signing key is refused, an unchanged carry is allowed.
        refuse_key_material(changes, self._read_stored_env())
        effective = self._effective_env(changes)
        refuse_incomplete_admin_pair(effective)
        with _environ(effective):
            refuse_dangling_env_markers(document, effective)
            manifest = self._validated_projection(document)
            bus_configured = BusSettings().enabled
        check_backend_needs_bus(backend_module=manifest.backend_module, bus_configured=bus_configured)

    def _validate_replace(self, profile_env: dict[str, str]) -> None:
        """Validate a whole-env REPLACE (a settings-profile apply) before it persists —
        the C5 apply's validate-before-persist entry, mirroring :meth:`apply_replace`.

        Refuses any X-band key in the profile's DECLARED payload (NEVER the post-carry
        effective env — the applier legitimately CARRIES the whole X band across
        ``replace_env``), refuses a change that leaves a manifest ``!ENV`` marker
        dangling against the replace-effective env, and evaluates the backend-needs-bus
        invariant against that same env."""
        refuse_x_band(profile_env.keys())
        # Change-aware key-material refusal: a profile snapshotted from the stored env carries
        # the KEK unchanged (allowed); only a profile that would SET key material to a new
        # value is refused (see :meth:`_validate_env`).
        refuse_key_material(profile_env, self._read_stored_env())
        effective = self._effective_replace_env(profile_env)
        refuse_incomplete_admin_pair(effective)
        with _environ(effective):
            preserved = self._read_preserved_manifest()
            refuse_dangling_env_markers(preserved, effective)
            manifest = self._validated_projection(preserved)
            bus_configured = BusSettings().enabled
        check_backend_needs_bus(backend_module=manifest.backend_module, bus_configured=bus_configured)

    def _validated_projection(self, document: Mapping[str, Any]) -> Manifest:
        """Build the RESOLVED in-memory projection of a PRESERVED document (``!ENV``
        markers materialized for validation only) and validate it against the
        ``Manifest`` schema. Raises on an invalid document."""
        return Manifest.model_validate(self._resolve(document))

    def _resolve(self, document: Mapping[str, Any]) -> dict[str, Any]:
        """The RESOLVED projection of a PRESERVED document — ``!ENV`` markers
        materialized against the current process env, structure otherwise intact. The
        one resolution the pipeline uses for both validation and the secret seal."""
        return cast("dict[str, Any]", parse_config(data=dump_manifest(cast("CommentedMap", document))) or {})

    # -- Secret seal -----------------------------------------------------------

    def _seal_secrets(self, document: dict[str, Any], current_preserved: Mapping[str, Any]) -> None:
        """Seal *document* against the current manifest before it persists.

        *current_preserved* is the CURRENT manifest in its preserved view (``!ENV``
        markers) — the pre-mutation document for :meth:`apply_change`, the persisted
        manifest for :meth:`apply_replace`. It is resolved once to know each marker's
        resolved value, then :func:`~tai42_skeleton.config.secret_seal.seal_resolved_secrets`
        retags any leaf of *document* that still equals a resolved secret back to its
        marker (a resolved round-trip preserves the operator's markers) and raises a
        :class:`~tai42_skeleton.config.secret_seal.ResolvedSecretError` on a stranded
        resolved secret with no marker origin. A pure in-place mutation whose leaves
        already carry markers is a no-op. *document* is mutated in place."""
        seal_resolved_secrets(document, cast("dict[str, Any]", current_preserved), self._resolve(current_preserved))

    def _read_preserved_manifest(self) -> dict[str, Any]:
        """The persisted manifest in its PRESERVED view, or an empty document when no
        manifest exists yet — a deployment with no manifest registers no backend, so
        the env-change invariant has nothing to reject."""
        try:
            return self._config_manager.read_manifest_preserved()
        except FileNotFoundError:
            return {}

    def _effective_env(self, changes: dict[str, str]) -> dict[str, str]:
        """The effective env an :meth:`apply_env_change` produces, as the reloaded
        process would see it.

        The stored env is merged with ``changes`` (empties are dropped — the store
        filters them), then overlaid onto the current process env, which a reload
        applies with ``os.environ.update``. A key the change empties is treated as
        removed so a bus-removing change is visible to the invariant."""
        stored = self._read_stored_env()
        removed = {key for key, value in changes.items() if value == ""}
        merged = {key: value for key, value in {**stored, **changes}.items() if value != ""}
        effective = {key: value for key, value in os.environ.items() if key not in removed}
        effective.update(merged)
        return effective

    def _effective_replace_env(self, profile_env: dict[str, str]) -> dict[str, str]:
        """The effective env a profile REPLACE produces, as the reloaded process would
        see it.

        The profile env becomes the WHOLE stored env — keys the profile omits are
        DELETED — while the deployment X band is carried untouched from the current
        process env (X keys never enter a profile). So the old stored keys drop out,
        the profile keys overlay, and the carried X band overlays last; system env the
        store never owned is left in place."""
        stored = self._read_stored_env()
        carried = {key: value for key, value in os.environ.items() if key in x_band_env_keys()}
        effective = {key: value for key, value in os.environ.items() if key not in stored}
        effective.update(profile_env)
        effective.update(carried)
        return effective

    def _read_stored_env(self) -> dict[str, str]:
        """The stored env map, treating a never-written store as empty."""
        try:
            return self._config_manager.read_env()
        except FileNotFoundError:
            return {}

    # -- Persist tail ----------------------------------------------------------

    async def _reload_and_broadcast(self, *, document: dict[str, Any] | None) -> ApplyResult:
        """Locally reload through the gate, then broadcast the reload to the whole
        fleet and embed the report.

        Post-persist contract: this runs only after the persist has committed, and it
        guarantees EVERY subsequent failure — the local reload OR the broadcast itself
        — surfaces as a :class:`FleetBroadcastError` carrying a fleet report, never a
        raw exception. So every caller can key on one shape: a raised
        ``FleetBroadcastError`` means the change LANDED but its propagation failed
        (restore / converge), while a bare raise below this point would mean nothing
        persisted.

        A local reload that raises after the persist landed does NOT abort the
        broadcast — the siblings must still converge on the persisted state — so the
        reload publishes anyway with a ``failed`` self entry and then re-raises the
        local failure with the fleet report attached. A broadcast that raises anything
        other than the transport-unreachable shape the bus already folds into a
        returned report (e.g. a redis ``ResponseError``, or a malformed presence key
        the census cannot parse) is caught here and re-raised as a
        ``FleetBroadcastError`` carrying the honest bus-unreachable report (no worker
        list, only the error) — the same shape the bus returns for a transport
        failure — so a raw broadcast error can never escape the committed persist. An
        unconfirmed worker on the happy path is a loud ERROR log and an explicit
        report entry, but the call returns successfully."""
        op_name = "reload_config"
        local_failure: Exception | None = None
        local_result: dict[str, Any] | None = None
        try:
            local_result = await reload_gate.run(self._admin.reload_config, reimports=True)
        except Exception as exc:
            local_failure = exc
            local = LocalApplyResult(outcome=OpOutcome.failed, error=f"{type(exc).__name__}: {exc}")
        else:
            local = LocalApplyResult(outcome=OpOutcome.applied, payload=local_result)

        try:
            report = await self._bus.publish({"op": op_name}, None, local)
        except Exception as broadcast_error:
            # The persist already committed, so a raw broadcast failure must not escape
            # the post-persist contract. Surface it as a FleetBroadcastError carrying
            # the honest bus-unreachable report (no worker list, only the error) — the
            # same shape WorkerBus.publish returns for a transport failure — so every
            # caller treats the change as landed-but-propagation-failed and restores or
            # converges rather than half-writing. A local reload that ALSO failed is
            # noted in the report error so it is not lost behind the broadcast error.
            error = f"{type(broadcast_error).__name__}: {broadcast_error}"
            if local_failure is not None:
                error = f"{error} (local reload also failed: {type(local_failure).__name__}: {local_failure})"
            report = FleetResult(op=op_name, reachable=False, error=error)
            raise FleetBroadcastError(op_name, report, broadcast_error) from broadcast_error

        # The report rides the response, but an unconfirmed worker is also a loud,
        # visible failure — never a silently stale sibling.
        log_non_convergence(report)
        if local_failure is not None:
            raise FleetBroadcastError(report.op, report, local_failure) from local_failure
        # local_result is set on the success path (no local_failure).
        return ApplyResult(fleet=report, local=cast("dict[str, Any]", local_result), document=document)


def _replace_diff_keys(stored: Mapping[str, str], proposed: Mapping[str, str]) -> set[str]:
    """The env key NAMES a whole-env replace changes: added, removed, or value-changed.
    Names only — the caller classifies them; a diff never carries a value off this seam."""
    added = {key for key in proposed if key not in stored}
    removed = {key for key in stored if key not in proposed}
    changed = {key for key in proposed if key in stored and stored[key] != proposed[key]}
    return added | removed | changed


def _refuse_unrecyclable(diff_keys: set[str], recycle_diff_keys: list[str], report: CapabilityReport) -> None:
    """Refuse a profile apply whose diff cannot be carried on this deployment shape.

    A diff key the shape PINS (``refused_keys`` — a pod/container respawn re-injects it,
    or it reaches the worker bus itself) is refused upfront naming the key: a recycle can
    never make it stick. A recycle-class diff on a BARE (unsupervised) deployment is
    refused wholesale — no supervisor exists to respawn a worker under the new env. Both
    are loud ``ValueError``s the operations layer maps to a 400. Names only."""
    pinned = sorted(diff_keys & set(report.refused_keys))
    if pinned:
        raise ValueError(
            f"Refusing to apply this settings profile on a {report.shape.value!r} deployment: it changes "
            f"deployment-pinned key(s) a recycle cannot carry (a pod/container respawn re-injects them, or "
            f"they reach the worker bus itself): {', '.join(pinned)}. Change them in the deployment manifest."
        )
    if recycle_diff_keys and not report.recycle_supported:
        raise ValueError(
            "Refusing to apply this settings profile on a bare (unsupervised) deployment: it changes "
            f"recycle-class key(s) that require a worker recycle, and no supervisor is present to respawn "
            f"workers under the new env: {', '.join(recycle_diff_keys)}. Run under a supervised deployment."
        )


def _recycle_step_timeout() -> float:
    """The per-step recycle budget — the same drain budget a retire uses, so a recycled
    worker's replacement gets the shutdown-drain window to boot and rejoin the census."""
    from tai42_skeleton.routers.tool_runs_settings import tool_runs_settings

    return tool_runs_settings().shutdown_drain_seconds


@contextmanager
def _environ(env: dict[str, str]) -> Iterator[None]:
    """Temporarily replace ``os.environ`` with ``env`` for the duration of the block,
    restoring it exactly afterwards. Used to resolve a manifest's ``!ENV`` markers
    against a proposed post-change env without mutating the real process env."""
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)
