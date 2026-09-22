"""Scheduling operations — a thin skin over the run-tool seam, reporting honestly when no backend is installed.

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
  a structured :class:`OperationFailedError` (500);
* a typed :class:`OperationError` (most sharply ``PermissionDeniedError``) passes through as
  the answer it already is;
* any other exception becomes a structured ``OperationFailedError`` (500), never an opaque
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
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from pydantic import Field, model_validator
from tai42_contract.app import tai42_app
from tai42_contract.app.responses import OpaqueJson
from tai42_contract.interactions.door_contract import ParkableDoorMixin
from tai42_contract.states import StateBinding, StateSubject
from tai42_contract.template import EXPRESSION_ANNOTATION_KEY, TemplatedText, expression_annotation
from tai42_contract.tools import tool_call_frame
from tai42_kit.backend import fire_schedule_door
from tai42_kit.utils.data import text_to_md5
from tai42_kit.utils.schedule_subject import (
    SCHEDULE_CONTRACT_ARG,
    SCHEDULE_EXECUTION_FINGERPRINT_ARG,
    SCHEDULE_EXECUTION_KEY_ARG,
    SCHEDULE_NAME_KEY,
    ReservedScheduleKeyError,
    assert_no_reserved_schedule_keys,
    schedule_create_fire,
)

from tai42_skeleton.operations import (
    BadRequestError,
    NotFoundError,
    NotSupportedError,
    OperationError,
    OperationFailedError,
    PermissionDeniedError,
    UnavailableError,
    operation,
)
from tai42_skeleton.operations._authority import assert_execution_key_bindable, resolve_caller
from tai42_skeleton.operations._submitted_tool_authz import authorize_submitted_tool
from tai42_skeleton.tools.binding import UnknownToolError

logger = logging.getLogger(__name__)

# The tools an installed scheduling backend registers; their presence is the marker
# that scheduling is available.
_LIST_TOOL = "backend_list_schedules"
_DELETE_TOOL = "backend_delete_schedule"
_EXPORT_TOOL = "backend_export_schedules"
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
_CRON_KEY = "cron"
# The crontab fields ``normalize_schedule``'s crontab branch reads; a friendly create
# maps whichever are given onto ``{"type": "crontab", ...}``.
_CRONTAB_FIELD_KEYS = frozenset({"minute", "hour", "day_of_month", "month_of_year", "day_of_week"})


def _derive_schedule_name(tool_name: str, tool_kwargs: dict[str, Any], backend_schedule: Any) -> str:
    """A deterministic name when the caller gives none: the tool plus a fingerprint of the full schedule spec.

    The spec fingerprint covers the tool, its arguments, and the cadence. An identical re-add resolves to
    the same name and updates that schedule in place; any change of arguments or cadence
    yields a distinct name, so repeated adds never clobber a different schedule.
    """
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
    tool's arguments (the silent run-once path).
    """
    if tool_name.endswith(_SCHEDULE_BRANCH_SUFFIX) or _EXPERT_SCHEDULE_KEY in schedule_kwargs:
        return tool_name, {**tool_kwargs, **schedule_kwargs}

    keys = set(schedule_kwargs)
    stray = sorted(keys - ({_CRON_KEY, SCHEDULE_NAME_KEY} | _CRONTAB_FIELD_KEYS))
    if stray:
        raise BadRequestError(
            f"unrecognized schedule parameter(s) {stray} for base tool {tool_name!r}: a cadence is given as "
            f"{_CRON_KEY!r} (a crontab string) or the crontab fields {sorted(_CRONTAB_FIELD_KEYS)}, "
            f"optionally with an explicit {SCHEDULE_NAME_KEY!r}"
        )

    cron_given = _CRON_KEY in keys
    struct_given = bool(_CRONTAB_FIELD_KEYS & keys)
    if not cron_given and not struct_given:
        if SCHEDULE_NAME_KEY in keys:
            raise BadRequestError(
                f"{SCHEDULE_NAME_KEY!r} given without a cadence: add {_CRON_KEY!r} or the crontab fields"
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
    schedule_name = schedule_kwargs.get(SCHEDULE_NAME_KEY) or _derive_schedule_name(
        tool_name, tool_kwargs, backend_schedule
    )
    # Schedule keys win on collision so the backend's scheduling parameters cannot be
    # shadowed by the tool's own arguments.
    return branch_name, {**tool_kwargs, SCHEDULE_NAME_KEY: schedule_name, _EXPERT_SCHEDULE_KEY: backend_schedule}


class ScheduleCreate(ParkableDoorMixin):
    """Create a schedule that periodically runs ``tool_name`` on the cadence in ``schedule_kwargs``.

    ``tool_kwargs`` are the arguments each fire passes to the tool.
    ``state_binding`` is the OPTIONAL door-layer binding the fire applies. Unlike the per-schedule
    ``subject`` (a free key inside ``tool_kwargs`` that legitimately reaches the base tool), it is a
    top-level field applied around the started tool alone (never the base tool's).

    A schedule is a parkable-driving door: it carries the four :class:`ParkableDoorMixin` jqs
    (``start_expr`` builds the fired tool's kwargs; ``cancel_expr`` / ``resume_expr`` act on the run's
    parked interactions; ``extras_expr`` builds the started run's extras), each evaluated over the
    fired tool's arguments with the run's parked interactions bound as ``$parked``. Any contract jq
    needs an ``execution_key`` (the api-key ``user_id`` the recurring fire runs as, so a park it
    raises can be rebound and resumed under that authority); the create refuses a contract jq with no
    key.
    """

    tool_name: str = Field(min_length=1)
    tool_kwargs: dict[str, Any] = Field(default_factory=dict)
    schedule_kwargs: dict[str, Any] = Field(default_factory=dict)
    execution_key: str | None = Field(
        default=None,
        min_length=1,
        description="The api-key user_id a contract-bearing recurring fire runs as (required with any contract jq).",
    )
    state_binding: StateBinding | None = None

    @model_validator(mode="after")
    def _contract_needs_execution_key(self) -> ScheduleCreate:
        if self.execution_key is None and any(
            getattr(self, field) is not None for field in ("start_expr", "cancel_expr", "resume_expr", "extras_expr")
        ):
            raise ValueError("a schedule contract jq (start/cancel/resume/extras) requires an execution_key")
        return self


async def _scheduling_backend_present() -> bool:
    """Whether an installed backend registers the scheduling marker tools."""
    tools = await tai42_app.tools.get_tools()
    return all(name in tools for name in _MARKER_TOOLS)


@operation(
    summary="List schedules",
    tags=["schedules"],
    errors=[NotSupportedError, PermissionDeniedError, UnavailableError, OperationFailedError],
    response_model=OpaqueJson,
)
async def list_schedules() -> Any:
    """List the installed backend's schedules; raises 501 when no scheduling backend is present."""
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
        raise OperationFailedError(f"schedule listing failed (unknown tool {exc.tool_name})") from exc
    except OperationError:
        raise
    except Exception as exc:
        logger.exception("list-schedules %r raised during execution", _LIST_TOOL)
        raise OperationFailedError(f"schedule listing failed ({type(exc).__name__})") from exc


async def export_schedules_raw() -> Any:
    """Dispatch ``backend_export_schedules`` and return its raw ``ScheduleRecord`` rows.

    An in-process helper for the tool-rename SCHEDULE referee — deliberately NOT an
    ``@operation`` HTTP door: exported kwargs can carry sensitive tool arguments (the
    schedules backup section is secret), so the raw records surface only to the
    in-process referee, which reads schedule NAMES from them. Feature-off (no installed
    scheduling backend) raises :class:`NotSupportedError`. An absent ``backend_export_schedules``
    while the marker tools ARE present propagates its ``UnknownToolError`` unchanged, so
    the referee gates the rename loudly rather than treating an unreadable target set as
    "no holders"; every other failure wraps as the discipline in ``list_schedules``.
    """
    if not await _scheduling_backend_present():
        raise NotSupportedError(_NO_BACKEND_MESSAGE)
    try:
        return await tai42_app.tools.run_tool(_EXPORT_TOOL, {})
    except UnknownToolError as exc:
        # The export tool's own absence is NOT feature-off here: the marker tools passed
        # the presence pre-check, so the backend is installed but cannot report its
        # dispatch targets. Propagate loudly — the referee blocks on it.
        if exc.tool_name == _EXPORT_TOOL:
            raise
        logger.exception("export-schedules %r raised unknown-tool %r during execution", _EXPORT_TOOL, exc.tool_name)
        raise OperationFailedError(f"schedule export failed (unknown tool {exc.tool_name})") from exc
    except OperationError:
        raise
    except Exception as exc:
        logger.exception("export-schedules %r raised during execution", _EXPORT_TOOL)
        raise OperationFailedError(f"schedule export failed ({type(exc).__name__})") from exc


@operation(
    summary="Get the server date and time",
    tags=["schedules"],
    errors=[NotSupportedError, PermissionDeniedError, UnavailableError, OperationFailedError],
    response_model=OpaqueJson,
)
async def server_datetime() -> Any:
    """Return the server's current date and time; raises 501 when the time tool is not available."""
    try:
        return await tai42_app.tools.run_tool(_TIME_TOOL, {})
    except UnknownToolError as exc:
        if exc.tool_name == _TIME_TOOL:
            raise NotSupportedError(f"{_TIME_TOOL} tool is not available") from exc
        logger.exception("server-datetime %r raised unknown-tool %r during execution", _TIME_TOOL, exc.tool_name)
        raise OperationFailedError(f"server-datetime lookup failed (unknown tool {exc.tool_name})") from exc
    except OperationError:
        raise
    except Exception as exc:
        logger.exception("server-datetime %r raised during execution", _TIME_TOOL)
        raise OperationFailedError(f"server-datetime lookup failed ({type(exc).__name__})") from exc


def _validate_schedule_subject(tool_kwargs: dict[str, Any]) -> None:
    """A ``subject`` in a schedule's tool kwargs must be a well-formed :class:`~tai42_contract.states.StateSubject`.

    That value is what the fire re-establishes as its ``schedule``-door state context (the fire is anonymous, so the
    subject is stamped at creation, where it is known). A malformed one is refused HERE, loudly, so
    a job that could never resolve its subject is never persisted. Absent leaves the fire with no state context.
    """
    subject = tool_kwargs.get("subject")
    if subject is None:
        return
    try:
        StateSubject.model_validate(subject)
    except ValueError as exc:
        raise BadRequestError(f"invalid schedule subject: {exc}") from exc


# The jq-typed flat params carry the ``x-tai42-expression`` vendor annotation so the generated MCP
# tool form recognizes them as jq — the model-level annotation on ``ScheduleCreate`` never reaches
# this FLAT projection, which fastmcp derives from the signature alone. Each door-contract expression
# runs over the schedule's fired tool ARGUMENTS and reads the run's currently parked interactions as
# ``$parked``.
_SCHEDULE_ARGUMENTS_BLURB = "the schedule's fired tool arguments"
_SCHEDULE_PARKED_VARIABLE = (
    "parked",
    "the run's currently parked interactions on the schedule's subject — each with its id, status, "
    "to, asked_by, question and answer-format fields",
    [{"id": "i-42", "status": "asking", "to": "caller", "asked_by": ["main"], "answer_format": "confirm"}],
)


def _schedule_expr_field(label: str, returns: str) -> Any:
    return Field(
        json_schema_extra={
            EXPRESSION_ANNOTATION_KEY: expression_annotation(
                label=label,
                blurb=_SCHEDULE_ARGUMENTS_BLURB,
                variables=[_SCHEDULE_PARKED_VARIABLE],
                returns=returns,
            )
        }
    )


_SCHEDULE_START_EXPR_PARAM = Annotated[
    TemplatedText | None,
    _schedule_expr_field(
        "start expression", "the fired tool's kwargs; absent fires the stored arguments, null starts nothing"
    ),
]
_SCHEDULE_CANCEL_EXPR_PARAM = Annotated[
    TemplatedText | None,
    _schedule_expr_field("cancel expression", "null (cancel nothing), a parked interaction id, or a list of ids"),
]
_SCHEDULE_RESUME_EXPR_PARAM = Annotated[
    TemplatedText | None,
    _schedule_expr_field(
        "resume expression",
        "null (resume nothing), {id, payload} to resume an ask, a bare id to take a waiting outcome, or a list",
    ),
]
_SCHEDULE_EXTRAS_EXPR_PARAM = Annotated[
    TemplatedText | None,
    _schedule_expr_field("extras expression", "the extras mapping handed to the started target; null for no extras"),
]


def _refuse_reserved_schedule_keys(arguments: dict[str, Any]) -> None:
    """Refuse a caller-supplied reserved schedule key in the fired arguments, as a 400.

    The one refusal rule lives in the kit (:func:`assert_no_reserved_schedule_keys`, the same rule
    every backend enqueue door runs); this create door maps its :class:`ReservedScheduleKeyError` to a
    ``BadRequestError`` so the caller sees a 400. It runs BEFORE :func:`_stamp_recurring_reserved`
    stamps the validated door signals, so a forged key never survives to the fire.
    """
    try:
        assert_no_reserved_schedule_keys(arguments)
    except ReservedScheduleKeyError as exc:
        raise BadRequestError(str(exc)) from exc


def _stamp_recurring_reserved(
    arguments: dict[str, Any],
    *,
    state_binding: StateBinding | None,
    execution_key: str | None,
    fingerprint: str | None,
    contract: ParkableDoorMixin,
    has_contract: bool,
) -> None:
    """Stamp the reserved door kwargs a recurring fire's worker ``backend_fire`` pops, from validated fields.

    The binding rides the kit's ``schedule_task`` preparer as the plain ``state_binding`` key; the
    firing identity ``(user_id, fingerprint)`` pair and the contract are stamped here — the raw
    execution key never enters the queue in any other form.
    """
    if state_binding is not None:
        arguments["state_binding"] = state_binding.model_dump(mode="json")
    if execution_key is not None:
        arguments[SCHEDULE_EXECUTION_KEY_ARG] = execution_key
        arguments[SCHEDULE_EXECUTION_FINGERPRINT_ARG] = fingerprint
    if has_contract:
        arguments[SCHEDULE_CONTRACT_ARG] = contract.model_dump(mode="json")


async def _fire_run_once(
    dispatch_name: str,
    arguments: dict[str, Any],
    *,
    subject: StateSubject | None,
    state_binding: StateBinding | None,
    contract: ParkableDoorMixin,
) -> Any:
    """Fire a run-once (no-cadence) schedule immediately as a first-class door, returning its mapped response.

    Opens the DOOR-form minting frame (``name=None``) so a resume-then-re-park at create carries the
    create fire's ``run_delivery_id``; the create op binds no completion, so ``delivery`` is ``None``
    (a start-only HTTP door). The immediate fire runs under the create caller's own bound identity, and
    ``receives_outcome=True`` returns the dispatch's outcome to the HTTP caller.
    """
    with tool_call_frame(name=None):
        outcome = await fire_schedule_door(
            dispatch_name,
            arguments,
            subject=subject,
            state_binding=state_binding,
            contract=contract,
            receives_outcome=True,
        )
    return _run_once_response(outcome)


async def _dispatch_schedule_creation(dispatch_name: str, coro_factory: Callable[[], Awaitable[Any]]) -> Any:
    """Run ``coro_factory`` (the recurring schedule submit or the run-once fire), mapping its failures.

    The unknown target vanishing between lookup and dispatch is a 404; a typed operation error is the
    inner tool's own answer and propagates; any other raise is a wrapped creation failure.
    """
    try:
        return await coro_factory()
    except UnknownToolError as exc:
        if exc.tool_name == dispatch_name:
            raise NotFoundError(f"unknown tool: {dispatch_name}") from exc
        logger.exception("create-schedule %r raised unknown-tool %r during execution", dispatch_name, exc.tool_name)
        raise OperationFailedError(f"schedule creation failed (unknown tool {exc.tool_name})") from exc
    except OperationError:
        raise
    except Exception as exc:
        logger.exception("create-schedule %r raised during execution", dispatch_name)
        raise OperationFailedError(f"schedule creation failed ({type(exc).__name__})") from exc


def _run_once_response(outcome: Any) -> Any:
    """Map a run-once fire's ``VisitOutcome`` to the create op's ``OpaqueJson`` response.

    ``result`` → the dispatch's raw body (today's shape); ``asks`` → the parked caller ask entries;
    ``parked`` → the park notice (the parked interaction ids); ``none`` → ``null``.
    """
    if outcome.kind == "result":
        return outcome.result
    if outcome.kind == "asks":
        return [ask.model_dump(mode="json", exclude_none=True) for ask in outcome.asks]
    if outcome.kind == "parked":
        return {"parked": outcome.suspended.interaction_ids if outcome.suspended is not None else []}
    return None


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
        PermissionDeniedError,
        UnavailableError,
        OperationFailedError,
    ],
    request_model=ScheduleCreate,
    response_model=OpaqueJson,
)
async def create_schedule(
    tool_name: str,
    tool_kwargs: dict[str, Any],
    schedule_kwargs: dict[str, Any],
    execution_key: str | None = None,
    state_binding: StateBinding | None = None,
    start_expr: _SCHEDULE_START_EXPR_PARAM = None,
    cancel_expr: _SCHEDULE_CANCEL_EXPR_PARAM = None,
    resume_expr: _SCHEDULE_RESUME_EXPR_PARAM = None,
    extras_expr: _SCHEDULE_EXTRAS_EXPR_PARAM = None,
) -> Any:
    """Schedule a caller-named tool to run on a cadence — a run-ANY-tool door.

    The caller supplies ``tool_name``, so reaching this is arbitrary-tool-execution
    privilege (the recurring firing runs the named tool with real side effects). As a
    "run any tool by name" door it is a tier-1 meta-executor, never projected to the MCP
    surface — matching ``run_tool`` and ``submit_run``.

    The four door-contract jqs make the schedule a parkable-driving door: a recurring fire evaluates
    them each firing over the tool's arguments with ``$parked`` bound; a RUN-ONCE shape (no cadence)
    honours them at the immediate create-time dispatch through the SAME door mechanism. A contract jq
    requires ``execution_key`` — the identity a receiver-less recurring fire binds to rebind/resume a
    park it raises.
    """
    if not await _scheduling_backend_present():
        raise NotSupportedError(_NO_BACKEND_MESSAGE)
    _validate_schedule_subject(tool_kwargs)
    contract = ParkableDoorMixin(
        start_expr=start_expr, cancel_expr=cancel_expr, resume_expr=resume_expr, extras_expr=extras_expr
    )
    has_contract = any(
        getattr(contract, field) is not None for field in ("start_expr", "cancel_expr", "resume_expr", "extras_expr")
    )
    if has_contract and execution_key is None:
        raise BadRequestError("a schedule contract jq (start/cancel/resume/extras) requires an execution_key")
    dispatch_name, arguments = await _resolve_schedule_dispatch(tool_name, tool_kwargs, schedule_kwargs)
    _refuse_reserved_schedule_keys(arguments)
    recurring = dispatch_name.endswith(_SCHEDULE_BRANCH_SUFFIX) or _EXPERT_SCHEDULE_KEY in arguments
    if state_binding is not None:
        from tai42_skeleton.app import instance
        from tai42_skeleton.tools.state_binding import validate_and_attach_binding

        # Attach-on-use + validate the binding at SAVE (create), before persisting the schedule — a
        # bad binding fails the create loudly, never a schedule that fires broken.
        await validate_and_attach_binding(instance.app, state_binding)
    fingerprint: str | None = None
    if execution_key is not None:
        # Validate the key is bindable and derive its per-mint fingerprint, the way a hook does; the
        # stored identity is that (user_id, fingerprint) pair — the raw key never enters the queue.
        fingerprint = await assert_execution_key_bindable(await resolve_caller(), execution_key)
    subject_value = tool_kwargs.get("subject")
    subject = StateSubject.model_validate(subject_value) if subject_value is not None else None
    # The recurring firing has no live caller, so this creation is the ONLY edge the inner tool
    # reaches — decide it here, over the exact arguments the dispatch below fires.
    await authorize_submitted_tool(dispatch_name, arguments)

    async def _dispatch() -> Any:
        if recurring:
            _stamp_recurring_reserved(
                arguments,
                state_binding=state_binding,
                execution_key=execution_key,
                fingerprint=fingerprint,
                contract=contract,
                has_contract=has_contract,
            )
            # Mark the branch dispatch as the platform's own create fire: the branch preparer trusts the
            # reserved keys stamped just above only when this marker is on the stack, so a caller who
            # names the ``schedule_task`` branch directly can never forge them.
            with schedule_create_fire():
                return await tai42_app.tools.run_tool(dispatch_name, arguments)
        return await _fire_run_once(
            dispatch_name, arguments, subject=subject, state_binding=state_binding, contract=contract
        )

    return await _dispatch_schedule_creation(dispatch_name, _dispatch)


@operation(
    summary="Delete a schedule",
    tags=["schedules"],
    reload_gated=True,
    errors=[NotSupportedError, PermissionDeniedError, UnavailableError, OperationFailedError],
    response_model=OpaqueJson,
)
async def delete_schedule(schedule_name: str) -> Any:
    """Delete the schedule named ``schedule_name``; raises 501 when no scheduling backend is present."""
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
        raise OperationFailedError(f"schedule deletion failed (unknown tool {exc.tool_name})") from exc
    except OperationError:
        raise
    except Exception as exc:
        logger.exception("delete-schedule %r raised during execution", _DELETE_TOOL)
        raise OperationFailedError(f"schedule deletion failed ({type(exc).__name__})") from exc
