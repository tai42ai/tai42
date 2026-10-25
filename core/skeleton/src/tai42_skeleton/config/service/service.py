"""The single manifest-mutation pipeline.

Every manifest / env mutation crosses one pipeline so no writer can forget a step:

    [write exclusion: read → mutate → VALIDATE → held-unpick check → re-VALIDATE when
     its inputs moved → SEAL → persist] → local reload → broadcast

A settings-profile apply (:meth:`ConfigService.apply_replace_env`) crosses its variant:

    [write exclusion: VALIDATE → held-unpick check → re-VALIDATE when its inputs moved
     → ``@previous`` snapshot → (reload gate + fork gate: re-VALIDATE when its inputs
     moved → build) → persist] → broadcast → recycle roll

Every write entry holds the write exclusion from its first store read through its persist,
so no other write lands between a write's last validation and its persist; it is released
before the reload / broadcast tail. The exclusion is two holds, taken in this order: the
process-wide config-write lock, which queues the process's own writers, then the config
store's exclusive write span (:meth:`~tai42_contract.config.manager.ConfigManager.exclusive_write`),
which excludes every other span over the store, in every other process and in this one. A
request waiting for either is recorded on its generation like a reload-gate waiter
(:func:`~tai42_skeleton.app.reload_gate.hold_recorded`,
:func:`~tai42_skeleton.app.reload_gate.hold_recorded_blocking`), so a retire the holder
drives never severs it. A writer abandoned before its span's entry completed releases the
config-write lock at once and leaves the entry to its thread, which exits the span as soon
as it is in; the store span keeps the next writer out until then. A writer whose
cancellation lands as its entry completes exits the span itself. A span's exit completes
before its writer returns. A span whose exit fails after the persist is recorded on the
result (``span_release_error``), and the reload / broadcast tail still runs
(:mod:`~tai42_skeleton.config.service.exclusion`). Lock order: config-write lock → store
write span → reload gate → fork gate, never the reverse; nothing that holds the reload gate
writes config.

Validation resolves ``!ENV`` markers against the env the store converges every worker to
(:meth:`_converged_env`), not against this process's env alone: a key another worker
removed from the store is gone for every worker at its next reload.

:class:`ConfigService` exposes the write entrypoints :meth:`apply_change` (a
read-modify-write through the config manager's transaction), :meth:`reapply_manifest`
(the stored manifest persisted unchanged, to drive a reload), :meth:`apply_replace`
(a whole-document replace), :meth:`apply_env_change` (an env override),
:meth:`apply_derived_env_change` (an env override derived from the stored env),
:meth:`apply_env_and_change` (an env write and a manifest mutation as one unit) and
:meth:`apply_replace_env` (a settings-profile apply). The first six return a structured
:class:`ApplyResult` carrying the persisted document (where applicable), the local reload
result, and the awaited per-worker fleet report.

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

The broadcast tail is the whole fleet, always: ``publish(ReloadConfig(op="reload_config"),
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

The profile apply's recycle roll follows its broadcast under the same contract: a roll
that stops (a target timed out, failed, or never returned to ready, or the bus could not
be read) is a loud ERROR log and the partial recycle report in the returned outcome,
never an exception past :meth:`ConfigService.apply_replace_env`; only a bus contract
violation (a reply that does not name the published target), or a bus fault that is not
an outage, escapes.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import logging
from typing import TYPE_CHECKING, Any, ClassVar, cast

from tai42_kit.fork_gate import fork_gate
from tai42_kit.llm import release_loop_bound_resources

from tai42_skeleton.app.epoch import Epoch, build_and_discard_epoch, build_and_swap_epoch
from tai42_skeleton.app.recycle import RecycleReport, orchestrate_recycle
from tai42_skeleton.app.reload_gate import FORK_QUIESCE_SECONDS, reload_gate
from tai42_skeleton.config.boundary import refuse_unrecyclable_env_change
from tai42_skeleton.config.secret_seal import _leaving_connector_secrets
from tai42_skeleton.config.service.broadcast import _BroadcastMixin
from tai42_skeleton.config.service.exclusion import WriteSpan, write_exclusion
from tai42_skeleton.config.service.resolution import _environ, _ResolutionMixin
from tai42_skeleton.config.service.results import ApplyResult, OrphanEnvWriteError, ProfileApplyOutcome
from tai42_skeleton.config.service.validation import _ValidationMixin
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.operations._broadcast import awaiting_respawn_after, log_awaiting_respawn, snapshot_membership
from tai42_skeleton.settings.env_secret_marks import SECRET_MARKS_ENV_VAR, format_secret_marks, parse_secret_marks

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from contextlib import AbstractAsyncContextManager

    from tai42_contract.manifest import ExtensionElement

    from tai42_skeleton.app.bus import WorkerBus
    from tai42_skeleton.config.service.results import _FleetPublisher, _ManifestStore, _ReloadAdmin

logger = logging.getLogger(__name__)


def _unchanged(_document: dict[str, Any]) -> None:
    """The mutator of a write that persists the stored manifest as it is."""


class ConfigService(_ValidationMixin, _ResolutionMixin, _BroadcastMixin):
    """The one pipeline every manifest / env mutation crosses.

    Constructed per call site from the running app via :meth:`from_app`, or directly
    with the config manager, the reload seam, and the worker bus for testing.
    """

    # Process-wide config-write lock: the loop-side queue of this process's writers. ``from_app``
    # builds a FRESH ConfigService per call, so this is CLASS-level (not per-instance) — one
    # queue for every writer of the process. Every write entry holds it from its first store
    # read through its persist, so the writers queued behind it wait on the loop, not on a pool
    # thread. A writer abandoned before its span's entry completed releases the lock and leaves
    # that entry to its pool thread, which stays blocked until the span is entered and exited,
    # so more than one pool thread can block on the span at once. Lazily bound to the running serving
    # loop and rebound on a loop swap (mirrors :class:`ReloadGate`), so it is correct across
    # the process's single serving loop and across per-test loops. Acquired through
    # :func:`hold_recorded`, which records its waiters: a settings-profile apply holds it
    # across its build, which may retire the generation a waiter belongs to.
    #
    # Lock order: config-write lock → store write span → reload gate → fork gate, never the
    # reverse. The lock is taken first so the process's own writers queue on the loop instead
    # of each blocking a thread on the store's span; the span is taken next, before the first
    # store read, so no other write, of this process or another, lands inside this one — the
    # combined op's READ→derive→WRITE span (the secret-marks append, the generated-key
    # collision check) included, which the provider's per-call lock cannot close
    # (``read_env`` sits OUTSIDE that lock).
    # The profile apply takes the reload gate (as its driver) and the fork gate inside both;
    # every other entry releases both before ``_reload_and_broadcast`` acquires the reload
    # gate, and nothing that holds the reload gate writes config — no deadlock.
    _config_write_lock: ClassVar[asyncio.Lock | None] = None
    _config_write_loop: ClassVar[asyncio.AbstractEventLoop | None] = None

    def __init__(
        self,
        config_manager: _ManifestStore,
        admin: _ReloadAdmin,
        bus: _FleetPublisher,
        *,
        combo_check: Callable[[str, Sequence[ExtensionElement]], None],
        orchestrate: Callable[..., Awaitable[RecycleReport]] = orchestrate_recycle,
    ) -> None:
        """Bind the config manager, the admin reload seam, the worker bus, the combo check and the recycle roll.

        ``combo_check`` dry-run binds one extension combo of a tool and raises on a refusal; the
        gate runs it on every combo of a candidate manifest. ``orchestrate`` is the fleet recycle
        roll an env change carrying a recycle-class key runs; it defaults to the running primitive
        and is injectable so the roll is exercised in isolation.
        """
        self._config_manager = config_manager
        self._admin = admin
        self._bus = bus
        self._combo_check = combo_check
        self._orchestrate = orchestrate

    @classmethod
    def _write_lock(cls) -> asyncio.Lock:
        """The process-wide config-write lock bound to the running serving loop.

        Created on first use and rebound only if the running loop differs from the bound one (a loop swap —
        there is never a second live loop in the same process). Mirrors :meth:`ReloadGate._serving_lock`,
        keeping the singleton correct across loops rather than raising the cross-loop ``RuntimeError`` a bare
        module-level ``asyncio.Lock`` would across test loops.
        """
        loop = asyncio.get_running_loop()
        if cls._config_write_lock is None or cls._config_write_loop is not loop:
            cls._config_write_lock = asyncio.Lock()
            cls._config_write_loop = loop
        return cls._config_write_lock

    def _write_exclusion(self) -> AbstractAsyncContextManager[WriteSpan]:
        """Hold the process-wide config-write lock, then the config store's exclusive write span.

        Every write entry holds this from before its first store read through its persist, its
        last statement inside it, and hands the yielded :class:`WriteSpan`'s ``release_error``
        to its tail. A caller waiting on either hold is recorded on its generation as waiting.
        """
        return write_exclusion(self._write_lock(), self._config_manager.exclusive_write())

    @classmethod
    def from_app(cls) -> ConfigService:
        """Wire the pipeline from the running app.

        Pulls the active config manager, the admin reload seam, this process's worker bus and the
        live extension combo check.
        """
        from tai42_contract.app import tai42_app

        from tai42_skeleton.app import instance

        return cls(
            config_manager=tai42_app.config.config_manager,
            # The bound app's admin facet is the skeleton's, which also lists a server's served names.
            admin=cast("_ReloadAdmin", tai42_app.admin),
            bus=instance.app.bus,
            combo_check=instance.app.extensions.check_combo,
        )

    def read_manifest_document(self) -> dict[str, Any]:
        """The persisted manifest in its PRESERVED view (empty when none exists yet), a copy the caller owns.

        A caller asks a mutator what it would change before driving :meth:`apply_change`, which
        persists, reloads and broadcasts whatever it is given.
        """
        return copy.deepcopy(self._read_preserved_manifest())

    async def apply_change(self, mutator: Callable[[dict[str, Any]], None]) -> ApplyResult:
        """Read-modify-write the manifest, then persist, reload, and broadcast to the fleet.

        Under the write exclusion, runs ``mutator`` on a copy of the PRESERVED manifest and
        VALIDATEs the resolved projection of the result before the held-unpick check lists any
        MCP server, then runs it again inside the config-manager transaction (validating the
        result anew when the stored manifest, the process env, the bus setting or the live
        serving generation changed meanwhile) and persists; then, the exclusion released, locally
        reloads and broadcasts to the whole fleet.

        ``mutator`` edits the passed document IN PLACE and must be pure / re-runnable
        (the transaction may re-run it on a concurrency conflict). An invalid mutation
        raises before anything persists. The mutated document
        is SEALED against the pre-mutation document before it persists, so a mutator
        that ingests resolved secret values (a resolved round-trip) cannot bake a
        secret to disk — see :meth:`_seal_secrets`.

        When the mutation DROPS an oauth connector, its ``client_secret_env`` NAME must
        stay masked after removal (its value is not deleted), so this method DELEGATES to
        the combined :meth:`apply_env_and_change` seam — persisting the marks union and the
        manifest mutation atomically — instead of the plain transaction. When nothing
        leaves, the plain path runs unchanged.
        """
        async with self._write_exclusion() as span:
            preserved = self._read_preserved_manifest()
            candidate = copy.deepcopy(preserved)
            mutator(candidate)
            if _leaving_connector_secrets(preserved, candidate):
                persisted, recycle_keys = await self._apply_change_with_leaving(mutator)
            else:
                persisted = await self._apply_change_locked(mutator, preserved, candidate)
                recycle_keys = ()
        return await self._reload_and_broadcast(
            document=persisted, recycle_keys=recycle_keys, span_release_error=span.release_error
        )

    async def _apply_change_locked(
        self, mutator: Callable[[dict[str, Any]], None], preserved: dict[str, Any], candidate: dict[str, Any]
    ) -> dict[str, Any]:
        """Validate, check and persist a manifest mutation that drops no oauth connector.

        The caller holds the write exclusion.
        """
        # An invalid mutation is refused before the held-unpick check lists any MCP server.
        validated = self._validate_manifest_noting_inputs(candidate)
        await self._refuse_held_unpicks(preserved, candidate)

        def guarded(document: dict[str, Any]) -> None:
            # Snapshot the preserved document the mutator received BEFORE it runs, so
            # the seal can retag the mutated document against the current manifest's
            # markers and their resolved values.
            current = copy.deepcopy(document)
            mutator(document)
            # ``candidate`` was validated; the result is validated again when it differs (the
            # manifest changed since ``candidate`` was built) or when the env, the bus setting
            # or the live serving generation changed during the listing.
            if document != candidate or not validated.unchanged():
                self._validate_manifest(document)
            self._seal_secrets(document, current)

        return self._config_manager.mutate_manifest(guarded)

    async def reapply_manifest(
        self, *, continues: ApplyResult | None = None, recycle_keys: Sequence[str] = ()
    ) -> ApplyResult:
        """Persist the stored manifest unchanged, then reload locally and broadcast to the fleet.

        The manifest is read under the write exclusion, so every write that landed before it
        stands; it crosses the same validate, held-unpick, seal and persist steps as
        :meth:`apply_change`.

        ``continues`` / ``recycle_keys`` (exclusive) make the reload the continuation of an
        earlier change: ``continues`` carries that change's already-run roll (no pin, no second
        roll, one verdict), ``recycle_keys`` its recycle-class keys whose roll never ran (this
        reload rolls them).
        """
        async with self._write_exclusion() as span:
            preserved = self._read_preserved_manifest()
            persisted = await self._apply_change_locked(_unchanged, preserved, copy.deepcopy(preserved))
        return await self._reload_and_broadcast(
            document=persisted, recycle_keys=recycle_keys, continues=continues, span_release_error=span.release_error
        )

    async def apply_replace(self, document: dict[str, Any]) -> ApplyResult:
        """Replace the whole manifest, then reload locally and broadcast to the fleet.

        Under the write exclusion, VALIDATEs the resolved projection of ``document`` BEFORE it is
        persisted (a replace has no mutator to abort) and replaces; then, the exclusion released, locally
        reloads and broadcasts to the whole fleet.

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
        async with self._write_exclusion() as span:
            preserved = self._read_preserved_manifest()
            recycle_keys: Sequence[str] = ()
            if _leaving_connector_secrets(preserved, document):

                def replace_mutator(doc: dict[str, Any]) -> None:
                    doc.clear()
                    doc.update(document)

                persisted, recycle_keys = await self._apply_change_with_leaving(replace_mutator)
            else:
                validated = self._validate_manifest_noting_inputs(document)
                await self._refuse_held_unpicks(preserved, document)
                # Validated again when the env, the bus setting or the live serving generation
                # changed during the listing.
                if not validated.unchanged():
                    self._validate_manifest(document)
                self._seal_secrets(document, preserved)
                persisted = self._config_manager.replace_manifest(document)
        return await self._reload_and_broadcast(
            document=persisted, recycle_keys=recycle_keys, span_release_error=span.release_error
        )

    async def _refuse_held_unpicks(
        self, before: Mapping[str, Any], after: Mapping[str, Any], *, env: Mapping[str, str] | None = None
    ) -> None:
        """Refuse a write whose MCP selection change would stop picking a tool something still uses.

        Both documents are projected to the typed manifest; ``after`` resolves its ``!ENV`` markers
        against ``env`` when given (the post-change env of a write that changes the env). Raises
        the 409 or 502 of :func:`refuse_held_unpicks` before anything persists. A write that keeps
        the env and leaves the ``mcp`` section as stored changes no selection and is not projected.
        """
        if env is None and before.get("mcp") == after.get("mcp"):
            return
        # Imported here: the manifest operations package imports this service.
        from tai42_skeleton.operations.manifest.selection_holders import refuse_held_unpicks

        before_manifest = self._typed_projection(before)
        if env is None:
            after_manifest = self._typed_projection(after)
        else:
            with _environ(dict(env), stored_env_file=self._stored_env_file()):
                after_manifest = self._typed_projection(after)
        await refuse_held_unpicks(before_manifest, after_manifest, self._admin.served_mcp_names)

    def _typed_projection(self, document: Mapping[str, Any]) -> Manifest:
        """The resolved ``Manifest`` of a PRESERVED document, schema-checked, its combos not dry-run."""
        return Manifest.model_validate(self._resolve(document))

    async def _apply_change_with_leaving(
        self, mutator: Callable[[dict[str, Any]], None]
    ) -> tuple[dict[str, Any], Sequence[str]]:
        """Persist a manifest mutation that drops an oauth connector through the combined env+manifest seam.

        The leaving ``client_secret_env`` names are folded into the stored
        ``TAI_ENV_SECRET_KEYS`` marks and the manifest mutation land atomically. The caller
        holds the write exclusion and runs the reload tail on the returned persisted
        document and recycle-class keys once it has released it.

        ``prepare`` returns an EMPTY change set and the caller's own ``mutator``:
        :meth:`apply_env_and_change` itself computes the leaving names from the mutated
        candidate and folds the marks union into the env write (written only when it grows
        the stored set). Its validate/seal/persist rules then apply exactly as for the
        combined seam's native callers, and a manifest-persist failure surfaces the same
        :class:`OrphanEnvWriteError` (a marks write may have landed).
        """

        async def prepare(_stored: dict[str, str]) -> tuple[dict[str, str], Callable[[dict[str, Any]], None]]:
            return {}, mutator

        return await self._apply_env_and_change_locked(prepare)

    async def apply_env_change(self, changes: dict[str, str], secret_keys: list[str] | None = None) -> ApplyResult:
        """Apply env overrides, validating the effective config before anything is written.

        ``secret_keys`` (when not ``None``) replaces the stored secret marks in the same
        validated write.

        Under the write exclusion, VALIDATE the effective/resolved config (the manifest's
        ``!ENV`` markers materialized against the post-change env) through the SAME
        backend-needs-bus gate and merge the overrides; then, the exclusion released, locally
        reload and broadcast to the whole fleet. An invalid effective config raises before
        anything is written.
        """
        if secret_keys is not None:
            changes = {**changes, SECRET_MARKS_ENV_VAR: format_secret_marks(dict.fromkeys(secret_keys))}
        async with self._write_exclusion() as span:
            recycle_keys = await self._apply_env_change_locked(changes)
        return await self._reload_and_broadcast(
            document=None, recycle_keys=recycle_keys, span_release_error=span.release_error
        )

    async def apply_derived_env_change(self, derive: Callable[[dict[str, str]], dict[str, str]]) -> ApplyResult:
        """Apply env overrides derived from the stored env, read under the write exclusion.

        ``derive`` receives the CURRENT stored env and returns the changes, so a write that
        landed before this one is what they are derived from. The changes then cross the same
        validate, held-unpick and write steps as :meth:`apply_env_change`, and the reload and
        broadcast run once the exclusion is released. An exception ``derive`` raises propagates
        with nothing written.
        """
        async with self._write_exclusion() as span:
            recycle_keys = await self._apply_env_change_locked(derive(self._read_stored_env()))
        return await self._reload_and_broadcast(
            document=None, recycle_keys=recycle_keys, span_release_error=span.release_error
        )

    async def _apply_env_change_locked(self, changes: dict[str, str]) -> list[str]:
        """Validate and write env ``changes``, the write exclusion held by the caller.

        Returns the change's recycle-class keys for the caller's reload tail.
        """
        validated = self._env_validation_inputs()
        recycle_keys = self._validate_env(changes)
        document = self._read_preserved_manifest()
        await self._refuse_held_unpicks(document, document, env=self.effective_env(changes))
        # Validated again when the process env, the stored env or manifest, or the live
        # serving generation changed while the held-unpick check awaited.
        if not self._env_validation_inputs_unchanged(validated):
            recycle_keys = self._validate_env(changes)
        self._config_manager.write_env(changes)
        return recycle_keys

    async def apply_env_and_change(
        self,
        prepare: Callable[[dict[str, str]], Awaitable[tuple[dict[str, str], Callable[[dict[str, Any]], None]]]],
        *,
        manifest_pointer: str | None = None,
    ) -> ApplyResult:
        """Combined env-write + manifest-mutate as ONE consistent unit.

        The ``set_mcp_secret_env`` door writes a secret VALUE to the env store and a
        matching ``!ENV ${KEY}`` MARKER into the manifest; the two must stay consistent.
        ``prepare`` receives the CURRENT stored env (read once under the write exclusion) and
        returns ``(changes, mutator)`` — it derives the generated/explicit key, the
        appended secret marks, and the marker-writing mutator FROM that snapshot. Running
        it inside the exclusion is what makes the read→derive→write span atomic.

        Ordering keeps env + manifest consistent, all under the write exclusion so
        concurrent writes, in this process or another sharing the store, cannot interleave
        their read→write (a lost secret-marks append, or two ops minting the same generated
        key):

        0. Acquire the write exclusion, read the stored env ONCE, and call
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
        4. RELEASE the write exclusion, THEN locally reload and broadcast to the whole
           fleet. The reload acquires ``reload_gate``; the write exclusion is released
           first so the two never nest (no deadlock) — the read→write span is entirely
           before the reload gate.

        ``manifest_pointer`` is the caller's target pointer, named in the orphan report
        when it is known (``set_mcp_secret_env`` supplies it).
        """
        # STEP 0-3 run under the write exclusion: the stored read, the caller's derive,
        # and both persists are one atomic span so a concurrent write cannot read the same
        # marks/keys and clobber this op's write. The reload tail (STEP 4) is OUTSIDE the
        # exclusion so ``reload_gate`` is never acquired while it is held.
        async with self._write_exclusion() as span:
            persisted, recycle_keys = await self._apply_env_and_change_locked(
                prepare, manifest_pointer=manifest_pointer
            )
        # STEP 4 — lock RELEASED; reload + broadcast (acquires reload_gate) runs outside it.
        return await self._reload_and_broadcast(
            document=persisted, recycle_keys=recycle_keys, span_release_error=span.release_error
        )

    async def _apply_env_and_change_locked(
        self,
        prepare: Callable[[dict[str, str]], Awaitable[tuple[dict[str, str], Callable[[dict[str, Any]], None]]]],
        *,
        manifest_pointer: str | None = None,
    ) -> tuple[dict[str, Any], Sequence[str]]:
        """STEP 0-3 of :meth:`apply_env_and_change`, the write exclusion held by the caller.

        Returns the persisted manifest and the env change's recycle-class keys for the caller's
        reload tail.
        """
        # STEP 0 — read the stored env ONCE under the exclusion; the caller derives the
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
        recycle_keys = self._validate_env_and_manifest(changes, candidate)
        await self._refuse_held_unpicks(preserved, candidate, env=self.effective_env(changes))

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
        return persisted, recycle_keys

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
        stored_marks = parse_secret_marks(stored.get(SECRET_MARKS_ENV_VAR))
        base = (
            parse_secret_marks(changes[SECRET_MARKS_ENV_VAR]) if SECRET_MARKS_ENV_VAR in changes else list(stored_marks)
        )
        final = list(dict.fromkeys([*base, *sorted(leaving)]))
        if set(final) != set(stored_marks):
            changes[SECRET_MARKS_ENV_VAR] = format_secret_marks(final)

    # -- Profile apply ----------------------------------------------------

    async def apply_replace_env(
        self,
        profile_env: dict[str, str],
        *,
        save_previous: Callable[[dict[str, str]], Awaitable[None]],
        build_and_swap: Callable[..., Awaitable[Epoch]] = build_and_swap_epoch,
        build_and_discard: Callable[..., Awaitable[None]] = build_and_discard_epoch,
        release_llm_pools: Callable[[], Awaitable[None]] = release_loop_bound_resources,
    ) -> ProfileApplyOutcome:
        """Apply a settings profile: the env-write-LAST reload pipeline.

        Ordered so a failed build leaves the STORE untouched and the old surface serving
        (``os.environ`` restored exactly by :func:`build_and_swap_epoch`). Steps 1-4 hold the
        write exclusion, so no other write, in this process or another sharing the store,
        lands between the validation and the env persist; step 5 runs once it is released:

        1. ``_validate_replace`` (X-band payload refusal + dangling ``!ENV`` + backend-
           needs-bus), validated again after the held-unpick check when its inputs moved;
           diff the proposed env against the stored env; refuse a recycle-
           class diff the deployment shape cannot carry; read the applier's own identity
           and the fleet membership the step-5 report is judged against.
        2. Snapshot the current stored env as the reserved ``@previous`` version.
        3. Build the epoch under ``profile_env`` holding the reload gate as its driver
           (:meth:`ReloadGate.driving`): a bus member builds and swaps it in
           (``build_and_swap_epoch``), so the retire waits for THIS door's own request to
           answer apart from the drain budget; a process that is not a bus member (a job
           child a backend worker forked) has no serving surface to swap into, so it builds
           the same epoch and discards it (``build_and_discard_epoch``) — the env is proven
           to build on every door. Right before the build, ``_validate_replace`` runs again when
           its inputs moved since step 1. A build failure re-raises and this method returns
           nothing persisted.
        4. ``replace_env`` — the LAST env write, reached only after a successful build.
        5. Broadcast the reload to the fleet, then roll a recycle across it for the
           recycle-class diff over the targets pinned in step 1 (the applier's own
           recycle is a deferred self-exit, armed only when the roll converged). A
           roll that stops — on a target, or because the bus could not be read — is
           logged at ERROR and returned as the outcome's partial recycle report, never
           raised: the env is persisted and the fleet reloaded. The outcome carries the
           operation's verdict — the workers still awaiting a respawn after the roll — and a
           non-empty verdict is ERROR-logged once.

        ``build_and_swap`` defaults to the running primitive and is injectable (the recycle roll
        through the service's ``orchestrate``) so the ordering / drain / recycle contract is
        exercised in isolation.
        """
        # STEP 1-4 run under the write exclusion, so no other write, in this process or another
        # sharing the store, lands between this apply's validation and its env persist. The broadcast and the recycle
        # roll (STEP 5) run once it is released.
        async with self._write_exclusion() as span:
            # STEP 1 — validate the whole-env replace (payload-scoped X-band refusal, dangling
            # !ENV, backend-needs-bus). A failure raises before anything is snapshotted/built.
            validated = self._env_validation_inputs()
            self._validate_replace(profile_env)
            # An ``!ENV`` marker in an ``mcp`` selection list resolves against the new env, so the
            # replace may stop picking a tool something still uses.
            document = self._read_preserved_manifest()
            await self._refuse_held_unpicks(document, document, env=self._effective_replace_env(profile_env))
            # Validated again when the process env, the stored env or manifest, or the live serving
            # generation changed while the held-unpick check awaited.
            if not self._env_validation_inputs_unchanged(validated):
                validated = self._env_validation_inputs()
                self._validate_replace(profile_env)

            # STEP 2 — diff the proposed env vs the current stored env; classify each diff key
            # by its live-registry reload_class (names only — never values).
            # STEP 1b — refuse a recycle-class diff this deployment shape cannot carry, upfront
            # and naming the key, so an unappliable profile never reaches the build.
            stored = self._read_stored_env()
            hot, recycle_diff_keys = refuse_unrecyclable_env_change(stored, profile_env, change="this settings profile")

            # No key classification tells a serve-affecting key from a backend-only one, so the
            # CONSERVATIVE reading applies: ANY recycle-class diff on a recycle-supported shape
            # affects the serve surface, so the applier self-exits. This over-fires on a purely backend-only diff
            # (an unnecessary serve respawn) but always converges. (Bare shape was refused above,
            # so a non-empty recycle diff here is always on a supported shape.)
            serve_affecting = bool(recycle_diff_keys)

            # STEP 1c — the applying worker's own identity (name + generation), the source
            # for the self-deferred row's ``generation_before`` and the recycle exclusion.
            # The applier is the bus member this process acts for: a job child a backend worker
            # forked applies on that worker's behalf.
            bus = cast("WorkerBus", self._bus)
            self_identity = bus.member_identity

            # STEP 1d — pin what the STEP 5 report is judged against BEFORE the swap in STEP 3
            # can age it out: the reload's expected-confirmation membership and, for a
            # recycle-class diff, the recycle targets. The swap is this door's local apply and
            # the slowest one there is; a sibling whose presence faded across the rebuild (or
            # whose slot was lost and is re-minting) would otherwise never be expected by the
            # reload nor targeted by the roll, and either would report converged without it.
            at_start = await snapshot_membership(self._bus, "reload_config")
            pinned_targets = await self._pin_recycle_targets(self_identity) if recycle_diff_keys else {}

            # STEP 2 (persist reserved snapshot) — save the CURRENT stored env as @previous
            # BEFORE the swap. A later failed build leaves it harmlessly reflecting the
            # unchanged live state.
            await save_previous(stored)

            # STEP 3 — build a fresh epoch under the proposed env and swap it in atomically.
            # HOLD the reload gate across the swap: the op is reload_gated (which only rejects
            # a CONCURRENT reload at the route edge), but a direct build_and_swap needs the
            # serving-loop-owned lock HELD, entered through ``driving()`` so this door's own
            # request (or supervised run) is recorded as the swap's driver; a build failure
            # re-raises here (env restored, store untouched) — STEP 4 never runs.
            #
            # HOLD THE FORK GATE TOO. This door drives ``build_and_swap`` directly rather than
            # through ``reload_gate.run``, so it does not inherit that path's fork exclusion —
            # yet its rebuild re-imports the manifest modules exactly the same way. A backend
            # work loop that forks a job child mid-reimport leaves the child deadlocked on an
            # inherited ``importlib`` per-module lock. The async façade holds the gate on a
            # dedicated thread so ownership stays single-threaded and this loop is never
            # blocked while job spans drain.
            #
            # The build is this door's proof that the env builds, on every path. A process that
            # is not a bus member (a job child a backend worker forked) has no serving surface to
            # swap into: it builds the same epoch and discards it, and its member converges on
            # the persisted env as a sibling through the STEP 5 broadcast.
            async with reload_gate.driving(), fork_gate.exclusive_async(timeout=FORK_QUIESCE_SECONDS):
                # Release the loop-bound kit resources BEFORE the build's settings reset drops
                # their per-loop registries: the build opens with ``reset_all_settings()``, whose
                # resource-registry reset REFUSES to drop a registry still holding live resources
                # on a running loop (a prior agent run leaves a cached checkpoint saver behind).
                # AppLifecycle._reload_config makes the same release before its own build.
                await release_llm_pools()
                # Validated again, last, when the process env, the stored env or manifest, the bus
                # setting or the live serving generation changed since the check above: no await
                # stands between this check and the build, and the write exclusion keeps every
                # other write out until the env persists.
                if not self._env_validation_inputs_unchanged(validated):
                    self._validate_replace(profile_env)
                await (build_and_swap if bus.identity.member else build_and_discard)(profile_env)

            # STEP 4 — env-write-LAST: persist the proposed env as the WHOLE stored map only
            # after the build succeeded. A failed build never reaches this line.
            self._config_manager.replace_env(profile_env)

        # STEP 5 — broadcast the reload to the fleet (each sibling re-reads the persisted
        # store), then roll a recycle for the recycle-class diff. The applier's own recycle
        # is deferred to the post-response self-exit, reported as the applier self-entry.
        fleet = await self._broadcast_profile_reload(
            at_start, swapped=bus.identity.member, span_release_error=span.release_error
        )
        recycle_report: RecycleReport | None = None
        if recycle_diff_keys:
            recycle_report = await self._roll_recycle(
                self_identity, pinned_targets, serve_affecting=serve_affecting, change="the profile apply"
            )

        outcome = ProfileApplyOutcome(
            hot=hot,
            recycle=recycle_report,
            self_identity=self_identity,
            serve_affecting=serve_affecting,
            fleet=fleet,
            span_release_error=span.release_error,
        )
        # The verdict belongs to this operation, which knows what its roll carried.
        verdict = awaiting_respawn_after(fleet, recycle_report, self_identity, self_exit_armed=outcome.self_exit_armed)
        log_awaiting_respawn(verdict)
        return dataclasses.replace(outcome, awaiting_respawn=verdict)
