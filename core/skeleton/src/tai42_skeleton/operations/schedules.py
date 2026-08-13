"""Scheduling operations — a thin skin over the run-tool seam, reporting honestly when
no scheduling backend is installed.

Availability is detected at CALL time, never probed at import: list/create/delete
pre-check that an installed backend registers the marker tools (``_MARKER_TOOLS``) and
raise :class:`NotSupportedError` (501) when it does not. ``server_datetime`` has no
pre-check — it dispatches ``current_time_info`` and learns of its absence from the
dispatch itself, so its 501 is independent of the scheduling backend. An unknown
caller-named tool on create is :class:`NotFoundError` (404) instead.

Every door dispatches a NAMED inner tool and wraps that dispatch identically:

* an :class:`~tai42_skeleton.tools.binding.UnknownToolError` naming the tool the door
  itself asked for is that tool's absence — the door's own verdict (501 for
  list/delete/server-datetime, 404 for create);
* an ``UnknownToolError`` naming a DIFFERENT tool escaped the running tool's own body —
  a structured :class:`OperationFailed` (500);
* a typed :class:`OperationError` (most sharply ``PermissionDenied``) passes through as
  the answer it already is;
* any other exception becomes a structured ``OperationFailed`` (500), never an opaque
  "Internal Server Error".

Only list's and delete's absent-marker-tool branch logs (``logger.warning``): the
marker passed the presence pre-check moments earlier, so failing to resolve at dispatch
is an ANOMALY worth a trace, and the caller sees only a plain 501. server-datetime's 501
and create's 404 stay silent — an uninstalled toolbox extra and an unregistered
caller-named tool are both steady-state/ordinary, and logging either would repeat every
request. Both 500 branches always ``logger.exception``.

``UnavailableError`` (503) on every door: the tool-dispatch seam — and for create,
``authorize_submitted_tool`` — refuses mid-rebuild with the retriable
``OperationSurfaceUnsettledError``.

These doors are authed but NOT admin-fenced: an UNTYPED failure's exception text never
reaches the caller (it can carry internal detail, e.g. a dialled host:port) and stays
server-side in the log — the caller gets only the exception CLASS. A typed
``OperationError`` is the deliberate exception: the tool raised it AS the client-facing
answer, so its message passes through untouched. The inner tool NAME on a mismatched
``UnknownToolError`` IS reported — it is always a registry identifier (never free text
from the caught exception, never third-party data), so naming it is a bounded, accepted
disclosure. The admin-fenced ``run_tool`` door keeps the full error message; these
doors do not.

The resolved dispatch target's authorization is decided at schedule CREATION, run
through the full tool-edge decision against the live submitter (a fenced/secret target
is admin-only) — the later recurring firing has no live caller and runs
anonymous/system, so creation is the only edge the inner tool reaches.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, Field
from tai42_contract.app import tai42_app
from tai42_kit.utils.data import text_to_md5

from tai42_skeleton.operations import (
    BadRequestError,
    NotFoundError,
    NotSupportedError,
    OperationError,
    OperationFailed,
    PermissionDenied,
    UnavailableError,
    operation,
)
from tai42_skeleton.operations._submitted_tool_authz import authorize_submitted_tool
from tai42_skeleton.tools.binding import UnknownToolError

logger = logging.getLogger(__name__)

# The tools an installed scheduling backend registers; their presence is the marker
# that scheduling is available.
_LIST_TOOL = "backend_list_schedules"
_DELETE_TOOL = "backend_delete_schedule"
_MARKER_TOOLS = (_LIST_TOOL, _DELETE_TOOL)
_NO_BACKEND_MESSAGE = "no installed backend exposes scheduling tools"
_TIME_TOOL = "current_time_info"

# A schedule fires the branch tool ``<tool_name>_schedule_task`` (the backend's
# ``schedule_task`` extension), reading its cadence from two EXPERT keys. The friendly
# create form gives the cadence instead as ``cron`` (a crontab STRING) or the structured
# crontab fields, which the door translates onto ``backend_schedule``.
_SCHEDULE_BRANCH_SUFFIX = "_schedule_task"
_SCHEDULE_EXTENSION = "schedule_task"
_EXPERT_SCHEDULE_KEY = "backend_schedule"
_SCHEDULE_NAME_KEY = "backend_schedule_name"
_CRON_KEY = "cron"
# The crontab fields ``normalize_schedule``'s crontab branch reads; a friendly create
# maps whichever are given onto ``{"type": "crontab", ...}``.
_CRONTAB_FIELD_KEYS = frozenset({"minute", "hour", "day_of_month", "month_of_year", "day_of_week"})


def _derive_schedule_name(tool_name: str, tool_kwargs: dict[str, Any], backend_schedule: Any) -> str:
    """A deterministic name when the caller gives none: the tool plus a fingerprint of the
    full schedule spec (tool, its arguments, the cadence). An identical re-add resolves to
    the same name and updates that schedule in place; any change of arguments or cadence
    yields a distinct name, so repeated adds never clobber a different schedule."""
    spec = json.dumps(
        {"tool_name": tool_name, "tool_kwargs": tool_kwargs, "backend_schedule": backend_schedule},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f"{tool_name}_{text_to_md5(spec)[:12]}"


async def _resolve_schedule_dispatch(
    tool_name: str, tool_kwargs: dict[str, Any], schedule_kwargs: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Resolve the (tool name, arguments) the create door dispatches.

    Expert shape — the caller named a schedule branch or passed ``backend_schedule`` —
    dispatches the named tool with schedule keys merged over the tool's own (schedule keys
    win on collision), unchanged. The friendly shape translates a ``cron`` crontab string
    or the structured crontab fields onto the ``<tool_name>_schedule_task`` branch's
    ``backend_schedule``; an empty ``schedule_kwargs`` dispatches the named tool once. Any
    other key on a base tool is a malformed request, and cadence keys never reach the base
    tool's arguments (the silent run-once path)."""
    if tool_name.endswith(_SCHEDULE_BRANCH_SUFFIX) or _EXPERT_SCHEDULE_KEY in schedule_kwargs:
        return tool_name, {**tool_kwargs, **schedule_kwargs}

    keys = set(schedule_kwargs)
    stray = sorted(keys - ({_CRON_KEY, _SCHEDULE_NAME_KEY} | _CRONTAB_FIELD_KEYS))
    if stray:
        raise BadRequestError(
            f"unrecognized schedule parameter(s) {stray} for base tool {tool_name!r}: a cadence is given as "
            f"{_CRON_KEY!r} (a crontab string) or the crontab fields {sorted(_CRONTAB_FIELD_KEYS)}, "
            f"optionally with an explicit {_SCHEDULE_NAME_KEY!r}"
        )

    cron_given = _CRON_KEY in keys
    struct_given = bool(_CRONTAB_FIELD_KEYS & keys)
    if not cron_given and not struct_given:
        if _SCHEDULE_NAME_KEY in keys:
            raise BadRequestError(
                f"{_SCHEDULE_NAME_KEY!r} given without a cadence: add {_CRON_KEY!r} or the crontab fields"
            )
        return tool_name, {**tool_kwargs}
    if cron_given and struct_given:
        raise BadRequestError(
            f"ambiguous cadence: give either {_CRON_KEY!r} or the crontab fields "
            f"{sorted(_CRONTAB_FIELD_KEYS)}, not both"
        )

    branch_name = f"{tool_name}{_SCHEDULE_BRANCH_SUFFIX}"
    tools = await tai42_app.tools.get_tools()
    if branch_name not in tools:
        raise NotFoundError(
            f"tool {tool_name!r} cannot be scheduled on a cadence: its {branch_name!r} vehicle is not registered — "
            f"the tool must carry the backend's {_SCHEDULE_EXTENSION!r} extension"
        )

    if cron_given:
        cron_value = schedule_kwargs[_CRON_KEY]
        if not isinstance(cron_value, str):
            raise BadRequestError(f"{_CRON_KEY!r} must be a crontab string, not {type(cron_value).__name__}")
        backend_schedule: Any = cron_value
    else:
        backend_schedule = {
            "type": "crontab",
            **{field: schedule_kwargs[field] for field in _CRONTAB_FIELD_KEYS & keys},
        }
    schedule_name = schedule_kwargs.get(_SCHEDULE_NAME_KEY) or _derive_schedule_name(
        tool_name, tool_kwargs, backend_schedule
    )
    # Schedule keys win on collision so the backend's scheduling parameters cannot be
    # shadowed by the tool's own arguments.
    return branch_name, {**tool_kwargs, _SCHEDULE_NAME_KEY: schedule_name, _EXPERT_SCHEDULE_KEY: backend_schedule}


class ScheduleCreate(BaseModel):
    """Create a schedule that periodically runs ``tool_name`` with ``tool_kwargs``
    on the cadence in ``schedule_kwargs``."""

    tool_name: str = Field(min_length=1)
    tool_kwargs: dict[str, Any] = {}
    schedule_kwargs: dict[str, Any] = {}


async def _scheduling_backend_present() -> bool:
    """Whether an installed backend registers the scheduling marker tools."""
    tools = await tai42_app.tools.get_tools()
    return all(name in tools for name in _MARKER_TOOLS)


@operation(
    summary="List schedules",
    tags=["schedules"],
    errors=[NotSupportedError, PermissionDenied, UnavailableError, OperationFailed],
)
async def list_schedules() -> Any:
    if not await _scheduling_backend_present():
        raise NotSupportedError(_NO_BACKEND_MESSAGE)
    try:
        return await tai42_app.tools.run_tool(_LIST_TOOL, {})
    except UnknownToolError as exc:
        # Discriminate by NAME — see module docstring for the shared dispatch-wrap contract.
        if exc.tool_name == _LIST_TOOL:
            logger.warning(
                "list-schedules: %r passed the presence pre-check but did not resolve at dispatch; answering 501",
                _LIST_TOOL,
            )
            raise NotSupportedError(_NO_BACKEND_MESSAGE) from exc
        logger.exception("list-schedules %r raised unknown-tool %r during execution", _LIST_TOOL, exc.tool_name)
        raise OperationFailed(f"schedule listing failed (unknown tool {exc.tool_name})") from exc
    except OperationError:
        raise
    except Exception as exc:
        logger.exception("list-schedules %r raised during execution", _LIST_TOOL)
        raise OperationFailed(f"schedule listing failed ({type(exc).__name__})") from exc


@operation(
    summary="Get the server date and time",
    tags=["schedules"],
    errors=[NotSupportedError, PermissionDenied, UnavailableError, OperationFailed],
)
async def server_datetime() -> Any:
    try:
        return await tai42_app.tools.run_tool(_TIME_TOOL, {})
    except UnknownToolError as exc:
        if exc.tool_name == _TIME_TOOL:
            raise NotSupportedError(f"{_TIME_TOOL} tool is not available") from exc
        logger.exception("server-datetime %r raised unknown-tool %r during execution", _TIME_TOOL, exc.tool_name)
        raise OperationFailed(f"server-datetime lookup failed (unknown tool {exc.tool_name})") from exc
    except OperationError:
        raise
    except Exception as exc:
        logger.exception("server-datetime %r raised during execution", _TIME_TOOL)
        raise OperationFailed(f"server-datetime lookup failed ({type(exc).__name__})") from exc


@operation(
    summary="Create a schedule",
    tags=["schedules"],
    destructive=True,
    reload_gated=True,
    meta_executor=True,
    errors=[
        BadRequestError,
        NotFoundError,
        NotSupportedError,
        PermissionDenied,
        UnavailableError,
        OperationFailed,
    ],
    request_model=ScheduleCreate,
)
async def create_schedule(tool_name: str, tool_kwargs: dict[str, Any], schedule_kwargs: dict[str, Any]) -> Any:
    """Schedule a caller-named tool to run on a cadence — a run-ANY-tool door.

    The caller supplies ``tool_name``, so reaching this is arbitrary-tool-execution
    privilege (the recurring firing runs the named tool with real side effects). As a
    "run any tool by name" door it is a tier-1 meta-executor, never projected to the MCP
    surface — matching ``run_tool`` and ``submit_run``."""
    if not await _scheduling_backend_present():
        raise NotSupportedError(_NO_BACKEND_MESSAGE)
    dispatch_name, arguments = await _resolve_schedule_dispatch(tool_name, tool_kwargs, schedule_kwargs)
    # The recurring firing has no live caller, so this creation is the ONLY edge the inner
    # tool reaches — decide it here, over the exact arguments the dispatch below fires.
    await authorize_submitted_tool(dispatch_name, arguments)
    try:
        return await tai42_app.tools.run_tool(dispatch_name, arguments)
    except UnknownToolError as exc:
        if exc.tool_name == dispatch_name:
            raise NotFoundError(f"unknown tool: {dispatch_name}") from exc
        logger.exception("create-schedule %r raised unknown-tool %r during execution", dispatch_name, exc.tool_name)
        raise OperationFailed(f"schedule creation failed (unknown tool {exc.tool_name})") from exc
    except OperationError:
        raise
    except Exception as exc:
        logger.exception("create-schedule %r raised during execution", dispatch_name)
        raise OperationFailed(f"schedule creation failed ({type(exc).__name__})") from exc


@operation(
    summary="Delete a schedule",
    tags=["schedules"],
    reload_gated=True,
    errors=[NotSupportedError, PermissionDenied, UnavailableError, OperationFailed],
)
async def delete_schedule(schedule_name: str) -> Any:
    if not await _scheduling_backend_present():
        raise NotSupportedError(_NO_BACKEND_MESSAGE)
    try:
        return await tai42_app.tools.run_tool(_DELETE_TOOL, {"name": schedule_name})
    except UnknownToolError as exc:
        if exc.tool_name == _DELETE_TOOL:
            logger.warning(
                "delete-schedule: %r passed the presence pre-check but did not resolve at dispatch; answering 501",
                _DELETE_TOOL,
            )
            raise NotSupportedError(_NO_BACKEND_MESSAGE) from exc
        logger.exception("delete-schedule %r raised unknown-tool %r during execution", _DELETE_TOOL, exc.tool_name)
        raise OperationFailed(f"schedule deletion failed (unknown tool {exc.tool_name})") from exc
    except OperationError:
        raise
    except Exception as exc:
        logger.exception("delete-schedule %r raised during execution", _DELETE_TOOL)
        raise OperationFailed(f"schedule deletion failed ({type(exc).__name__})") from exc
