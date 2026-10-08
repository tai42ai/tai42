"""Lost-run reconciliation and the crash-resume re-drive for tool runs.

``lost`` is computed-and-persisted one way: the FIRST read of a record still
``running`` whose liveness key has expired writes ``status: lost``. A record that
declared the generic crash-resume flag is, at that same reconcile point, marked
``lost`` (single winner) AND re-dispatched from scratch under the principal's
reconstructed current-grant identity.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from tai42_contract.access_control import reset_request_secret_capability, set_request_secret_capability
from tai42_contract.app import tai42_app
from tai42_contract.states import StateContext

import tai42_skeleton.operations.tool_runs as _pkg
from tai42_skeleton.states.context import state_context
from tai42_skeleton.tools.binding.errors import UnknownToolError

from . import supervisor
from .models import _CRASH_RESUME_META_KEY, _LOST, _RUNNING
from .store import ToolRunStore

if TYPE_CHECKING:
    from collections.abc import Mapping

    from tai42_skeleton.authz.identity import CallerIdentity
    from tai42_skeleton.routers.tool_runs_settings import ToolRunsSettings

logger = logging.getLogger(__name__)

# The package this submodule belongs to; ``_now`` and ``_spawn_crash_resume`` are read
# THROUGH it at call time so a package-alias ``setattr`` on this generation takes effect.


async def _reconcile_lost_with_liveness(
    r: Any, store: ToolRunStore, run_id: str, record: dict[str, str], liveness_present: bool, ttl: int
) -> dict[str, str]:
    """Persist ``lost`` one way when a still-``running`` record has lost its liveness key.

    ``liveness_present`` is ``False`` when a dead supervisor's ``finally`` never
    wrote a terminal record. The write is a compare-and-set gated on ``running``
    (``mark_terminal_if_running``): should the supervisor's
    own terminal write land between this reader's GET and the CAS, the CAS is
    rejected and the real terminal record is re-read rather than reporting a stale
    ``lost``. A live run keeps its liveness key, so it is never reconciled.

    Crash-resume seam: a record carrying the generic ``crash_resume`` flag is, at the
    SAME reconcile point, marked ``lost`` (the one-way CAS, so exactly one reader wins
    and only one dispatches) AND re-dispatched as a DETACHED background task replaying
    ``run_recorded`` from scratch under the principal's reconstructed CURRENT-grant
    identity. An un-flagged record keeps today's quiet ``lost`` EXACTLY.
    """
    if record.get("status") != _RUNNING or liveness_present:
        return record
    finished_at = _pkg._now().isoformat()
    if not await store.mark_terminal_if_running(r, run_id, {"status": _LOST, "finished_at": finished_at}, ttl):
        # The supervisor reached a terminal state first — reflect the real record.
        return await store.get_run(r, run_id) or record
    lost_record = {**record, "status": _LOST, "finished_at": finished_at}
    # This reader won the one-way transition. Only now (single winner) may the flagged
    # record be re-dispatched, so a second reader never double-dispatches.
    if record.get("crash_resume") == "1":
        _pkg._spawn_crash_resume(run_id, record)
    return lost_record


async def create_recorded_run(
    r: Any,
    store: ToolRunStore,
    tool_name: str,
    settings: ToolRunsSettings,
    *,
    user_id: str | None,
    arguments: dict[str, Any],
    extras: Mapping[str, Any] | None,
    state_context: StateContext | None,
) -> str:
    """Persist a new ``running`` record through the ONE create seam both detached-record doors share.

    The background-submit door and the inline ``run_recorded`` (hook/trigger) door each create a
    DETACHED tool-run record — one with no live caller to retry it on a crash — so a crash-resume
    re-drive reads that record later. BOTH doors store the crash-resume inputs here, in one place,
    with no second copy of the field list: the ``crash_resume`` flag read from the tool's
    registration meta, and the ``arguments``/``extras``/``state_context`` the re-drive replays from.
    An un-flagged tool stores only the base record — the store gates every crash-resume field on the
    flag.

    ``state_context`` is the subject context the re-drive deposits around the replay so its park
    indexes where the original's would have. Each door resolves its own source and passes it here:
    the hook door its ambient fire context, the submit door the ``door="api"`` context built from
    the submitted ``StateSubject`` (the same context the supervisor deposits around the live run).
    Returns the generated ``run_id``.
    """
    crash_resume = await _tool_declares_crash_resume(tool_name)
    run_id = secrets.token_urlsafe(16)
    started = _pkg._now()
    await store.create_run(
        r,
        run_id,
        tool_name,
        started.isoformat(),
        started.timestamp(),
        settings,
        user_id=user_id,
        arguments=arguments,
        extras=extras,
        state_context=state_context,
        crash_resume=crash_resume,
    )
    return run_id


async def _tool_declares_crash_resume(tool_name: str) -> bool:
    """Whether ``tool_name``'s registration meta opts it into crash-resume (absent → ``False``).

    Reads the generic ``tai42/crash_resume`` meta off the registered tool by name;
    an unregistered name is treated as un-flagged (the run itself fails loudly in
    ``run_tool``).
    """
    try:
        tool = await tai42_app.tools.get_tool(tool_name)
    except UnknownToolError:
        return False
    return bool((tool.meta or {}).get(_CRASH_RESUME_META_KEY))


def _spawn_crash_resume(run_id: str, record: dict[str, str]) -> None:
    """Dispatch a crash-resume re-drive of ``record`` as a DETACHED background task.

    Never awaited inline: the reconciler runs on READ paths (get-by-id, list) and an
    inline re-drive would block the reader for the run's whole wall-clock. The re-invoke
    is logged loudly by run id; a re-invoke that itself raises is surfaced loudly by the
    task's done-callback, never silently swallowed.
    """
    task = asyncio.create_task(
        _crash_resume(run_id, record),
        name=f"tai-crash-resume-{run_id}",
    )
    supervisor._enroll_supervisor(task)
    task.add_done_callback(lambda t: _on_crash_resume_done(t, run_id, record["tool_name"]))


def _on_crash_resume_done(task: asyncio.Task[None], run_id: str, tool_name: str) -> None:
    """Drop the drain registry reference and surface a crash-resume failure loudly.

    A re-invoke that itself raises is logged at ERROR, never silently swallowed.
    """
    supervisor._discard_supervisor(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("crash-resume: re-dispatched run %s (%s) failed", run_id, tool_name, exc_info=exc)


async def _crash_resume(run_id: str, record: dict[str, str]) -> None:
    """Replay ``record``'s run FROM SCRATCH under the principal's reconstructed identity and subject.

    Binds the execution identity rebuilt from the record's ``user_id`` (its CURRENT live
    grants, so a mid-life de-scope/revocation lands on the re-drive) AND deposits the
    fire's persisted ``state_context`` around the replay, so the re-driven run runs under
    the SAME subject the original fire did: its park indexes where the original's would
    have and a caller ask can still be raised. It then replays
    ``run_recorded(tool_name, persisted arguments, extras=persisted extras)``. A record with no
    stored context (a fire without a subject) deposits none. When the principal's live grants no
    longer carry authority the reconstruction binds ``None`` (identity-less) — the
    re-drive then fail-closes loudly on any credential seam, never a silent principal
    substitution under a revoked key. An unreadable arguments/extras/context blob is logged
    and the re-drive skipped, the same loud path the persisted input takes.
    """
    from tai42_skeleton.authz.execution_identity import reset_execution_identity, set_execution_identity

    tool_name = record["tool_name"]
    try:
        arguments = json.loads(record.get("arguments") or "{}")
        extras = json.loads(record.get("extras") or "{}")
        raw_context = record.get("state_context")
        context = StateContext.model_validate(json.loads(raw_context)) if raw_context is not None else None
    except (json.JSONDecodeError, ValidationError):
        logger.exception(
            "crash-resume: run %s (%s) has an unreadable arguments/extras/context blob; skipping re-drive",
            run_id,
            tool_name,
        )
        return
    user_id = record.get("user_id")
    identity = await _rebuild_crash_resume_identity(user_id) if user_id is not None else None
    logger.info("crash-resume: re-dispatching lost run %s (%s) from scratch", run_id, tool_name)
    context_scope = state_context(context) if context is not None else nullcontext()
    token = set_execution_identity(identity)
    # Bind the secret-read capability of the ORIGINAL run key, NEVER the reader's: the reconcile
    # runs on a read path under the reader's ambient context, so an unbound capability would leave
    # the re-drive acting on the reader's ``action=secret`` fence. It is the admin status of the
    # rebuilt identity (its standing's admin verdict, carried on ``is_admin``), and
    # fail-closed ``False`` when the key no longer carries authority — mirroring
    # ``bind_execution_identity``.
    secret_token = set_request_secret_capability(identity.is_admin if identity is not None else False)
    try:
        with context_scope:
            await supervisor.run_recorded(tool_name, arguments, extras=extras)
    finally:
        reset_request_secret_capability(secret_token)
        reset_execution_identity(token)


async def _rebuild_crash_resume_identity(execution_key: str) -> CallerIdentity | None:
    """Rebuild the synthetic execution identity for a crash-resume re-drive from ``execution_key``'s live grants.

    Returns ``None`` when they no longer carry authority. The record persists
    only the ``user_id`` string (never the mint fingerprint), so the
    reconstruction reads the key's live fingerprint and builds the identity from the
    current grants — a mid-life de-scope/revocation therefore lands on the re-drive. A key
    with no live policy / disabled / grantless yields ``None`` so the re-drive fail-closes
    loudly rather than substituting a different principal. Delegates to the shared
    :func:`~tai42_skeleton.authz.execution.rebuild_execution_identity` (function-local
    import keeps the operations→authz edge lazy, matching the guarded authz-edge
    idiom).
    """
    from tai42_skeleton.authz.execution import rebuild_execution_identity

    return await rebuild_execution_identity(execution_key)


async def _reconcile_lost(r: Any, store: ToolRunStore, run_id: str, record: dict[str, str], ttl: int) -> dict[str, str]:
    """Single-record ``lost`` reconciliation for the GET-by-id door.

    Reads the run's liveness only while it is still ``running`` (a terminal record
    is never reconciled), then applies ``_reconcile_lost_with_liveness``.
    """
    liveness_present = record.get("status") == _RUNNING and await store.liveness_present(r, run_id)
    return await _reconcile_lost_with_liveness(r, store, run_id, record, liveness_present, ttl)
