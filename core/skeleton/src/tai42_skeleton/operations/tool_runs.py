"""Background tool-run operations — submit, get-by-id, and per-tool list.

The synchronous ``POST /api/run-tool`` door holds one request open for the whole
tool call; a dropped connection loses the result even though the tool finished
server-side. These operations detach the run from the request:

* ``submit_run`` — takes ``tool_name`` + ``arguments`` (the same field shape the
  sync door's parser enforces), returns ``{"run_id": ...}`` at once (the route
  answers ``202``) and executes the tool as an in-process background task through
  the SAME ``tai42_app.tools.run_tool`` seam the sync door uses — with
  ``offload_sync`` set, so a blocking sync tool runs on a worker thread and cannot
  starve the supervisor's liveness refresh. A "run any tool by name" door, so it
  is a tier-1 meta-executor (never projected to the MCP surface, like
  ``run_tool``).
* ``get_run`` — the run record ``{run_id, tool_name, status, started_at,
  finished_at?, result?, error?}``; an unknown/expired id is a loud 404.
  ``status ∈ running | succeeded | failed | lost``.
* ``list_tool_runs`` — the recent runs for one tool (id, tool name, status,
  timestamps only — never ``result``/``error``), newest first, from a per-tool
  ZSET trimmed to ``ToolRunsSettings.recent_runs_limit``.

Per-identity isolation: a run records the OWNING identity of its submitter (always
the caller's OWN id — each key is its own island, never sharing its owner's or a
sibling owned key's slice) and is indexed under a per-identity
``recent:{user_id}:{tool_name}`` window in addition to the shared per-tool window. A
restricted caller reads and prunes only its own per-identity window (complete within
its own bound, never truncated by other identities' volume) and may GET only a run it
owns — another identity's run id is a loud ``403`` (never a ``404``: the run exists,
it is simply not the caller's). An unrestricted caller keeps the full view over the
shared window.

A supervisor wraps each run: it refreshes a per-run liveness key while the tool
runs, writes the terminal record when the tool returns or raises (``succeeded``
+ result, or ``failed`` + the caught error string — the error becomes visible
record data, never swallowed), and in ``finally`` cancels the liveness refresher.
``lost`` is computed-and-persisted one way: the FIRST read of a record still
``running`` whose liveness key has expired writes ``status: lost`` (a dead
process never wrote its terminal record, so it cannot later flip to succeeded).
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field
from tai42_contract.app import tai42_app
from tai42_contract.secrets import mask_secrets
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.utils.detached_util import mark_detached_run, reset_detached_run

from tai42_skeleton.access_control.user import request_identity
from tai42_skeleton.interactions.origin import reset_interaction_origin, set_interaction_origin
from tai42_skeleton.operations import (
    BadRequestError,
    ForbiddenError,
    NotFoundError,
    NotSupportedError,
    UnavailableError,
    operation,
)
from tai42_skeleton.operations._submitted_tool_authz import authorize_submitted_tool
from tai42_skeleton.routers.tool_runs_settings import ToolRunsSettings, tool_runs_settings, tool_runs_store_configured

if TYPE_CHECKING:
    from tai42_skeleton.authz.identity import CallerIdentity

logger = logging.getLogger(__name__)

# The machine-readable code + message the tool-run OFF refusal carries when the
# store is unconfigured. Hoisted so the submit refusal reads one way.
_NOT_CONFIGURED_CODE = "tool-runs-not-configured"
_NOT_CONFIGURED_MESSAGE = "the tool-run store is not configured: set TAI_TOOL_RUNS_REDIS_URL (or TAI_DEFAULT_REDIS_URL)"

# The spawned supervisor tasks are held here so the event loop keeps a strong
# reference (``asyncio`` only holds a weak one) — a dropped task would be
# garbage-collected mid-run. Each task removes itself on completion. Every task is
# tagged with the serving epoch that ADMITTED its run, so an epoch retire drains only
# that generation's in-flight runs and never a run admitted on the fresh epoch.
_SUPERVISORS: set[asyncio.Task[None]] = set()
_SUPERVISOR_EPOCH: dict[asyncio.Task[None], int] = {}

# The default cancellation reason recorded on a run cancelled by a drain — overridden
# per drain caller (epoch retire vs process shutdown) so the run's ``error`` names the
# real cause rather than always claiming a shutdown.
_DEFAULT_CANCEL_REASON = "the tool-run was cancelled before it completed"

# Per-worker count of in-flight background runs, enforced against
# ``max_concurrent_runs``. The submit door increments it synchronously before
# creating a record; each supervisor's done-callback decrements it. Exact on the
# single event loop (no interleaving between the capacity check and the
# increment).
_ACTIVE_RUNS: int = 0

_RUNNING = "running"
_SUCCEEDED = "succeeded"
_FAILED = "failed"
_LOST = "lost"

# The platform-generic registration meta key a consumer sets to opt one of its runs
# into crash-resume (``meta={"tai42/crash_resume": True}``), the existing ``tai42/*``
# meta convention. The skeleton reads it and stores a generic bool; it names no consumer.
_CRASH_RESUME_META_KEY = "tai42/crash_resume"

# Atomic compare-and-set terminal write. ``lost`` is one-way: a reader that finds
# a still-``running`` record whose liveness key has expired writes ``lost``, and
# the supervisor's own terminal write (``succeeded``/``failed``) must never
# overwrite it — nor may a late ``lost`` clobber a terminal the supervisor just
# wrote. Both writes go through this script, which transitions the record ONLY
# while its stored ``status`` is still ``running``, making the read-decide-write
# one atomic server-side step (a Python read-then-write across two awaits could
# interleave with the other writer).
#   KEYS[1] = run_key
#   ARGV[1] = record TTL (seconds); ARGV[2..] = HSET field/value pairs
_TERMINAL_CAS_LUA = """
-- tool_runs:terminal-cas
if redis.call('HGET', KEYS[1], 'status') ~= 'running' then return 0 end
redis.call('HSET', KEYS[1], unpack(ARGV, 2))
redis.call('EXPIRE', KEYS[1], ARGV[1])
return 1
"""


def _now() -> datetime:
    return datetime.now(UTC)


class ToolRunSubmission(BaseModel):
    """A background tool-run submission: the ``tool_name`` and its keyword
    ``arguments``. Mirrors the shape ``read_tool_call`` enforces at runtime."""

    tool_name: str = Field(min_length=1, description="Registered tool name.")
    arguments: dict[str, object] = Field(default_factory=dict, description="Tool keyword arguments.")


class ToolRunsListQuery(BaseModel):
    """The per-tool run listing's ``?tool_name=`` query. ``tool_name`` is REQUIRED — a client
    generated without it calls the door with no tool to list and is answered 400.

    Spec metadata only — the door parses its query at the HTTP edge."""

    tool_name: str = Field(min_length=1, description="The registered tool whose recent runs to list.")


# -- Redis store -------------------------------------------------------------


class ToolRunStore:
    """Every tool-run key shape and the read/write operations behind one class.

    Operations take the redis client as an argument; each caller opens it from
    the tool-runs settings via ``client_ctx(RedisClient, settings.redis)``. Loud
    by contract — no swallowed errors, no silent fallback."""

    def __init__(self, key_prefix: str) -> None:
        self._p = key_prefix

    # -- key shapes ----------------------------------------------------------

    def run_key(self, run_id: str) -> str:
        return f"{self._p}run:{run_id}"

    def liveness_key(self, run_id: str) -> str:
        return f"{self._p}live:{run_id}"

    def recent_key(self, tool_name: str, user_id: str | None = None) -> str:
        """The recent-runs index key for ``tool_name``. With ``user_id`` given, the
        PER-IDENTITY index ``recent:{user_id}:{tool_name}`` a restricted caller reads
        (its own complete window); without it, the shared ``recent:{tool_name}`` index
        an unrestricted caller reads."""
        if user_id is None:
            return f"{self._p}recent:{tool_name}"
        return f"{self._p}recent:{user_id}:{tool_name}"

    # -- writes --------------------------------------------------------------

    async def create_run(
        self,
        r: Any,
        run_id: str,
        tool_name: str,
        started_at: str,
        score: float,
        settings: ToolRunsSettings,
        user_id: str | None = None,
        arguments: dict[str, Any] | None = None,
        crash_resume: bool = False,
    ) -> None:
        """Persist a new ``running`` record, prime its liveness key, and index it in
        the tool's recent-runs ZSET — trimming the ZSET to the newest
        ``recent_runs_limit`` members. The record hash and the index both carry the
        record TTL so a tool that stops being run eventually drops its index.

        ``user_id`` is the OWNING identity of the run — always the caller's own id
        (each key is its own island). When present it is stamped onto
        the record AND the run id is also pushed onto the per-identity index
        ``recent:{user_id}:{tool_name}`` (its own bound/TTL, mirroring the shared
        index), so a restricted caller's list reads a complete window that other
        identities' volume can never truncate. A caller with no bound identity — an
        anonymous or gate-off REQUEST — leaves ``user_id`` absent and writes only the
        shared index; a fire always binds its execution key, gate off included, and is
        attributed to it.

        None of the writes branches on a prior result, so they are all issued in ONE
        pipeline (a single round trip) rather than sequentially."""
        run_key = self.run_key(run_id)
        recent_key = self.recent_key(tool_name)
        record: dict[str, str] = {"tool_name": tool_name, "status": _RUNNING, "started_at": started_at}
        if user_id is not None:
            record["user_id"] = user_id
        # Crash-resume seam: a run whose registration declared the generic crash-resume
        # flag persists its ``arguments`` (JSON) and a generic ``crash_resume`` marker, so
        # the liveness→lost reconciler can replay it FROM SCRATCH under the principal's
        # current live grants. An un-flagged run stores neither and keeps today's behavior
        # byte-for-byte. The arguments are stored raw (not masked) because a from-scratch
        # replay must fire the exact recorded input.
        if crash_resume:
            record["crash_resume"] = "1"
            record["arguments"] = json.dumps(arguments or {})
        pipe = r.pipeline()
        pipe.hset(run_key, mapping=record)
        pipe.expire(run_key, settings.result_ttl_seconds)
        pipe.set(self.liveness_key(run_id), "1", ex=settings.liveness_ttl_seconds)
        pipe.zadd(recent_key, {run_id: score})
        # Trim to the newest N: rank 0..-(limit+1) is every member older than the
        # newest ``limit`` (lowest-scored first), removed in one call.
        pipe.zremrangebyrank(recent_key, 0, -(settings.recent_runs_limit + 1))
        pipe.expire(recent_key, settings.result_ttl_seconds)
        if user_id is not None:
            # The per-identity index mirrors the shared index's shape exactly (same
            # bound, same TTL) so a restricted caller's own window stays complete.
            user_key = self.recent_key(tool_name, user_id)
            pipe.zadd(user_key, {run_id: score})
            pipe.zremrangebyrank(user_key, 0, -(settings.recent_runs_limit + 1))
            pipe.expire(user_key, settings.result_ttl_seconds)
        await pipe.execute()

    async def refresh_liveness(self, r: Any, run_id: str, ttl: int) -> None:
        await r.set(self.liveness_key(run_id), "1", ex=ttl)

    async def mark_terminal_if_running(self, r: Any, run_id: str, fields: dict[str, str], ttl: int) -> bool:
        """Compare-and-set terminal write: write ``fields`` (the new ``status`` +
        ``finished_at`` and any ``result``/``error``) and refresh the record TTL,
        but ONLY while the stored ``status`` is still ``running`` — enforcing the
        one-way ``lost`` invariant atomically (see ``_TERMINAL_CAS_LUA``). Returns
        ``True`` when this call performed the transition, ``False`` when the record
        was no longer ``running`` (another writer reached a terminal state first)."""
        flat: list[Any] = []
        for field, value in fields.items():
            flat.extend((field, value))
        written = await r.eval(_TERMINAL_CAS_LUA, 1, self.run_key(run_id), ttl, *flat)
        return bool(written)

    # -- reads ---------------------------------------------------------------

    async def get_run(self, r: Any, run_id: str) -> dict[str, str] | None:
        record = await r.hgetall(self.run_key(run_id))
        return record or None

    async def get_runs(self, r: Any, run_ids: list[str]) -> list[dict[str, str] | None]:
        """Batch ``HGETALL`` for many run ids in ONE pipeline, aligned to the input
        order; a vanished record maps to ``None`` (no per-id N+1)."""
        if not run_ids:
            return []
        pipe = r.pipeline()
        for run_id in run_ids:
            pipe.hgetall(self.run_key(run_id))
        return [record or None for record in await pipe.execute()]

    async def liveness_present(self, r: Any, run_id: str) -> bool:
        return await r.get(self.liveness_key(run_id)) is not None

    async def liveness_present_many(self, r: Any, run_ids: list[str]) -> list[bool]:
        """Batch liveness-key ``GET`` for many run ids in ONE pipeline, aligned to
        the input order; each entry is ``True`` when that run's liveness key is
        present."""
        if not run_ids:
            return []
        pipe = r.pipeline()
        for run_id in run_ids:
            pipe.get(self.liveness_key(run_id))
        return [value is not None for value in await pipe.execute()]

    async def recent_run_ids(self, r: Any, tool_name: str, limit: int, user_id: str | None = None) -> list[str]:
        # Highest score (most recent start) first. With ``user_id`` given, reads the
        # caller's per-identity index; without it, the shared index.
        return await r.zrevrange(self.recent_key(tool_name, user_id), 0, limit - 1)

    async def prune_recent(self, r: Any, tool_name: str, run_id: str, user_id: str | None = None) -> None:
        # Prune the SAME index the list read: the per-identity index for a restricted
        # caller (``user_id`` given), the shared index otherwise — so an expired entry
        # is never pruned from the wrong index.
        await r.zrem(self.recent_key(tool_name, user_id), run_id)


# -- supervisor --------------------------------------------------------------


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
    global _ACTIVE_RUNS
    _discard_supervisor(task)
    # Release the concurrency slot the submit door reserved for this run. Every
    # spawned supervisor reaches this callback exactly once, so the count returns
    # to the submit door's increment.
    _ACTIVE_RUNS -= 1
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
    settings = tool_runs_settings()
    store = ToolRunStore(settings.key_prefix)
    async with client_ctx(RedisClient, settings.redis) as r:
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
        try:
            try:
                result = await tai42_app.tools.run_tool(tool_name, arguments, offload_sync=True)
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
                    "finished_at": _now().isoformat(),
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
                fields = {"status": _FAILED, "finished_at": _now().isoformat(), "error": str(exc)}
            else:
                fields = {"status": _SUCCEEDED, "finished_at": _now().isoformat(), "result": result_json}
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

    settings = tool_runs_settings()
    store = ToolRunStore(settings.key_prefix)
    # The owning identity is the fire's own bound key — request_identity reads it from the
    # bound execution identity — so the record is attributed and per-identity indexed
    # exactly as a restricted submit's is.
    user_id, _restricted = request_identity()
    # Read the tool's registration meta BY NAME for the generic crash-resume flag
    # (absent → False): the recording caller holds only tool_name/arguments/user_id and
    # cannot address the record, so the flag comes from the registration meta, never a
    # record write. The skeleton just READS a platform-generic meta key and STORES a
    # generic bool + a generic argument blob — it never names the consumer.
    crash_resume = await _tool_declares_crash_resume(tool_name)
    run_id = secrets.token_urlsafe(16)
    started = _now()
    async with client_ctx(RedisClient, settings.redis) as r:
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
    budget = deadline if deadline is not None else tool_runs_settings().shutdown_drain_seconds
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


async def _reconcile_lost_with_liveness(
    r: Any, store: ToolRunStore, run_id: str, record: dict[str, str], liveness_present: bool, ttl: int
) -> dict[str, str]:
    """Persist ``lost`` one way when a still-``running`` ``record`` has lost its
    liveness key (``liveness_present`` is ``False``) — a dead supervisor's
    ``finally`` never wrote a terminal record. The write is a compare-and-set
    gated on ``running`` (``mark_terminal_if_running``): should the supervisor's
    own terminal write land between this reader's GET and the CAS, the CAS is
    rejected and the real terminal record is re-read rather than reporting a stale
    ``lost``. A live run keeps its liveness key, so it is never reconciled.

    Crash-resume seam: a record carrying the generic ``crash_resume`` flag is, at the
    SAME reconcile point, marked ``lost`` (the one-way CAS, so exactly one reader wins
    and only one dispatches) AND re-dispatched as a DETACHED background task replaying
    ``run_recorded`` from scratch under the principal's reconstructed CURRENT-grant
    identity. An un-flagged record keeps today's quiet ``lost`` EXACTLY."""
    if record.get("status") != _RUNNING or liveness_present:
        return record
    finished_at = _now().isoformat()
    if not await store.mark_terminal_if_running(r, run_id, {"status": _LOST, "finished_at": finished_at}, ttl):
        # The supervisor reached a terminal state first — reflect the real record.
        return await store.get_run(r, run_id) or record
    lost_record = {**record, "status": _LOST, "finished_at": finished_at}
    # This reader won the one-way transition. Only now (single winner) may the flagged
    # record be re-dispatched, so a second reader never double-dispatches.
    if record.get("crash_resume") == "1":
        _spawn_crash_resume(run_id, record)
    return lost_record


async def _tool_declares_crash_resume(tool_name: str) -> bool:
    """Whether ``tool_name``'s registration meta opts it into crash-resume (absent →
    ``False``). Reads the generic ``tai42/crash_resume`` meta off the registered tool by
    name; an unregistered name is treated as un-flagged (the run itself fails loudly in
    ``run_tool``)."""
    try:
        tool = await tai42_app.tools.get_tool(tool_name)
    except Exception:
        return False
    return bool((tool.meta or {}).get(_CRASH_RESUME_META_KEY))


def _spawn_crash_resume(run_id: str, record: dict[str, str]) -> None:
    """Dispatch a crash-resume re-drive of ``record`` as a DETACHED background task.

    Never awaited inline: the reconciler runs on READ paths (get-by-id, list) and an
    inline re-drive would block the reader for the run's whole wall-clock. The re-invoke
    is logged loudly by run id; a re-invoke that itself raises is surfaced loudly by the
    task's done-callback, never silently swallowed."""
    task = asyncio.create_task(
        _crash_resume(run_id, record),
        name=f"tai-crash-resume-{run_id}",
    )
    _enroll_supervisor(task)
    task.add_done_callback(lambda t: _on_crash_resume_done(t, run_id, record["tool_name"]))


def _on_crash_resume_done(task: asyncio.Task[None], run_id: str, tool_name: str) -> None:
    """Drop the drain registry reference and surface a crash-resume failure LOUDLY — a
    re-invoke that itself raises is logged at ERROR, never silently swallowed."""
    _discard_supervisor(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("crash-resume: re-dispatched run %s (%s) failed", run_id, tool_name, exc_info=exc)


async def _crash_resume(run_id: str, record: dict[str, str]) -> None:
    """Replay ``record``'s run FROM SCRATCH under the principal's reconstructed identity.

    Binds the execution identity rebuilt from the record's ``user_id`` (its CURRENT live
    grants, so a mid-life de-scope/revocation lands on the re-drive), then replays
    ``run_recorded(tool_name, persisted arguments)``. When the principal's live grants no
    longer carry authority the reconstruction binds ``None`` (identity-less) — the
    re-drive then fail-closes loudly on any credential seam, never a silent principal
    substitution under a revoked key."""
    from tai42_skeleton.authz.execution_identity import reset_execution_identity, set_execution_identity

    tool_name = record["tool_name"]
    try:
        arguments = json.loads(record.get("arguments") or "{}")
    except json.JSONDecodeError:
        logger.error("crash-resume: run %s (%s) has an unreadable arguments blob; skipping re-drive", run_id, tool_name)
        return
    user_id = record.get("user_id")
    identity = await _rebuild_crash_resume_identity(user_id) if user_id is not None else None
    logger.info("crash-resume: re-dispatching lost run %s (%s) from scratch", run_id, tool_name)
    token = set_execution_identity(identity)
    try:
        await run_recorded(tool_name, arguments)
    finally:
        reset_execution_identity(token)


async def _rebuild_crash_resume_identity(execution_key: str) -> CallerIdentity | None:
    """Rebuild the synthetic execution identity for a crash-resume re-drive from
    ``execution_key``'s CURRENT live grants, or ``None`` when they no longer carry
    authority.

    The record persists only the ``user_id`` string (never the mint fingerprint), so the
    reconstruction reads the key's live fingerprint and builds the identity from the
    current grants — a mid-life de-scope/revocation therefore lands on the re-drive. A key
    with no live policy / disabled / grantless yields ``None`` so the re-drive fail-closes
    loudly rather than substituting a different principal."""
    from tai42_contract.access_control import KEY_FINGERPRINT_CLAIM

    from tai42_skeleton.access_control.policy import PolicyEnforcer
    from tai42_skeleton.access_control.settings import access_control_settings
    from tai42_skeleton.authz.execution import build_execution_identity
    from tai42_skeleton.operations.errors import PermissionDenied

    settings = access_control_settings()
    if not settings.enable:
        # Gate off: every principal is the synthetic admin; the identity carries the key
        # alone (no fingerprint needed, matching ``build_execution_identity``'s gate-off).
        return await build_execution_identity(execution_key, bound_fingerprint="")
    enforcer = PolicyEnforcer(settings)
    version = await enforcer.current_policy_version()
    policy = await enforcer.get_policy_at(execution_key, version)
    fingerprint = policy.policy_data.get(KEY_FINGERPRINT_CLAIM)
    if not isinstance(fingerprint, str) or not fingerprint:
        return None
    try:
        return await build_execution_identity(execution_key, bound_fingerprint=fingerprint)
    except PermissionDenied:
        return None


async def _reconcile_lost(r: Any, store: ToolRunStore, run_id: str, record: dict[str, str], ttl: int) -> dict[str, str]:
    """Single-record ``lost`` reconciliation for the GET-by-id door: read the run's
    liveness only while it is still ``running`` (a terminal record is never
    reconciled), then apply ``_reconcile_lost_with_liveness``."""
    liveness_present = record.get("status") == _RUNNING and await store.liveness_present(r, run_id)
    return await _reconcile_lost_with_liveness(r, store, run_id, record, liveness_present, ttl)


# -- response views ----------------------------------------------------------


def _run_view(run_id: str, record: dict[str, str]) -> dict[str, Any]:
    """The full GET view; ``result`` is parsed back from its stored JSON."""
    view: dict[str, Any] = {
        "run_id": run_id,
        "tool_name": record["tool_name"],
        "status": record["status"],
        "started_at": record["started_at"],
    }
    if "finished_at" in record:
        view["finished_at"] = record["finished_at"]
    if "result" in record:
        view["result"] = json.loads(record["result"])
    if "error" in record:
        view["error"] = record["error"]
    return view


def _list_view(run_id: str, record: dict[str, str]) -> dict[str, Any]:
    """The list view — id/tool name/status/timestamps only, never ``result``/``error``."""
    view: dict[str, Any] = {
        "run_id": run_id,
        "tool_name": record["tool_name"],
        "status": record["status"],
        "started_at": record["started_at"],
    }
    if "finished_at" in record:
        view["finished_at"] = record["finished_at"]
    return view


# -- operations --------------------------------------------------------------


@operation(
    name="submit_run",
    summary="Submit a tool for background execution",
    tags=["tool-runs"],
    destructive=True,
    reload_gated=True,
    meta_executor=True,
    errors=[BadRequestError, NotFoundError, NotSupportedError, UnavailableError],
    request_model=ToolRunSubmission,
)
async def submit_run(tool_name: str, arguments: dict[str, object]) -> dict:
    """Submit a tool for background execution — returns ``202 {run_id}`` at once
    and runs the tool through the same seam the sync door uses.

    The submitted tool is authorized against the caller with the full tool-edge decision
    before anything is recorded, so a fenced/secret target is admin-only here exactly as
    at the sync door and the MCP edge."""
    # OFF gate — before ANY side effect (the concurrency slot, the authorize
    # decision, the registry read): with no store configured the surface is cleanly
    # OFF and refuses with a named, machine-readable reason rather than reaching for
    # an absent Redis.
    if not tool_runs_store_configured():
        raise NotSupportedError(_NOT_CONFIGURED_MESSAGE, extra={"code": _NOT_CONFIGURED_CODE})

    settings = tool_runs_settings()
    store = ToolRunStore(settings.key_prefix)

    # Resolve the name against the live registry BEFORE creating a record: an
    # unknown tool is a loud 404 up front, so a typo'd name never earns a
    # ``running`` record that the supervisor would only later fail — keeping a
    # real runtime failure distinguishable from a bad request and the store clean.
    tools = await tai42_app.tools.get_tools()
    if tool_name not in tools:
        raise NotFoundError(f"unknown tool: {tool_name}")

    # The run detaches from this request, so this is the ONLY edge the inner tool reaches:
    # decide it here, before a slot is reserved.
    await authorize_submitted_tool(tool_name, arguments)

    # Per-worker concurrency cap: check + increment are synchronous (no await
    # between them) so two concurrent submits cannot both pass the check before
    # either reserves its slot. The slot is released by the supervisor's
    # done-callback, or by the ``except`` below if the record write fails.
    global _ACTIVE_RUNS
    if settings.max_concurrent_runs <= _ACTIVE_RUNS:
        raise UnavailableError(
            f"tool-run capacity reached ({settings.max_concurrent_runs} concurrent runs); "
            "retry later or raise TAI_TOOL_RUNS_MAX_CONCURRENT_RUNS"
        )
    _ACTIVE_RUNS += 1

    # The owning identity of this run is always the caller's own id — each key is its
    # own island. Stamped on the record and used to build the per-identity index. An
    # unauthenticated caller leaves it None, so only the shared index is written.
    user_id, _restricted = request_identity()
    owning_identity = user_id

    run_id = secrets.token_urlsafe(16)
    started = _now()
    try:
        async with client_ctx(RedisClient, settings.redis) as r:
            await store.create_run(
                r, run_id, tool_name, started.isoformat(), started.timestamp(), settings, user_id=owning_identity
            )
        _spawn_supervisor(run_id, tool_name, arguments)
    except Exception:
        # The record never became a live run (no supervisor owns the slot), so
        # return the reserved slot here and re-raise loudly.
        _ACTIVE_RUNS -= 1
        raise
    return {"run_id": run_id}


@operation(
    name="get_run",
    summary="Get a background tool run's status and result",
    tags=["tool-runs"],
    errors=[ForbiddenError, NotFoundError],
)
async def get_run(run_id: str) -> dict:
    # OFF gate: with no store, no run can exist — a 404 byte-identical to the
    # genuine miss below, so the door is no oracle for the store's absence.
    if not tool_runs_store_configured():
        raise NotFoundError(f"run {run_id!r} not found")
    settings = tool_runs_settings()
    store = ToolRunStore(settings.key_prefix)
    _user_id, restricted = request_identity()
    async with client_ctx(RedisClient, settings.redis) as r:
        record = await store.get_run(r, run_id)
        if record is None:
            raise NotFoundError(f"run {run_id!r} not found")
        # A restricted caller may read only a run owned by its OWN identity. A mismatch
        # (including a run whose ``user_id`` is absent — owned by no identity) is a
        # loud ``403`` naming the denial, NEVER a ``404`` — the run exists under an id
        # namespace of unguessable tokens, so an honest denial leaks nothing
        # actionable while a ``404`` would lie about existence.
        if restricted is not None and record.get("user_id") != restricted:
            raise ForbiddenError("run belongs to another identity")
        record = await _reconcile_lost(r, store, run_id, record, settings.result_ttl_seconds)
    return _run_view(run_id, record)


@operation(
    name="list_tool_runs",
    summary="List background tool runs for a tool",
    tags=["tool-runs"],
    errors=[BadRequestError],
    request_model=ToolRunsListQuery,
)
async def list_tool_runs(tool_name: str) -> list[dict]:
    """List the recent runs for one tool, newest first.

    A restricted caller reads its OWN per-identity index (complete within its own
    bound — never truncated by other identities' volume that saturates the shared
    window); an unrestricted caller reads the shared index unchanged. An empty list
    is the honest answer to "my runs of this tool" — this filters a collection to the
    caller's own slice (distinct from GET-by-id, which raises ``403`` for a NAMED run
    owned by another identity)."""
    # OFF gate: with no store, the honest answer to "my runs of this tool" is the
    # empty collection — no store touched.
    if not tool_runs_store_configured():
        return []
    settings = tool_runs_settings()
    store = ToolRunStore(settings.key_prefix)
    _user_id, restricted = request_identity()
    entries: list[dict[str, Any]] = []
    async with client_ctx(RedisClient, settings.redis) as r:
        run_ids = await store.recent_run_ids(r, tool_name, settings.recent_runs_limit, user_id=restricted)
        # One pipeline for every record hash — no per-id N+1 of HGETALLs.
        records = await store.get_runs(r, run_ids)
        present: list[tuple[str, dict[str, str]]] = []
        for run_id, record in zip(run_ids, records, strict=True):
            if record is None:
                # The record hash expired out from under the index — prune the
                # phantom from the SAME index that was read (per-identity for a
                # restricted caller, shared otherwise) so the list doesn't carry a
                # vanished run.
                await store.prune_recent(r, tool_name, run_id, user_id=restricted)
                continue
            present.append((run_id, record))
        # One pipeline for the liveness keys of only the still-``running`` subset;
        # a terminal record is never reconciled and needs no liveness read.
        running_ids = [run_id for run_id, record in present if record.get("status") == _RUNNING]
        liveness = dict(zip(running_ids, await store.liveness_present_many(r, running_ids), strict=True))
        for run_id, record in present:
            record = await _reconcile_lost_with_liveness(
                r, store, run_id, record, liveness.get(run_id, True), settings.result_ttl_seconds
            )
            entries.append(_list_view(run_id, record))
    return entries
