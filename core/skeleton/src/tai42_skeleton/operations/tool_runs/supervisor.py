"""Supervisor lifecycle and the per-worker concurrency-slot accounting for tool runs.

A supervisor wraps each run: it refreshes a per-run liveness key while the tool
runs, writes the terminal record when the tool returns or raises, and in
``finally`` cancels the liveness refresher. Spawned tasks are held so the event
loop keeps a strong reference and each is tagged with the epoch that admitted its
run, so an epoch retire drains only that generation's in-flight runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from contextlib import suppress
from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.interactions import SuspendedInteraction
from tai42_contract.secrets import mask_secrets
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.utils.detached_util import mark_detached_run, reset_detached_run

import tai42_skeleton.operations.tool_runs as _pkg
from tai42_skeleton.interactions.origin import reset_interaction_origin, set_interaction_origin
from tai42_skeleton.routers.tool_runs_settings import ToolRunsSettings, tool_runs_store_configured

from .models import _DEFAULT_CANCEL_REASON, _FAILED, _PARKED, _SUCCEEDED
from .store import ToolRunStore

logger = logging.getLogger(__name__)

# The ``operations.tool_runs`` package this submodule belongs to. A door reads every
# package-alias seam symbol (``client_ctx``, ``tool_runs_settings``, ``_now`, …) and the
# homed ``_ACTIVE_RUNS`` counter THROUGH this alias at call time, so a test's
# ``setattr(operations.tool_runs, …)`` on this same generation's package takes effect.
# Bound to THIS generation's package (reload re-imports each submodule, re-binding it).


# The spawned supervisor tasks are held here so the event loop keeps a strong
# reference (``asyncio`` only holds a weak one) — a dropped task would be
# garbage-collected mid-run. Each task removes itself on completion. Every task is
# tagged with the serving epoch that ADMITTED its run, so an epoch retire drains only
# that generation's in-flight runs and never a run admitted on the fresh epoch.
_SUPERVISORS: set[asyncio.Task[None]] = set()
_SUPERVISOR_EPOCH: dict[asyncio.Task[None], int] = {}


def reserve_active_slot(limit: int) -> bool:
    """Synchronously claim one of ``limit`` per-worker concurrency slots.

    Reads and increments the package-homed ``_ACTIVE_RUNS`` counter through the
    package object so the single mutable binding is shared with the submit door's
    reset patch. Check-and-increment is done with no ``await`` between the read and
    the ``+= 1``, so two concurrent submits cannot both pass the check on the single
    event loop. Returns ``False`` (no slot taken) when ``limit <= _ACTIVE_RUNS``."""
    if limit <= _pkg._ACTIVE_RUNS:
        return False
    _pkg._ACTIVE_RUNS += 1
    return True


def release_active_slot() -> None:
    """Return one concurrency slot to the pool — the ``-= 1`` the submit door's
    ``except`` and each supervisor's done-callback perform on the package-homed
    ``_ACTIVE_RUNS`` counter."""
    _pkg._ACTIVE_RUNS -= 1


def _enroll_supervisor(task: asyncio.Task[None]) -> None:
    """Register ``task`` in the drain registry and tag it with the epoch that ADMITTED
    its run, so a whole-server drain cancels-and-awaits it and an epoch retire drains
    exactly this generation's runs — sequencing its terminal write ahead of the pooled
    clients' close. A ``None`` epoch (a loop-less unit context) tags nothing."""
    from tai42_skeleton.app.epoch import current_epoch_or_none

    _SUPERVISORS.add(task)
    epoch = current_epoch_or_none()
    if epoch is not None:
        _SUPERVISOR_EPOCH[task] = epoch.number


def _discard_supervisor(task: asyncio.Task[None]) -> None:
    """Drop the drain registry's strong reference and epoch tag for a finished
    supervisor. Reserves no ``_ACTIVE_RUNS`` slot to release — the caller that reserved
    one (the submit door) releases it in its own done-callback."""
    _SUPERVISORS.discard(task)
    _SUPERVISOR_EPOCH.pop(task, None)


def _spawn_supervisor(run_id: str, tool_name: str, arguments: dict[str, Any]) -> None:
    """Detach the task that runs ``tool_name`` and persists its outcome.

    The task must be spawned HERE so it copies the submitting context: that carries a
    bound execution identity into the run, which is what authorizes the dispatch
    :func:`_supervise` makes long after the submitting fire released its binding."""
    task = asyncio.create_task(_supervise(run_id, tool_name, arguments))
    _enroll_supervisor(task)
    task.add_done_callback(lambda t: _on_supervisor_done(t, run_id, tool_name))


def _on_supervisor_done(task: asyncio.Task[None], run_id: str, tool_name: str) -> None:
    """Done-callback for a supervisor task: drop the strong reference AND surface a
    failure at completion time.

    The supervisor's inner ``try`` persists a tool that raises as a ``failed``
    record, but a failure BEFORE it (e.g. the ``client_ctx`` enter raising because
    Redis died after submit) escapes that guard — asyncio would then report it only
    via the nondeterministic 'never retrieved' message at GC. Logging it here with
    the ``run_id``/``tool_name`` makes it a timely, attributable signal. A
    cancellation (test teardown / shutdown) is the normal stop and stays silent."""
    _discard_supervisor(task)
    # Release the concurrency slot the submit door reserved for this run. Every
    # spawned supervisor reaches this callback exactly once, so the count returns
    # to the submit door's increment.
    release_active_slot()
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("tool-run %s (%s) supervisor task failed", run_id, tool_name, exc_info=exc)


async def _refresh_liveness_loop(r: Any, store: ToolRunStore, run_id: str, settings: ToolRunsSettings) -> None:
    """Re-set the run's liveness key every ``liveness_ttl_seconds / 3`` — a
    constant cadence — so a live run (including a slow sync tool offloaded to a
    thread, which leaves the loop free to run this task) never looks ``lost``.

    A transient failure of a single refresh is logged and the loop CONTINUES: one
    failed ``SET`` must never stop the refresher, or a still-``running`` run would
    lose liveness while alive and be wrongly reconciled to ``lost``."""
    cadence = settings.liveness_ttl_seconds / 3
    while True:
        try:
            await store.refresh_liveness(r, run_id, settings.liveness_ttl_seconds)
        except Exception:
            # Loud, not silent: log and keep refreshing on the next cadence rather
            # than letting one failed SET permanently kill the refresher.
            logger.warning("tool-run %s liveness refresh failed; retrying next cadence", run_id, exc_info=True)
        await asyncio.sleep(cadence)


async def _supervise(
    run_id: str, tool_name: str, arguments: dict[str, Any], *, propagate_failure: bool = False
) -> None:
    """Run ``tool_name`` and persist its terminal record, refreshing liveness while it runs.

    ``propagate_failure`` governs a RAISING tool: the failure is recorded either way, but
    when set the ORIGINAL exception is re-raised AFTER the record is written, so an inline
    caller's own failure surfacing (the hooks fan-out's per-hook error log) still fires.
    The detached submit supervisor leaves it off — it is the top of its task, with no caller
    to propagate to, so a recorded failure is the whole outcome (a re-raise would only reach
    the done-callback's generic task-failure log)."""
    settings = _pkg.tool_runs_settings()
    store = ToolRunStore(settings.key_prefix)
    async with _pkg.client_ctx(RedisClient, settings.redis) as r:
        refresher = asyncio.create_task(_refresh_liveness_loop(r, store, run_id, settings))
        # Bind this run's id as the interaction origin for the tool body, so a
        # question the tool raises through ``ask_user`` is attributed to the run.
        origin_token = set_interaction_origin(run_id)
        # Detached: this run has no live caller holding a connection, so the turn budget
        # does not apply — covers a background submit AND a store-ON hook fire.
        # The secret-read capability is deliberately NOT rebound here: an in-process
        # submit inherits the submitting request's own bound capability, which is the
        # same identity-following model the detached-worker leg carries the submitter's
        # capability to — the capability follows the identity the run acts as.
        detached_token = mark_detached_run()
        tool_error: Exception | None = None
        parked: SuspendedInteraction | None = None
        result_json = ""
        try:
            try:
                result = await tai42_app.tools.run_tool(tool_name, arguments, offload_sync=True)
                if isinstance(result, SuspendedInteraction):
                    # The tool async-parked (a generic contract sentinel): the run has NOT
                    # succeeded — its answer is delivered out of band by the tool's own resumer —
                    # so it terminates PARKED, keyed by the parked interaction id, never a
                    # succeeded record over an unfinished run.
                    parked = result
                else:
                    # ``run_tool`` already json-normalizes the body; a residual dumps
                    # failure surfaces as a ``failed`` record rather than a lost run. A
                    # background run has no live-caller door, so any wrapped secret is
                    # masked to the placeholder before it lands in the durable record.
                    result_json = json.dumps(mask_secrets(result))
            except asyncio.CancelledError as cancel:
                # A drain (process shutdown OR an epoch retire) cancelled this run
                # mid-flight. Record it as ``failed`` through the same one-way CAS the
                # normal path uses (so a record already reconciled to ``lost`` is never
                # overwritten), naming the ACTUAL cause the drain passed as the cancel
                # message, then re-raise so the cancellation propagates to the drain
                # handler. Safe to await here: the drain cancels each task exactly once,
                # then waits.
                reason = cancel.args[0] if cancel.args else _DEFAULT_CANCEL_REASON
                fields = {
                    "status": _FAILED,
                    "finished_at": _pkg._now().isoformat(),
                    "error": reason,
                }
                persisted = await store.mark_terminal_if_running(r, run_id, fields, settings.result_ttl_seconds)
                if not persisted:
                    logger.warning(
                        "tool-run %s (%s) was cancelled at shutdown but the record was already "
                        "reconciled to lost; terminal write skipped (one-way lost)",
                        run_id,
                        tool_name,
                    )
                raise
            except Exception as exc:
                # Persist the raised error as record data so the requester reads it; logged
                # too, never dropped. An inline caller additionally re-raises it below.
                logger.exception("tool-run %s (%s) failed", run_id, tool_name)
                tool_error = exc
                fields = {"status": _FAILED, "finished_at": _pkg._now().isoformat(), "error": str(exc)}
            else:
                if parked is not None:
                    fields = {
                        "status": _PARKED,
                        "finished_at": _pkg._now().isoformat(),
                        "result": json.dumps(
                            {
                                "interaction_id": parked.interaction_id,
                                "expiry_at": parked.expiry_at.isoformat() if parked.expiry_at is not None else None,
                            }
                        ),
                    }
                else:
                    fields = {"status": _SUCCEEDED, "finished_at": _pkg._now().isoformat(), "result": result_json}
            # Gate the terminal write on the record still being ``running`` so it
            # can never overwrite a ``lost`` a reader already wrote (one-way lost).
            persisted = await store.mark_terminal_if_running(r, run_id, fields, settings.result_ttl_seconds)
            if not persisted:
                logger.warning(
                    "tool-run %s (%s) finished as %s but the record was already reconciled to lost; "
                    "terminal write skipped (one-way lost)",
                    run_id,
                    tool_name,
                    fields["status"],
                )
            # Terminal record written; only now let the failure propagate to an inline
            # caller so its own surfacing runs on top of the recorded failure.
            if tool_error is not None and propagate_failure:
                raise tool_error
        finally:
            reset_detached_run(detached_token)
            reset_interaction_origin(origin_token)
            refresher.cancel()
            with suppress(asyncio.CancelledError):
                await refresher


async def run_recorded(tool_name: str, arguments: dict[str, Any]) -> None:
    """Execute ``tool_name`` under the CURRENTLY bound execution identity, writing the
    SAME full run-record lifecycle (running -> succeeded/failed) a background submit
    writes — so a hook- or trigger-dispatched fire is listable via ``GET /api/tool-runs``
    and gettable by run id exactly as a submitted run, attributed to and indexed under the
    fire's execution key.

    Store OFF (no tool-run Redis configured): the tool still runs, unrecorded — the same
    OFF semantics as the rest of the tool-run surface, no error and no warning.

    Reserves NO ``max_concurrent_runs`` slot and detaches no supervisor task: the run is
    awaited inline and its capacity is the CALLER's to bound (the hooks manager's
    ``max_workers`` semaphore), never the submit door's per-worker slot pool. A failure
    creating the record propagates loudly to the caller."""
    from . import reconcile

    if not tool_runs_store_configured():
        # Detached, store OFF: run unrecorded but still with no live caller, so the
        # turn budget is skipped here exactly as on the store-ON path. The tool is
        # thread-offloaded like every other execution site, so a synchronous tool never
        # blocks the event loop — the OFF branch only skips recording, not the offload.
        detached_token = mark_detached_run()
        try:
            await tai42_app.tools.run_tool(tool_name, arguments, offload_sync=True)
        finally:
            reset_detached_run(detached_token)
        return

    settings = _pkg.tool_runs_settings()
    store = ToolRunStore(settings.key_prefix)
    # The owning identity is the fire's own bound key — request_identity reads it from the
    # bound execution identity — so the record is attributed and per-identity indexed
    # exactly as a restricted submit's is.
    user_id, _restricted = _pkg.request_identity()
    # Read the tool's registration meta BY NAME for the generic crash-resume flag
    # (absent → False): the recording caller holds only tool_name/arguments/user_id and
    # cannot address the record, so the flag comes from the registration meta, never a
    # record write. The skeleton just READS a platform-generic meta key and STORES a
    # generic bool + a generic argument blob — it never names the consumer.
    crash_resume = await reconcile._tool_declares_crash_resume(tool_name)
    run_id = secrets.token_urlsafe(16)
    started = _pkg._now()
    async with _pkg.client_ctx(RedisClient, settings.redis) as r:
        await store.create_run(
            r,
            run_id,
            tool_name,
            started.isoformat(),
            started.timestamp(),
            settings,
            user_id=user_id,
            arguments=arguments,
            crash_resume=crash_resume,
        )
    # Run under a supervisor task ENROLLED in the drain registry exactly like a submitted
    # run — so a drain (process shutdown or an epoch retire) cancels-and-awaits it and its
    # terminal write lands BEFORE the pooled clients close, never leaving a completed run
    # stuck ``running`` to reconcile to ``lost``. The task reserves NO ``max_concurrent_runs``
    # slot (its capacity is the hooks manager's ``max_workers`` semaphore), so its
    # done-callback only clears the registry, never the submit door's ``_ACTIVE_RUNS`` count.
    # The run is still awaited INLINE, so ``propagate_failure`` re-raises a tool failure to
    # the fan-out's per-hook error log exactly as a direct await would.
    task = asyncio.create_task(_supervise(run_id, tool_name, arguments, propagate_failure=True))
    _enroll_supervisor(task)
    task.add_done_callback(_discard_supervisor)
    try:
        await task
    except asyncio.CancelledError:
        # A drain cancelled the enrolled run task directly: its terminal ``failed`` write
        # already landed and the task is done — surface the cancellation. If instead THIS
        # awaiting context was cancelled (e.g. the request drain severs the firing request)
        # the run task is still live: propagate the cancel so it writes its terminal record,
        # then await it — never orphaning the enrolled task nor double-cancelling a done one.
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        raise


async def drain_supervisors(
    deadline: float | None = None,
    *,
    epoch: int | None = None,
    reason: str = _DEFAULT_CANCEL_REASON,
) -> None:
    """Cancel in-flight supervisors and wait, bounded, for each to write its terminal
    ``failed`` record naming ``reason``.

    Reused by two retire paths: process shutdown (:func:`_drain_supervisors`, all runs)
    and an epoch retire (a settings-profile apply swaps in a fresh serving surface and
    retires the old one). With ``epoch`` given, cancels ONLY the runs that generation
    admitted — a run admitted on the fresh epoch during the retire is left running;
    with ``epoch`` ``None`` it drains every run. Both cancel bounded by a budget so the
    retire always proceeds; a supervisor that misses the window is logged loudly and its
    run reconciles to ``lost`` later — an explicit, logged recovery, never a silent one.
    ``reason`` is passed as the cancel message so each cancelled run records the actual
    cause. ``deadline`` is a drain budget in SECONDS — a relative duration, not an
    absolute time (mirrors the kit's ``drain_epoch`` vocabulary); defaults to
    ``shutdown_drain_seconds``."""
    tasks = [t for t in _SUPERVISORS if not t.done() and (epoch is None or _SUPERVISOR_EPOCH.get(t) == epoch)]
    if not tasks:
        return
    budget = deadline if deadline is not None else _pkg.tool_runs_settings().shutdown_drain_seconds
    for t in tasks:
        t.cancel(reason)
    _done, pending = await asyncio.wait(tasks, timeout=budget)
    if pending:
        logger.error(
            "tool-runs retire: %d supervisor(s) did not finish their terminal write within "
            "the drain budget; those runs will reconcile to lost",
            len(pending),
        )


@tai42_app.lifecycle.on_shutdown
async def _drain_supervisors() -> None:
    """Cancel every in-flight supervisor at shutdown and drain them bounded by
    ``shutdown_drain_seconds``.

    Shutdown handlers run BEFORE ``_teardown_resources`` closes the pooled clients,
    so a cancelled supervisor still has a live Redis to write through."""
    await drain_supervisors(reason="the server is shutting down before the tool-run completed")
