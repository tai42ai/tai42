"""Hook-management operations — the authed hooks surface the Studio drives, over ``get_hooks_manager()``.

* ``list_hooks`` lists registered hooks (filtered to ``topic`` when given) plus the
  per-topic verifier bindings and each covered topic's derived ``trigger_auth`` — one
  read the Studio hooks UI consumes.
* ``register_hook`` registers a hook from its FLAT parameters (the client-facing
  ``HookRegister`` fields spread as arguments; the server-derived fingerprint is not
  among them) — an upsert, so it is the edit path too.
* ``unregister_hook`` removes a hook by name; an unknown name is a loud 404.
* ``list_verifiers`` lists the registered webhook-verifier names (the bind catalog).
* ``set_topic_verifier`` binds (or replaces) a topic's webhook verifier; an unknown
  verifier name is rejected at bind time (400).
* ``delete_topic_verifier`` removes a topic's binding — reopening its public ingress
  door to anyone who knows the topic name; a missing binding is a 404.
* ``list_hook_runs`` lists the kept hook runs (outstanding, failed, or both) with their
  totals, never their payloads; ``retry_hook_run`` re-queues a failed run as a fresh run
  with the same id; ``discard_hook_run`` deletes a failed run. A missing run is a 404 and a
  run that is not failed, or whose hook committed a resume or a take (retry only), a 409.

These operations are the single source the ``/api/hooks*`` routes, the ``tai hooks``
CLI, and the MCP tool surface derive from — one definition behind every edge. Not all of
them project as MCP tools by DEFAULT: the five ``authority_changing`` ones —
``register_hook``, ``set_topic_verifier``, ``delete_topic_verifier``,
``create_trigger_link`` and ``delete_trigger_link`` — are tier-2 and reachable as tools
only through an explicit ``api_tools.include``, because each wires, mints or revokes
authority on a public URL, or decides the lock on one. The rest project normally, and
the HTTP doors and the CLI cover every one of them either way. A hook's ``tool_kwargs``
can carry caller-supplied values, so the list read is secret-bearing; that is acceptable
on this authed surface (as with config env).

The two verifier operations are additionally ADMIN-ONLY: their routes are ``fenced``, so
the per-tag level pass hard-fences them at the HTTP edge and at both tool edges. A
binding is the only authentication a topic's public ingress door has, and the topic
namespace carries no owner to key a per-caller rule on.

Both record writers — hook registration (an upsert, so the create AND the edit path)
and trigger-link mint — bind an ``execution_key``. A HOOK's key is the identity that
hook's fire runs as; a LINK's key is the identity its dispatch is gated on, the hooks
it fans out to each still firing as their own. Binding either is a pass-role decision
(:func:`~tai42_skeleton.operations._authority.assert_execution_key_bindable`), taken
BEFORE the write, so a refusal leaves no record behind.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, NoReturn

from pydantic import BaseModel, Field
from tai42_contract.app import tai42_app
from tai42_contract.errors import OperationError
from tai42_contract.hooks import HookParams, HookRegister, HookSubject, TopicVerifierBinding
from tai42_contract.states.binding import StateBinding
from tai42_contract.template import EXPRESSION_ANNOTATION_KEY, TemplatedText, expression_annotation

from tai42_skeleton.hooks import trigger_links
from tai42_skeleton.hooks.cache import get_hooks_manager
from tai42_skeleton.hooks.runs import runtime as hook_runs_runtime
from tai42_skeleton.hooks.runs.metrics import hook_run_metrics
from tai42_skeleton.hooks.runs.models import HookRun, page_offset
from tai42_skeleton.hooks.trigger_auth import webhook_trigger_auth
from tai42_skeleton.hooks.trigger_links import TriggerLinkError
from tai42_skeleton.operations import BadRequestError, NotFoundError, ValidationRejectedError, operation
from tai42_skeleton.operations._authority import assert_execution_key_bindable, resolve_caller
from tai42_skeleton.operations.definition_door import definition_door
from tai42_skeleton.operations.errors import ConflictError, ForbiddenError, NotSupportedError, OperationFailedError
from tai42_skeleton.operations.reference_check import RegistryReference
from tai42_skeleton.operations.reference_graph import assert_blob_references_resolve
from tai42_skeleton.operations.response_models.hooks import (
    HookListView,
    HookRegisterResult,
    HookRunClaimIntentView,
    HookRunDiscarded,
    HookRunsPage,
    HookRunView,
    RemovedByName,
    RemovedByTopic,
    TopicVerifierView,
    TriggerLinkCreated,
    TriggerLinkListView,
)
from tai42_skeleton.operations.response_models.shared import StringListResponse

logger = logging.getLogger(__name__)


class HookListQuery(BaseModel):
    """The hook listing's optional ``?topic=`` filter.

    Spec metadata only — the door parses its query at the HTTP edge.
    """

    topic: str | None = Field(
        default=None, description="Restrict the listing to hooks registered for this topic; omit for all hooks."
    )


class TriggerLinkCreate(BaseModel):
    """Mint a trigger link for a hook ``topic``.

    ``ttl_seconds`` is REQUIRED (no default ⇒ the key must be present) and STRICT
    (``"3600"``, ``3600.0``, ``3600.5`` and bools all reject under one regime): a
    positive int is a timed link, ``null`` is a permanent link, and ``0``/negative
    is a loud 400 — expiry is the creator's explicit choice with no default and no
    product ceiling. ``execution_key`` is the api-key identity the link's dispatch is
    gated on — the link dies with the key — and the creator must be allowed to
    delegate it; each hook the dispatch reaches still fires as its OWN bound key.
    ``require_api_key`` makes the link's door demand an authenticated principal ON TOP
    of the token — one the authentication backend admits, and, when that principal is
    governed by a ROLE, one the ordinary ``hooks``-tag level pass admits at the request's
    method (``read`` for GET, ``write`` for POST); the governing policy is the OWNER's for
    an owned key, so a key escapes that pass exactly when its governing policy is admin or
    carries no role pointer. The
    default is token-only — the QR-on-a-wall case. ``topic`` must be non-empty, the same
    rule the stored record enforces, so the mint door never writes a link its own restore
    would refuse. ``tool_kwargs`` (optional) is stored on the link and merged into every
    fired hook's input BELOW that hook's own static ``tool_kwargs``, so it supplies only
    the arguments the hook's author left unpinned — a colliding key stays the author's.
    """

    topic: str = Field(min_length=1)
    execution_key: str = Field(min_length=1)
    name: str | None = None
    ttl_seconds: int | None = Field(strict=True)
    tool_kwargs: dict[str, Any] | None = None
    require_api_key: bool = False


# The status → operation-error mapping shared by the three trigger-link adapters,
# mirroring how ``ClaimLinkError`` maps to the operation error surface.
_TRIGGER_ERROR_BY_STATUS: dict[int, type[OperationError]] = {
    400: BadRequestError,
    404: NotFoundError,
    409: ConflictError,
    501: NotSupportedError,
}


def _map_trigger_error(exc: TriggerLinkError) -> OperationError:
    error_cls = _TRIGGER_ERROR_BY_STATUS.get(exc.status, OperationError)
    return error_cls(exc.message)


@operation(
    summary="List registered hooks",
    tags=["hooks"],
    request_model=HookListQuery,
    response_model=HookListView,
)
async def list_hooks(topic: str | None = None) -> dict[str, Any]:
    """List registered hooks plus the per-topic verifier bindings.

    With a topic, lists only the hooks registered for that topic; without one,
    lists every registered hook. The response carries the hooks under ``items``
    (with ``total``), the per-topic verifier bindings under ``topic_verifiers``, and
    under ``trigger_auth`` how a topic's webhook ingress door authenticates its caller —
    DERIVED from the live verifier bindings just read, never stored. ``trigger_auth`` is
    keyed by every topic among the LISTED hooks plus every topic currently carrying a
    verifier binding (one may be bound before any hook exists on it), so under a
    ``topic`` filter it is neither a subset nor a superset of the listed topics.
    """
    manager = get_hooks_manager()
    hooks = await manager.list_hooks_by_topic(topic=topic) if topic else await manager.list_hooks()
    items = [params.model_dump(mode="json") for params in hooks.values()]
    topic_verifiers = await manager.all_topic_verifiers()
    # Every topic the reader can see here: the listed hooks' topics plus every topic
    # carrying a binding (one may be bound before any hook is registered on it).
    topics = {params.topic for params in hooks.values()} | set(topic_verifiers)
    trigger_auth = {name: webhook_trigger_auth(verifier_bound=name in topic_verifiers) for name in sorted(topics)}
    return {
        "items": items,
        "total": len(items),
        "topic_verifiers": {name: binding.model_dump(mode="json") for name, binding in topic_verifiers.items()},
        "trigger_auth": trigger_auth,
    }


# The jq-typed flat params carry the ``x-tai42-expression`` vendor annotation so the
# generated MCP tool form (the RunPanel door) recognizes them as jq — the model-level
# annotation on ``HookRegister``'s expressions never reaches this FLAT projection, which
# fastmcp derives from the signature alone. The payloads state the HOOK surface's facts:
# every expression runs over the parsed webhook delivery (payload_parser merges structured
# bodies — JSON objects, XML, form fields — top-level with the query string; arbitrary text
# arrives under raw_body). A hook's condition proceeds on any TRUTHY result (``bool(result)``,
# unlike access control's strict-true); the four door-contract expressions each read the run's
# currently parked interactions as ``$parked`` beside the delivery. Declared as ``Annotated``
# metadata on the parameter type so the schema fastmcp builds off the flat signature carries
# it; schema METADATA only, never validation.
_HOOK_DELIVERY_BLURB = (
    "the parsed webhook delivery (structured bodies merged top-level with query params; arbitrary text under raw_body)"
)
# The parked-interactions variable every door-contract flat param reads as ``$parked``.
_HOOK_PARKED_VARIABLE = (
    "parked",
    "the run's currently parked interactions on the hook's subject — each with its id, status, to, "
    "asked_by, question and answer-format fields",
    [{"id": "i-42", "status": "asking", "to": "caller", "asked_by": ["main"], "answer_format": "confirm"}],
)
_HOOK_CONDITION_PARAM = Annotated[
    TemplatedText | None,
    Field(
        json_schema_extra={
            EXPRESSION_ANNOTATION_KEY: expression_annotation(
                label="condition",
                blurb="the parsed webhook delivery (structured bodies merged top-level with query "
                "params; arbitrary text under raw_body)",
                returns="truthy to fire the hook; a falsy result skips it",
                caveats=[
                    "an absent/empty condition always fires; a present condition that "
                    "produces no output errors the fire loudly (it is not a silent skip)"
                ],
            )
        }
    ),
]
_HOOK_START_EXPR_PARAM = Annotated[
    TemplatedText | None,
    Field(
        json_schema_extra={
            EXPRESSION_ANNOTATION_KEY: expression_annotation(
                label="start expression",
                blurb=_HOOK_DELIVERY_BLURB,
                variables=[_HOOK_PARKED_VARIABLE],
                returns="an object — the fired tool's kwargs (the hook's static tool_kwargs win on a key clash)",
                caveats=[
                    "absent fires the tool with its static tool_kwargs only; a present start expression "
                    "must yield an object or null — null starts nothing (the run may still be "
                    "cancelled/resumed by the other expressions)"
                ],
            )
        }
    ),
]
_HOOK_CANCEL_EXPR_PARAM = Annotated[
    TemplatedText | None,
    Field(
        json_schema_extra={
            EXPRESSION_ANNOTATION_KEY: expression_annotation(
                label="cancel expression",
                blurb=_HOOK_DELIVERY_BLURB,
                variables=[_HOOK_PARKED_VARIABLE],
                returns="null (cancel nothing), a parked interaction id, or a list of ids to cancel",
            )
        }
    ),
]
_HOOK_RESUME_EXPR_PARAM = Annotated[
    TemplatedText | None,
    Field(
        json_schema_extra={
            EXPRESSION_ANNOTATION_KEY: expression_annotation(
                label="resume expression",
                blurb=_HOOK_DELIVERY_BLURB,
                variables=[_HOOK_PARKED_VARIABLE],
                returns=(
                    "null (resume nothing), {id, payload} to resume an ask with an answer, a bare id to "
                    "take a waiting outcome, or a list of these"
                ),
            )
        }
    ),
]
_HOOK_EXTRAS_EXPR_PARAM = Annotated[
    TemplatedText | None,
    Field(
        json_schema_extra={
            EXPRESSION_ANNOTATION_KEY: expression_annotation(
                label="extras expression",
                blurb=_HOOK_DELIVERY_BLURB,
                variables=[_HOOK_PARKED_VARIABLE],
                returns="the extras mapping handed to the started target; null for no extras",
            )
        }
    ),
]


# ``authority_changing``: registering binds an api-key identity onto a topic the PUBLIC
# ``/universal_webhook/{topic}`` door fires unauthenticated.
@operation(
    summary="Register a hook",
    tags=["hooks"],
    destructive=True,
    authority_changing=True,
    errors=[BadRequestError, ConflictError, ForbiddenError, NotFoundError, OperationFailedError],
    request_model=HookRegister,
    response_model=HookRegisterResult,
)
async def register_hook(
    name: str,
    topic: str,
    tool: str,
    execution_key: str,
    tool_kwargs: dict[str, Any] | None = None,
    condition: _HOOK_CONDITION_PARAM = None,
    start_expr: _HOOK_START_EXPR_PARAM = None,
    cancel_expr: _HOOK_CANCEL_EXPR_PARAM = None,
    resume_expr: _HOOK_RESUME_EXPR_PARAM = None,
    extras_expr: _HOOK_EXTRAS_EXPR_PARAM = None,
    *,
    subject: HookSubject | None = None,
    state_binding: StateBinding | None = None,
) -> dict[str, Any]:
    """Register a hook from its flat parameters — an UPSERT (the create path and the edit path).

    Supports conditional execution (``condition``) and the four door-contract expressions
    (``start_expr`` builds the fired tool's kwargs; ``cancel_expr`` / ``resume_expr`` act on the
    run's parked interactions; ``extras_expr`` builds the started run's extras) — each a templated
    text carrying its jq inline or by stored id — plus dynamic tool arguments.
    ``execution_key`` is the api-key identity every fire runs as; the caller's authority
    to delegate it and its usability by a tokenless fire are decided BEFORE the upsert, so
    a refusal stores nothing and leaves any existing hook of that name untouched. Returns
    ``{"registered", "name"}``; ``registered`` is ``True`` for a create and a replace
    alike. The hook tool and every name its ``tool_kwargs`` compose must be served (a 400);
    what ``start_expr`` builds at fire is judged at fire.
    """
    execution_key_fingerprint = await assert_execution_key_bindable(await resolve_caller(), execution_key)
    await assert_blob_references_resolve(RegistryReference("hook tool", tool), tool, tool_kwargs or {}, "tool_kwargs")
    # The model parse and the write service both run inside the definition mapping: a field the
    # model refuses, a jq that does not compile and a binding the states store refuses are each a
    # 4xx that stores no hook; a store or transport failure stays unmapped (a 500).
    with definition_door("hook params"):
        params = HookParams(
            name=name,
            topic=topic,
            tool=tool,
            execution_key=execution_key,
            execution_key_fingerprint=execution_key_fingerprint,
            tool_kwargs=tool_kwargs or {},
            subject=subject,
            state_binding=state_binding,
            condition=condition,
            start_expr=start_expr,
            cancel_expr=cancel_expr,
            resume_expr=resume_expr,
            extras_expr=extras_expr,
        )
        registered = await get_hooks_manager().register(params)
    return {"registered": registered, "name": name}


@operation(summary="Unregister a hook", tags=["hooks"], errors=[NotFoundError], response_model=RemovedByName)
async def unregister_hook(name: str) -> dict[str, Any]:
    """Unregister a hook by name. An unknown hook name is a loud 404.

    Returns ``{"removed", "name"}``.
    """
    removed = await get_hooks_manager().unregister(name=name)
    if not removed:
        raise NotFoundError(f"hook not found: {name!r}")
    return {"removed": True, "name": name}


@operation(summary="List registered webhook verifiers", tags=["hooks"], response_model=StringListResponse)
async def list_verifiers() -> list[str]:
    """The sorted names of every registered webhook verifier — the catalog the Studio bind form offers.

    Offered instead of free text. Names ONLY: a verifier object and a bound config are
    secret-adjacent and never leave this door. An empty registry (no verifier lifecycle
    module loaded) is a valid state, so it returns ``[]``.
    """
    # Reached through the concrete app singleton because ``names`` rides the skeleton
    # facet, not the tai42-contract ``AppWebhookVerifiers`` protocol.
    from tai42_skeleton.app import instance

    return instance.app.webhook_verifiers.names()


# ``authority_changing``: binding is also REBINDING — it replaces the lock a topic
# already carries, so fencing only the unbind would leave the rebind as its bypass.
@operation(
    summary="Bind a webhook verifier to a topic",
    tags=["hooks"],
    destructive=True,
    authority_changing=True,
    errors=[BadRequestError],
    request_model=TopicVerifierBinding,
    response_model=TopicVerifierView,
)
async def set_topic_verifier(topic: str, verifier: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Bind (or replace) a topic's webhook verifier.

    The verifier name is resolved against the registry here — an unknown name is
    rejected at BIND time (400), never left to fail at delivery.
    """
    try:
        tai42_app.webhook_verifiers.get(verifier)
    except Exception as exc:
        raise BadRequestError(f"unknown webhook verifier: {verifier!r}") from exc
    await get_hooks_manager().set_topic_verifier(topic, {"verifier": verifier, "config": config or {}})
    return {"topic": topic, "verifier": verifier}


# ``authority_changing``: the binding is the ONLY authentication
# ``/universal_webhook/{topic}`` has — unbinding reopens that public door and takes every
# out-of-service trigger link on the topic back into service.
@operation(
    summary="Unbind a topic's webhook verifier",
    tags=["hooks"],
    destructive=True,
    authority_changing=True,
    errors=[NotFoundError],
    response_model=RemovedByTopic,
)
async def delete_topic_verifier(topic: str) -> dict[str, Any]:
    """Remove a topic's verifier binding, reopening its public ingress door.

    A missing binding is a loud 404.
    """
    removed = await get_hooks_manager().delete_topic_verifier(topic)
    if not removed:
        raise NotFoundError(f"no verifier bound to topic: {topic!r}")
    return {"removed": True, "topic": topic}


# -- Trigger links -----------------------------------------------------------
#
# A trigger link is a PUBLIC capability URL that fires a hook topic; the CRUD lives
# under the ``hooks`` tag (grantable read/write). Both mutating ops are
# ``authority_changing`` so an agent never silently mints or revokes a public
# capability through the MCP tool surface. The redis hooks backend is required — an
# in-memory deployment refuses with a loud 501 (``NotSupportedError``), a capability
# the deployment does not provide, distinct from a transient 503 outage.


@operation(
    summary="Create a trigger link",
    tags=["hooks"],
    destructive=True,
    authority_changing=True,
    errors=[BadRequestError, ConflictError, ForbiddenError, NotFoundError, NotSupportedError],
    request_model=TriggerLinkCreate,
    response_model=TriggerLinkCreated,
)
async def create_trigger_link(
    topic: str,
    execution_key: str,
    name: str | None,
    ttl_seconds: int | None,
    tool_kwargs: dict[str, Any] | None,
    require_api_key: bool = False,
) -> dict[str, Any]:
    """Mint a trigger link for ``topic``.

    ``ttl_seconds`` is the creator's explicit choice (``null`` permanent, positive
    timed; ``0``/negative → 400); a unique name is generated when omitted; a
    verifier-bound topic is refused (400); ``tool_kwargs`` rides every fire, filling
    only the arguments each fired hook's author left
    unpinned. ``execution_key`` is the api-key identity the link's dispatch is gated on,
    decided BEFORE the mint so a refusal leaves no live URL. ``require_api_key`` makes
    the link's door demand an authenticated caller beside the token. Returns
    ``{"name", "trigger_path", "token", "topic", "expires_at"}``
    — the token appears ONLY here (nothing else stores or lists it).

    ``created_by`` is stamped from the AMBIENT caller identity, never a request field
    (which would be caller-spoofable); with the gate off there is no principal and it
    is stored ``null``.
    """
    caller = await resolve_caller()
    execution_key_fingerprint = await assert_execution_key_bindable(caller, execution_key)
    try:
        return await trigger_links.create_trigger_link(
            topic=topic,
            name=name,
            ttl_seconds=ttl_seconds,
            tool_kwargs=tool_kwargs,
            execution_key=execution_key,
            execution_key_fingerprint=execution_key_fingerprint,
            require_api_key=require_api_key,
            created_by=caller.caller_id,
        )
    except TriggerLinkError as exc:
        raise _map_trigger_error(exc) from exc


@operation(
    summary="List trigger links",
    tags=["hooks"],
    errors=[NotSupportedError],
    response_model=TriggerLinkListView,
)
async def list_trigger_links() -> dict[str, Any]:
    """Every live trigger link's record plus its hash PREFIX and its derived ``trigger_auth``.

    Never a raw token (none is stored; a listed link's QR is unrecoverable by design).
    Returns ``{"items", "total"}``.
    """
    try:
        return await trigger_links.list_trigger_links()
    except TriggerLinkError as exc:
        raise _map_trigger_error(exc) from exc


@operation(
    summary="Delete a trigger link",
    tags=["hooks"],
    destructive=True,
    authority_changing=True,
    errors=[NotFoundError, NotSupportedError],
    response_model=RemovedByName,
)
async def delete_trigger_link(name: str) -> dict[str, Any]:
    """Revoke a trigger link by name — immediate and DURABLE.

    A permanent tombstone keeps a restored backup from re-arming it. An unknown name is a
    loud 404. Returns ``{"removed", "name"}``.
    """
    try:
        await trigger_links.revoke_trigger_link(name)
    except TriggerLinkError as exc:
        raise _map_trigger_error(exc) from exc
    return {"removed": True, "name": name}


# -- hook runs: the operator doors over failed and outstanding runs ------------

_MAX_RUNS_LIMIT = 200


def _run_time(value: float) -> datetime:
    return datetime.fromtimestamp(value, tz=UTC)


def hook_run_view(run: HookRun) -> dict[str, Any]:
    """A hook run as the doors serve it: no event payload, no start spec."""
    intent = run.claim_intent
    return HookRunView(
        id=run.id,
        topic=run.topic,
        hook=run.hook,
        status=run.status,
        attempts=run.attempts,
        next_attempt_at=_run_time(run.next_attempt_at),
        acted=run.acted,
        acted_ids=list(run.acted_ids),
        claim_intent=None if intent is None else HookRunClaimIntentView(action=intent.action, ids=list(intent.ids)),
        last_error=run.last_error,
        last_error_kind=run.last_error_kind,
        created_at=_run_time(run.created_at),
        failed_at=None if run.failed_at is None else _run_time(run.failed_at),
    ).model_dump(mode="json")


def _refused_retry_text(run: HookRun) -> str:
    """Why a failed run whose hook committed a resume or a take cannot be retried, and what to do."""
    ids = list(run.acted_ids)
    if run.acted == "resume":
        return (
            f"hook run {run.id}: its hook resumed {ids}; the resumed run's continuation keeps its due record "
            "and the interactions reaper redelivers it, so a retry would act on it twice. Discard this run "
            "once handled."
        )
    return (
        f"hook run {run.id}: its hook took the waiting outcome of {ids}; a taken outcome is removed when it "
        "is taken and nothing redelivers it, so a retry cannot take it again. Its failure is in last_error. "
        "Discard this run once handled."
    )


def _refuse_absent(run_id: str, outcome: str) -> NoReturn:
    if outcome == "missing":
        raise NotFoundError(f"no hook run {run_id}")
    raise ConflictError(f"hook run {run_id} is not failed")


@operation(
    summary="List hook runs",
    tags=["hooks"],
    errors=[ValidationRejectedError],
    response_model=HookRunsPage,
)
async def list_hook_runs(
    status: Literal["outstanding", "failed"] | None = None, limit: int = 50, cursor: str | None = None
) -> dict[str, Any]:
    """Kept hook runs, with the outstanding and failed totals; never their payloads.

    ``status`` ``outstanding`` lists the pending and running runs, the soonest due first;
    ``failed`` the failed runs, the most recently failed first; none lists both, outstanding
    first. A page holds at most ``limit`` runs; ``cursor`` continues from a page's ``next_cursor``.
    """
    if status not in (None, "outstanding", "failed"):
        raise ValidationRejectedError(f"status is 'outstanding' or 'failed', got {status!r}")
    if not 1 <= limit <= _MAX_RUNS_LIMIT:
        raise ValidationRejectedError(f"limit is between 1 and {_MAX_RUNS_LIMIT}, got {limit}")
    try:
        page_offset(cursor)
    except ValueError as exc:
        raise ValidationRejectedError(str(exc)) from None
    manager = get_hooks_manager()
    page = await manager.list_hook_runs(status, limit, cursor)
    counts = await manager.count_hook_runs()
    return {
        "items": [hook_run_view(run) for run in page.items],
        "next_cursor": page.next_cursor,
        "outstanding": counts.outstanding,
        "failed": counts.failed,
    }


@operation(
    summary="Retry a failed hook run",
    tags=["hooks"],
    destructive=False,
    errors=[NotFoundError, ConflictError],
    response_model=HookRunView,
)
async def retry_hook_run(id: str) -> dict[str, Any]:  # noqa: A002 - the door's path parameter
    """Re-queue a failed hook run as a fresh run with the same id, and so the same idempotency key.

    A run whose hook started its tool replays that start alone; any other runs its hook again on
    the current parked entries. A run whose hook committed a resume or a take is refused: retrying
    it would act twice, so it can only be discarded. Answers the re-queued run.
    """
    manager = get_hooks_manager()
    now = hook_runs_runtime.now()
    spawn = hook_runs_runtime.on_serving_loop() and not hook_runs_runtime.stopping()
    if spawn:
        result = await manager.retry_hook_run(
            id,
            now,
            claimed_by=hook_runs_runtime.claimed_by(),
            lease_until=now + manager.settings.run_lease_seconds,
        )
    else:
        result = await manager.retry_hook_run(id, now)
    if result.outcome == "refused":
        if result.run is None:
            raise RuntimeError(f"the hook-run store refused retrying {id} without the run")
        raise ConflictError(_refused_retry_text(result.run))
    if result.outcome != "ok":
        _refuse_absent(id, result.outcome)
    run = result.run
    if run is None:
        raise RuntimeError(f"the hook-run store re-queued {id} without the run")
    hook_run_metrics().retried.inc()
    logger.info("hook run %s (%s on %s) retried by an operator", run.id, run.hook, run.topic)
    if spawn:
        hook_runs_runtime.spawn_attempt(run)
    return hook_run_view(run)


@operation(
    summary="Discard a failed hook run",
    tags=["hooks"],
    destructive=True,
    errors=[NotFoundError, ConflictError],
    response_model=HookRunDiscarded,
)
async def discard_hook_run(id: str) -> dict[str, Any]:  # noqa: A002 - the door's path parameter
    """Delete a failed hook run for good: its hook never runs again for that event."""
    from tai42_skeleton.access_control.user import request_identity

    outcome = await get_hooks_manager().discard_hook_run(id)
    if outcome != "ok":
        _refuse_absent(id, outcome)
    principal, _restricted = request_identity()
    hook_run_metrics().discarded.inc()
    logger.warning("hook run %s discarded by %s", id, principal)
    return {"discarded": id}
