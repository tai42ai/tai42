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
from typing import TYPE_CHECKING, Any, ClassVar, cast

from tai42_kit.fork_gate import fork_gate

from tai42_skeleton.app.epoch import Epoch, build_and_swap_epoch
from tai42_skeleton.app.recycle import RecycleReport, orchestrate_recycle
from tai42_skeleton.app.reload_gate import FORK_QUIESCE_SECONDS, reload_gate
from tai42_skeleton.config.boundary import reload_class_by_env_var
from tai42_skeleton.config.recycle_policy import (
    CENSUS_TARGET_KINDS,
    _recycle_step_timeout,
    _refuse_unrecyclable,
    _replace_diff_keys,
    capability_report,
)
from tai42_skeleton.config.secret_seal import _leaving_connector_secrets, _parse_marks
from tai42_skeleton.config.service.broadcast import _BroadcastMixin, _release_llm_pools
from tai42_skeleton.config.service.resolution import _ResolutionMixin
from tai42_skeleton.config.service.results import ApplyResult, OrphanEnvWriteError, ProfileApplyOutcome
from tai42_skeleton.config.service.validation import _ValidationMixin
from tai42_skeleton.operations._broadcast import snapshot_membership

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from tai42_skeleton.app.bus import WorkerBus
    from tai42_skeleton.config.service.results import _FleetPublisher, _ManifestStore, _ReloadAdmin


# The operator's "treat these env keys as secret" marks var — the same
# comma-separated key-name list ``set_mcp_secret_env`` and the installer append to.
_SECRET_MARKS_VAR = "TAI_ENV_SECRET_KEYS"  # noqa: S105 constant identifier, not a secret value


class ConfigService(_ValidationMixin, _ResolutionMixin, _BroadcastMixin):
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
        """Bind the config manager, the admin reload seam, and the worker bus the pipeline drives."""
        self._config_manager = config_manager
        self._admin = admin
        self._bus = bus

    @classmethod
    def _env_lock(cls) -> asyncio.Lock:
        """The process-wide env-write lock bound to the running serving loop.

        Created on first use and rebound only if the running loop differs from the bound one (a loop swap —
        there is never a second live loop in the same process). Mirrors :meth:`ReloadGate._serving_lock`,
        keeping the singleton correct across loops rather than raising the cross-loop ``RuntimeError`` a bare
        module-level ``asyncio.Lock`` would across test loops.
        """
        loop = asyncio.get_running_loop()
        if cls._env_write_lock is None or cls._env_write_loop is not loop:
            cls._env_write_lock = asyncio.Lock()
            cls._env_write_loop = loop
        return cls._env_write_lock

    @classmethod
    def from_app(cls) -> ConfigService:
        """Wire the pipeline from the running app.

        Pulls the active config manager, the admin reload seam, and this process's worker bus.
        """
        from tai42_contract.app import tai42_app

        from tai42_skeleton.app import instance

        return cls(config_manager=tai42_app.config.config_manager, admin=tai42_app.admin, bus=instance.app.bus)

    async def apply_change(self, mutator: Callable[[dict[str, Any]], None]) -> ApplyResult:
        """Read-modify-write the manifest, then persist, reload, and broadcast to the fleet.

        Runs ``mutator`` on the PRESERVED manifest inside the config-manager transaction, VALIDATEs the
        resolved projection of the mutated document, persists, locally reloads, and broadcasts to the whole
        fleet.

        ``mutator`` edits the passed document IN PLACE and must be pure / re-runnable
        (the transaction may re-run it on a concurrency conflict). An invalid mutation
        raises inside the transaction, so nothing is persisted. The mutated document
        is SEALED against the pre-mutation document before it persists, so a mutator
        that ingests resolved secret values (a resolved round-trip) cannot bake a
        secret to disk — see :meth:`_seal_secrets`.

        When the mutation DROPS an oauth connector, its ``client_secret_env`` NAME must
        stay masked after removal (its value is not deleted), so this method DELEGATES to
        the combined :meth:`apply_env_and_change` seam — persisting the marks union and the
        manifest mutation atomically under the env-write lock — instead of the plain
        transaction. When nothing leaves, the plain path runs unchanged.
        """
        preserved = self._read_preserved_manifest()
        candidate = copy.deepcopy(preserved)
        mutator(candidate)
        if _leaving_connector_secrets(preserved, candidate):
            return await self._apply_change_with_leaving(mutator)

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
        """Replace the whole manifest, then reload locally and broadcast to the fleet.

        VALIDATEs the resolved projection of ``document`` BEFORE it is persisted (a replace has no mutator to
        abort), then replaces, locally reloads, and broadcasts to the whole fleet.

        The caller supplies the PRESERVED-view document (``!ENV`` markers, never
        resolved values) — the seam persists it verbatim. The document is SEALED
        against the CURRENT persisted manifest before it persists, so a replacement
        carrying a resolved secret (a resolved round-trip) is retagged or refused —
        see :meth:`_seal_secrets`.

        When the replacement DROPS an oauth connector, its ``client_secret_env`` NAME must
        stay masked after removal, so this method DELEGATES to the combined
        :meth:`apply_env_and_change` seam with a replace-as-mutator (``clear()`` +
        ``update()``, so the whole-document replace still DELETES omitted sections — never
        a merge) — persisting the marks union and the replace atomically. When nothing
        leaves, the plain replace runs unchanged.
        """
        preserved = self._read_preserved_manifest()
        if _leaving_connector_secrets(preserved, document):

            def replace_mutator(doc: dict[str, Any]) -> None:
                doc.clear()
                doc.update(document)

            return await self._apply_change_with_leaving(replace_mutator)
        self._validate_manifest(document)
        self._seal_secrets(document, preserved)
        persisted = self._config_manager.replace_manifest(document)
        return await self._reload_and_broadcast(document=persisted)

    async def _apply_change_with_leaving(self, mutator: Callable[[dict[str, Any]], None]) -> ApplyResult:
        """Persist a manifest mutation that drops an oauth connector through the combined env+manifest seam.

        The leaving ``client_secret_env`` names are folded into the stored
        ``TAI_ENV_SECRET_KEYS`` marks and the manifest mutation land atomically.

        ``prepare`` returns an EMPTY change set and the caller's own ``mutator``:
        :meth:`apply_env_and_change` itself computes the leaving names from the mutated
        candidate and folds the marks union into the env write (written only when it grows
        the stored set). Its validate/seal/persist/reload rules then apply exactly as for
        the combined seam's native callers, and a manifest-persist failure surfaces the same
        :class:`OrphanEnvWriteError` (a marks write may have landed).
        """

        async def prepare(_stored: dict[str, str]) -> tuple[dict[str, str], Callable[[dict[str, Any]], None]]:
            return {}, mutator

        return await self.apply_env_and_change(prepare)

    async def apply_env_change(self, changes: dict[str, str]) -> ApplyResult:
        """Apply env overrides, validating the effective config before anything is written.

        VALIDATE the effective/resolved config (the manifest's ``!ENV`` markers
        materialized against the post-change env) through the SAME backend-needs-bus gate,
        then merge the overrides, locally reload, and broadcast to the whole fleet. An
        invalid effective config raises before anything is written.
        """
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
           document, so an external store's optimistic-concurrency REPLAY (re-read + re-run on a
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
            # BEFORE anything persists (an external store's transaction cannot resolve markers against the
            # not-yet-live env, so validation cannot move inside it).
            preserved = self._read_preserved_manifest()
            candidate = copy.deepcopy(preserved)
            mutator(candidate)
            # Stickiness across removal: an oauth connector this mutation DROPS keeps its
            # secret value in the store, so fold its client_secret_env NAME into the marks
            # union (stored union caller marks union leaving; written only when it grows the stored
            # set) BEFORE the env write, so the value stays masked after the connector goes.
            self._fold_leaving_secret_marks(changes, stored, preserved, candidate)
            self._validate_env_and_manifest(changes, candidate)

            # STEP 2 — env-write-FIRST. Env-first (not manifest-first) guarantees there is
            # never a persisted ``!ENV`` marker referencing an env key not yet stored.
            self._config_manager.write_env(changes)

            # STEP 3 — persist the manifest with a guarded, PURE, re-runnable mutator: it seals
            # the mutated document against its pre-mutation snapshot before persist (parity with
            # ``apply_change`` — a mutator can never bake a resolved secret to disk), and it edits
            # only the passed document, so an external store's optimistic-concurrency REPLAY (re-read + re-run
            # on a 409) is side-effect-free and never double-writes the env, which is outside the
            # replayed span. A persist failure performs NO rollback — the env write STANDS as
            # an inert, re-runnable orphan — and raises loudly naming the orphan key(s) + pointer.
            def guarded(document: dict[str, Any]) -> None:
                current = copy.deepcopy(document)
                mutator(document)
                # Parity with ``apply_change.guarded``: re-validate the mutated document
                # inside the transaction so an external store's optimistic-concurrency REPLAY against a
                # concurrently-changed manifest never persists an unvalidated result. ONE
                # validator for every caller — the env-aware ``_validate_env_and_manifest``
                # (``_validate_manifest`` would wrongly reject the just-written store key its
                # os.environ marker resolution cannot see).
                self._validate_env_and_manifest(changes, document)
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

    @staticmethod
    def _fold_leaving_secret_marks(
        changes: dict[str, str],
        stored: Mapping[str, str],
        preserved: Mapping[str, Any],
        candidate: Mapping[str, Any],
    ) -> None:
        """Fold every dropped oauth ``client_secret_env`` NAME into ``changes[TAI_ENV_SECRET_KEYS]``.

        Every NAME this change DROPS from the manifest is folded in so a leaving
        connector's secret value stays masked after removal (its value is never deleted).

        The union is ``stored marks union caller marks (already in ``changes`` when the caller
        set them) union leaving names``, written back only when it GROWS the stored set — so a
        no-op change never rewrites the marks and a leaving name already marked adds nothing.
        Names only; env VALUES are untouched.
        """
        leaving = _leaving_connector_secrets(preserved, candidate)
        if not leaving:
            return
        stored_marks = _parse_marks(stored.get(_SECRET_MARKS_VAR))
        base = _parse_marks(changes[_SECRET_MARKS_VAR]) if _SECRET_MARKS_VAR in changes else list(stored_marks)
        final = list(dict.fromkeys([*base, *sorted(leaving)]))
        if set(final) != set(stored_marks):
            changes[_SECRET_MARKS_VAR] = ",".join(final)

    # -- Profile apply ----------------------------------------------------

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
        """Apply a settings profile: the env-write-LAST reload pipeline.

        Ordered so a failed build leaves the STORE untouched and the old surface serving
        (``os.environ`` restored exactly by :func:`build_and_swap_epoch`):

        1. ``_validate_replace`` (X-band payload refusal + dangling ``!ENV`` + backend-
           needs-bus); diff the proposed env against the stored env; refuse a recycle-
           class diff the deployment shape cannot carry; read the applier's own identity
           and the fleet membership the step-5 report is judged against.
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

        # STEP 1d — pin the expected-confirmation membership BEFORE the swap in STEP 3.
        # The swap is this door's local apply; the STEP 5 broadcast censuses only when it
        # publishes, so without this a sibling whose presence faded across the rebuild
        # would never be expected and the reload would report converged without it.
        at_start = await snapshot_membership(self._bus, "reload_config")

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
        fleet = await self._broadcast_profile_reload(at_start)
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
