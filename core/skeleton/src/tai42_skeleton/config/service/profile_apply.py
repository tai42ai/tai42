"""The settings-profile apply: the env-write-LAST variant of the pipeline.

A profile apply replaces the whole stored env. It validates, snapshots the stored env as
``@previous``, builds the epoch under the proposed env, and persists that env only after the
build succeeded; then it broadcasts the reload and rolls a recycle across the fleet for its
recycle-class diff.

The recycle roll follows the broadcast under the pipeline's post-persist contract: a roll
that stops (a target timed out, failed, or never returned to ready, or the bus could not
be read) is a loud ERROR log and the partial recycle report in the returned outcome,
never an exception past :meth:`_ProfileApplyMixin.apply_replace_env`; only a bus contract
violation (a reply that does not name the published target), or a bus fault that is not
an outage, escapes.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, cast

from tai42_kit.fork_gate import fork_gate
from tai42_kit.llm import release_loop_bound_resources

from tai42_skeleton.app.epoch import Epoch, build_and_discard_epoch, build_and_swap_epoch
from tai42_skeleton.app.reload_gate import FORK_QUIESCE_SECONDS, reload_gate
from tai42_skeleton.config.boundary import refuse_unrecyclable_env_change
from tai42_skeleton.config.service.broadcast import _BroadcastMixin
from tai42_skeleton.config.service.results import ProfileApplyOutcome
from tai42_skeleton.config.service.validation import _ValidationMixin
from tai42_skeleton.operations._broadcast import awaiting_respawn_after, log_awaiting_respawn, snapshot_membership

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from tai42_skeleton.app.bus import WorkerBus
    from tai42_skeleton.app.recycle import RecycleReport


class _ProfileApplyMixin(_ValidationMixin, _BroadcastMixin):
    """The settings-profile apply, validated by the validation mixin and broadcast by the broadcast tail."""

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

        1. Read the fleet's deployment ownership once from the census (every step below
           judges by it); ``_validate_replace`` (X-band payload refusal + deployment-owned
           refusal + dangling ``!ENV`` + backend-needs-bus), validated again after the
           held-unpick check when its inputs moved; diff the proposed env against the
           stored env, both without the keys the deployment sets (the change the reload really
           produces); refuse a diff no recycle can carry; read the applier's own identity
           and the fleet membership the step-5 report is judged against.
        2. Snapshot the stored env that counts — the stored keys the deployment does not set —
           as the reserved ``@previous`` version. A stored copy of a deployment key is not
           snapshotted: it never applies, and no write may set one, so applying ``@previous``
           restores every key that counted before this apply, as the deployment env stood at
           this apply. A key the deployment sets later is refused on ``@previous`` as on any
           profile.
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
            validated = self._env_validation_inputs()
            self._validate_replace(profile_env, ownership)
            # An ``!ENV`` marker in an ``mcp`` selection list resolves against the new env, so the
            # replace may stop picking a tool something still uses.
            document = self._read_preserved_manifest()
            await self._refuse_held_unpicks(document, document, env=self._effective_replace_env(profile_env, ownership))
            # Validated again when the process env, the stored env or manifest, or the live serving
            # generation changed while the held-unpick check awaited.
            if not self._env_validation_inputs_unchanged(validated):
                validated = self._env_validation_inputs()
                self._validate_replace(profile_env, ownership)

            # STEP 2 — diff the proposed env vs the current stored env, the keys the deployment
            # sets dropped from both; classify each diff key by its live-registry reload_class
            # (names only — never values).
            # STEP 1b — refuse a diff no recycle can carry (a bus-reaching key), upfront and
            # naming the key, so an unappliable profile never reaches the build.
            stored = self._read_stored_env()
            hot, recycle_diff_keys = refuse_unrecyclable_env_change(
                stored, profile_env, ownership, change="this settings profile"
            )

            # No key classification tells a serve-affecting key from a backend-only one, so the
            # CONSERVATIVE reading applies: ANY recycle-class diff affects the serve surface, so
            # the applier self-exits. This over-fires on a purely backend-only diff (an
            # unnecessary serve respawn) but always converges.
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
            pinned_targets = (
                await self._pin_recycle_targets(self_identity, change="the profile apply") if recycle_diff_keys else {}
            )

            # STEP 2 (persist reserved snapshot) — save the stored env that counts as @previous
            # BEFORE the swap. A later failed build leaves it harmlessly reflecting the
            # unchanged live state.
            await save_previous(ownership.counting(stored))

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
                    self._validate_replace(profile_env, ownership)
                await (build_and_swap if bus.identity.member else build_and_discard)(profile_env, ownership=ownership)

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
