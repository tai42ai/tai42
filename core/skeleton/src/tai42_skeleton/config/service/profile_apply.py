"""The settings-profile apply: the env-write-LAST variant of the pipeline.

A profile apply replaces the whole stored env. It validates, snapshots the stored env as
``@previous``, builds the epoch under the proposed env, and persists that env only after the
build succeeded; then it broadcasts the reload and settles the fleet recycle the reload's report
calls for, through the tail every pipeline shares (:meth:`_BroadcastMixin._settle_recycle`).

The recycle roll follows the broadcast under the pipeline's post-persist contract: a roll
that stops (a target timed out, failed, or never returned to ready, or the bus could not
be read) is a loud ERROR log and the partial recycle report in the returned outcome,
never an exception past :meth:`_ProfileApplyMixin.apply_replace_env`. A roll that fails
instead — its hand-off to the bus member a bounded run acts for failed, or the roll raised
(a bus contract violation, such as a reply that does not name the published target, or a
bus fault that is not an outage) — escapes as the
:class:`~tai42_skeleton.operations._broadcast.FleetBroadcastError` of a persisted env,
carrying the fleet report and the outcome the failed roll left
(:meth:`_BroadcastMixin._settle_recycle`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from tai42_skeleton.app.epoch import Epoch, build_and_discard_epoch, build_and_swap_epoch
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.config.boundary import refuse_unrecyclable_env_change
from tai42_skeleton.config.service.broadcast import _BroadcastMixin
from tai42_skeleton.config.service.results import ProfileApplyOutcome
from tai42_skeleton.config.service.validation import _ValidationMixin

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from tai42_skeleton.app.bus import WorkerBus
    from tai42_skeleton.config.service.validation import _ManifestValidationInputs


class _ProfileApplyMixin(_ValidationMixin, _BroadcastMixin):
    """The settings-profile apply, validated by the validation mixin and broadcast by the broadcast tail."""

    async def apply_replace_env(
        self,
        profile_env: dict[str, str],
        *,
        save_previous: Callable[[dict[str, str]], Awaitable[None]],
        build_and_swap: Callable[..., Awaitable[Epoch]] = build_and_swap_epoch,
        build_and_discard: Callable[..., Awaitable[None]] = build_and_discard_epoch,
    ) -> ProfileApplyOutcome:
        """Apply a settings profile: the env-write-LAST reload pipeline.

        Ordered so a failed build leaves the STORE untouched and the old surface serving
        (``os.environ`` restored by :func:`build_and_swap_epoch`). Steps 1-4 hold the
        write exclusion, so no other write, in this process or another sharing the store,
        lands between the validation and the env persist; step 5 runs once it is released:

        1. Read the fleet's deployment ownership once from the census (every step below
           judges by it); ``_validate_replace`` (X-band payload refusal + deployment-owned
           refusal + dangling ``!ENV`` + backend-needs-bus), validated again after the
           held-unpick check when its inputs moved; diff the proposed env against the
           stored env, both without the keys the deployment sets (the change the reload really
           produces); refuse a diff no recycle can carry; read the applier's own identity
           and, in one census read, the fleet membership the step-5 report is judged against
           and the recycle targets.
        2. Snapshot the stored env that counts — the stored keys the deployment does not set —
           as the reserved ``@previous`` version. A stored copy of a deployment key is not
           snapshotted: it never applies, and no write may set one, so applying ``@previous``
           restores every key that counted before this apply, as the deployment env stood at
           this apply. A key the deployment sets later is refused on ``@previous`` as on any
           profile.
        3. Build the epoch under ``profile_env`` as a re-importing reload body
           (:meth:`ReloadGate.run`), on the serving loop: a bus member builds and swaps it in
           (``build_and_swap_epoch``), so the retire waits for THIS door's own request to
           answer apart from the drain budget; a process that is not a bus member (a job
           child a backend worker forked) has no serving surface to swap into, so it builds
           the same epoch and discards it (``build_and_discard_epoch``) — the env is proven
           to build on every door. Right before the build, ``_validate_replace`` runs again when
           its inputs moved since step 1. A build failure re-raises and this method returns
           nothing persisted.
        4. ``replace_env`` — the LAST env write, reached only after a successful build.
        5. Broadcast the reload to the fleet, then settle its recycle: the reload's report
           decides which workers need a restart (the targets pinned in step 1 among them)
           and the recycle rolls over them, the applier's own restart a deferred self-exit,
           armed only when it needs one and the roll converged. A roll that stops — on a
           target, or because the bus could not be read — is logged at ERROR and returned as
           the outcome's partial recycle report, never raised: the env is persisted. A roll
           that fails (one a bounded run could not hand to its member among them) surfaces
           as the :class:`~tai42_skeleton.operations._broadcast.FleetBroadcastError` of a
           persisted env, its report naming the roll's failure and its roll the outcome the
           failed roll left (:meth:`_settle_recycle`).
           The outcome carries the operation's verdict — the workers still awaiting a
           respawn after the roll — and a non-empty verdict is ERROR-logged once.

        ``save_previous`` persists the ``@previous`` snapshot it is handed (step 2).
        ``build_and_swap`` defaults to the running primitive and is injectable (the recycle roll
        through the service's ``orchestrate``) so the ordering / drain / recycle contract is
        exercised in isolation.
        """
        # STEP 1-4 run under the write exclusion, so no other write, in this process or another
        # sharing the store, lands between this apply's validation and its env persist. The broadcast and the recycle
        # roll (STEP 5) run once it is released.
        async with self._write_exclusion() as span:
            # STEP 1 — read the fleet's deployment ownership once, then validate the whole-env
            # replace by it (payload-scoped X-band refusal, deployment-owned refusal, dangling
            # !ENV, backend-needs-bus). A failure raises before anything is snapshotted/built.
            ownership = await self.deployment_ownership("apply_replace_env")

            def validate(inputs: _ManifestValidationInputs) -> None:
                self._validate_replace(profile_env, ownership, inputs)

            validated = await self._env_validation_inputs()
            validate(validated.process)
            # An ``!ENV`` marker in an ``mcp`` selection list resolves against the new env, so the
            # replace may stop picking a tool something still uses.
            document = self._read_preserved_manifest()
            await self._refuse_held_unpicks(document, document, env=self._effective_replace_env(profile_env, ownership))
            # Validated again when the process env, the stored env or manifest, or the live serving
            # generation or its tool surface changed while the held-unpick check awaited.
            validated = await self._validate_again_if_moved(validated, validate)

            # STEP 2 — diff the proposed env vs the current stored env, the keys the deployment
            # sets dropped from both; classify each diff key by its live-registry reload_class
            # (names only — never values).
            # STEP 1b — refuse a diff no recycle can carry (a bus-reaching key), upfront and
            # naming the key, so an unappliable profile never reaches the build.
            stored = self._read_stored_env()
            hot = refuse_unrecyclable_env_change(stored, profile_env, ownership, change="this settings profile")

            # STEP 1c — the applying worker's own identity (name + generation), the source
            # for the self-deferred row's ``generation_before`` and the recycle exclusion.
            # The applier is the bus member this process acts for: a job child a backend worker
            # forked applies on that worker's behalf.
            bus = cast("WorkerBus", self._bus)
            writer = bus.member_identity

            # STEP 1d — pin what the STEP 5 report is judged against BEFORE the swap in STEP 3
            # can age it out: the reload's expected-confirmation membership and the recycle
            # targets, from one census read. The swap is this door's local apply and the slowest
            # one there is; a sibling whose presence faded across the rebuild (or whose slot was
            # lost and is re-minting) would otherwise never be expected by the reload nor
            # targeted by the roll, and either would report converged without it.
            at_start, pinned = await self._census_at_start(writer, "reload_config", targets=None)

            # STEP 2 (persist reserved snapshot) — save the stored env that counts as @previous
            # BEFORE the swap. A later failed build leaves it harmlessly reflecting the
            # unchanged live state.
            await save_previous(ownership.counting(stored))

            # STEP 3 — build a fresh epoch under the proposed env and swap it in atomically,
            # entering the reload gate like every reload body (``reload_gate.run``, re-importing):
            # it holds the gate as its driver and the fork gate, and runs the build on the serving
            # loop; a build failure re-raises here (env restored, store untouched) — STEP 4 never
            # runs. A process that is not a bus member (a job child a backend worker forked) has
            # no serving surface to swap into: it builds the same epoch and discards it.
            async def _build() -> None:
                # Validated again, last, against its inputs re-taken here on the serving loop, when
                # the process env, the stored env or manifest, the bus setting, the live serving
                # generation or its tool surface changed since the check above: no await stands
                # between this check and the build, and the write exclusion keeps every other write
                # out until the env persists.
                await self._validate_again_if_moved(validated, validate)
                await (build_and_swap if bus.identity.member else build_and_discard)(profile_env, ownership=ownership)

            await reload_gate.run(_build, reimports=True)

            # STEP 4 — env-write-LAST: persist the proposed env as the WHOLE stored map only
            # after the build succeeded. A failed build never reaches this line.
            self._config_manager.replace_env(profile_env)

        # STEP 5 — broadcast the reload to the fleet (each sibling re-reads the persisted
        # store), then settle the recycle its report calls for. The applier's own restart, when
        # it needs one, is left to the post-response self-exit, armed only on a converged roll
        # and then reported as the applier self-entry.
        fleet, local = await self._broadcast_profile_reload(
            at_start, writer, pinned, swapped=bus.identity.member, span_release_error=span.release_error
        )
        roll = await self._settle_recycle(
            fleet, writer, pinned, local=local, change="the profile apply", span_release_error=span.release_error
        )
        if roll is None:
            return ProfileApplyOutcome(
                hot=hot,
                decision=None,
                recycle=None,
                fleet=fleet,
                span_release_error=span.release_error,
                roll_deferred=True,
            )
        return ProfileApplyOutcome(
            hot=hot,
            decision=roll.decision,
            recycle=roll.recycle,
            fleet=fleet,
            awaiting_respawn=roll.awaiting_respawn,
            span_release_error=span.release_error,
            roll_deferred=roll.deferred,
            roll_deferred_to=roll.deferred_to,
            self_exit_armed=roll.self_exit_armed,
        )
