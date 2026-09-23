"""The three tool-run operation doors: submit a run, get a run by id, list a tool's runs."""

from __future__ import annotations

import secrets
from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.states import StateSubject
from tai42_kit.clients.impl.redis import RedisClient

import tai42_skeleton.operations.tool_runs as _pkg
from tai42_skeleton.operations import (
    BadRequestError,
    ForbiddenError,
    NotFoundError,
    NotSupportedError,
    UnavailableError,
    operation,
)
from tai42_skeleton.operations.response_models_group_b import RunSubmitted, ToolRunListResponse, ToolRunView
from tai42_skeleton.routers.tool_runs_settings import tool_runs_store_configured

from . import reconcile, supervisor, views
from .models import _NOT_CONFIGURED_CODE, _NOT_CONFIGURED_MESSAGE, _RUNNING, ToolRunsListQuery, ToolRunSubmission
from .store import ToolRunStore

# The package this door submodule belongs to; the package-alias seam symbols
# (``client_ctx``, ``tool_runs_settings``, ``authorize_submitted_tool``,
# ``request_identity``, ``_now``) are read THROUGH it at call time so a test's
# ``setattr(operations.tool_runs, …)`` on this generation takes effect.


@operation(
    name="submit_run",
    summary="Submit a tool for background execution",
    tags=["tool-runs"],
    destructive=True,
    reload_gated=True,
    meta_executor=True,
    errors=[BadRequestError, NotFoundError, NotSupportedError, UnavailableError],
    request_model=ToolRunSubmission,
    response_model=RunSubmitted,
)
async def submit_run(tool_name: str, arguments: dict[str, object], subject: StateSubject | None = None) -> dict:
    """Submit a tool for background execution — returns ``202 {run_id}`` at once.

    Runs the tool through the same seam the sync door uses.

    The submitted tool is authorized against the caller with the full tool-edge decision
    before anything is recorded, so a fenced/secret target is admin-only here exactly as
    at the sync door and the MCP edge.

    ``subject`` names the addressed subject the detached run's async parks index under: it is
    handed to the spawned supervisor, which deposits it as the run's ``door="api"``
    ``StateContext`` so a park is findable and resumable by a later run on the same subject.
    """
    # OFF gate — before ANY side effect (the concurrency slot, the authorize
    # decision, the registry read): with no store configured the surface is cleanly
    # OFF and refuses with a named, machine-readable reason rather than reaching for
    # an absent Redis.
    if not tool_runs_store_configured():
        raise NotSupportedError(_NOT_CONFIGURED_MESSAGE, extra={"code": _NOT_CONFIGURED_CODE})

    settings = _pkg.tool_runs_settings()
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
    await _pkg.authorize_submitted_tool(tool_name, arguments)

    # Per-worker concurrency cap: check + increment are synchronous (no await
    # between them) so two concurrent submits cannot both pass the check before
    # either reserves its slot. The slot is released by the supervisor's
    # done-callback, or by the ``except`` below if the record write fails.
    if not supervisor.reserve_active_slot(settings.max_concurrent_runs):
        raise UnavailableError(
            f"tool-run capacity reached ({settings.max_concurrent_runs} concurrent runs); "
            "retry later or raise TAI_TOOL_RUNS_MAX_CONCURRENT_RUNS"
        )

    # The owning identity of this run is always the caller's own id — each key is its
    # own island. Stamped on the record and used to build the per-identity index. An
    # unauthenticated caller leaves it None, so only the shared index is written.
    user_id, _restricted = _pkg.request_identity()
    owning_identity = user_id

    # An HTTP submit carries no execution identity, so an async-parking tool in the
    # detached run could never rebind its continuation (a fire's submit inherits the
    # fire's binding and is left untouched). Bind the caller's OWN key — the same
    # rebuild the crash-resume re-drive uses, live grants and all — around the spawn
    # so the copied context carries it; a key whose grants carry no authority binds
    # nothing and the run has no subject context. The scope resets after the spawn,
    # by which point the supervisor already copied the identity into its own context.
    from tai42_contract.monitoring import RunAttribution

    from tai42_skeleton.states.api_context import caller_execution_identity
    from tai42_skeleton.tools.attribution import reset_run_attribution, set_run_attribution

    async with caller_execution_identity(owning_identity):
        # Deposit the caller's identity as this run's attribution around the spawn so the
        # supervisor's copied context carries it: a runs-index row the detached dispatch
        # registers (a preset target) is then born with a ``user_id`` rather than NULL. Reset
        # after the spawn — the supervisor already copied it — exactly like the bind above.
        attribution_token = (
            set_run_attribution(RunAttribution(user_id=owning_identity)) if owning_identity is not None else None
        )

        run_id = secrets.token_urlsafe(16)
        started = _pkg._now()
        try:
            async with _pkg.client_ctx(RedisClient, settings.redis) as r:
                await store.create_run(
                    r, run_id, tool_name, started.isoformat(), started.timestamp(), settings, user_id=owning_identity
                )
            supervisor._spawn_supervisor(run_id, tool_name, arguments, subject)
        except Exception:
            # The record never became a live run (no supervisor owns the slot), so
            # return the reserved slot here and re-raise loudly.
            supervisor.release_active_slot()
            raise
        finally:
            # Release the submit-scope attribution: the spawned supervisor already copied it
            # into its own context, and this request must not stay stamped past the submit.
            if attribution_token is not None:
                reset_run_attribution(attribution_token)
    return {"run_id": run_id}


@operation(
    name="get_run",
    summary="Get a background tool run's status and result",
    tags=["tool-runs"],
    errors=[ForbiddenError, NotFoundError],
    response_model=ToolRunView,
)
async def get_run(run_id: str) -> dict:
    """Get a background tool run by ``run_id``; a restricted caller may read only its own runs."""
    # OFF gate: with no store, no run can exist — a 404 byte-identical to the
    # genuine miss below, so the door is no oracle for the store's absence.
    if not tool_runs_store_configured():
        raise NotFoundError(f"run {run_id!r} not found")
    settings = _pkg.tool_runs_settings()
    store = ToolRunStore(settings.key_prefix)
    _user_id, restricted = _pkg.request_identity()
    async with _pkg.client_ctx(RedisClient, settings.redis) as r:
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
        record = await reconcile._reconcile_lost(r, store, run_id, record, settings.result_ttl_seconds)
    return views._run_view(run_id, record)


@operation(
    name="list_tool_runs",
    summary="List background tool runs for a tool",
    tags=["tool-runs"],
    errors=[BadRequestError],
    request_model=ToolRunsListQuery,
    response_model=ToolRunListResponse,
)
async def list_tool_runs(tool_name: str) -> list[dict]:
    """List the recent runs for one tool, newest first.

    A restricted caller reads its OWN per-identity index (complete within its own
    bound — never truncated by other identities' volume that saturates the shared
    window); an unrestricted caller reads the shared index unchanged. An empty list
    is the honest answer to "my runs of this tool" — this filters a collection to the
    caller's own slice (distinct from GET-by-id, which raises ``403`` for a NAMED run
    owned by another identity).
    """
    # OFF gate: with no store, the honest answer to "my runs of this tool" is the
    # empty collection — no store touched.
    if not tool_runs_store_configured():
        return []
    settings = _pkg.tool_runs_settings()
    store = ToolRunStore(settings.key_prefix)
    _user_id, restricted = _pkg.request_identity()
    entries: list[dict[str, Any]] = []
    async with _pkg.client_ctx(RedisClient, settings.redis) as r:
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
            record = await reconcile._reconcile_lost_with_liveness(
                r, store, run_id, record, liveness.get(run_id, True), settings.result_ttl_seconds
            )
            entries.append(views._list_view(run_id, record))
    return entries
