"""The human answer operation for the ask_user interactions surface.

``answer_interaction`` is the authenticated human answer door
(``POST /api/interactions/{interaction_id}/answer``): the value is validated
server-side against the stored question's ``answer_format`` before the blocked
caller is woken; an invalid answer is rejected loudly and the caller stays
blocked. An EXTERNAL question is answered through its callback URL, never here.

The answer-validation helpers (``_validate_answer``, ``_schema_mismatch``, …),
the reply-TTL clamp, and the serializer-guarded claim live here because the
router's still-handler callback door shares the exact same rules — it imports
them from this module (the store claim, the typed-format validation, the reply
TTL). The router's HTTP-edge extractor reads/parses the request body (the byte
cap → 413, invalid JSON / missing ``answer`` → 400) and hands this operation the
already-parsed ``answer`` value.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import jsonschema
from pydantic import BaseModel, Field
from pydantic_core import PydanticSerializationError
from tai42_contract.interactions import (
    AnswerFormat,
    InteractionRequest,
    InteractionResponse,
)
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.access_control.user import request_identity
from tai42_skeleton.interactions.continuation import continuation_due_timing, fire_continuation_after_claim
from tai42_skeleton.interactions.settings import interactions_settings, interactions_store_configured
from tai42_skeleton.interactions.store import InteractionStore
from tai42_skeleton.operations import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    PayloadTooLargeError,
    operation,
)

# The synthetic label recorded when access control is disabled and no caller
# identity exists. A namespaced ``system:`` sentinel (mirroring the
# ``external-callback`` label the callback door records) cannot collide with a
# looked-up user id.
_NO_AUTH_ANSWERED_BY = "system:no-auth"


class _AnswerInvalid(Exception):
    """Raised when a human-door answer fails its stored-format validation.

    ``field`` is the failing answer field's dotted path when the fault is located
    to one (a form schema mismatch), else ``None``; the callback door surfaces it
    as the 400 body's optional ``field`` key so a channel can pin the error on the
    right control."""

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


class InteractionAnswer(BaseModel):
    """An answer to a pending interaction — the ``answer`` value validated at
    runtime against the interaction's own answer schema."""

    answer: Any


def _reply_ttl(request: InteractionRequest) -> int:
    """Short TTL for the reply key ≈ the remaining timeout budget, so a late
    answer to a timed-out question expires instead of resurrecting it."""
    remaining = int((request.timeout_at - datetime.now(UTC)).total_seconds())
    return max(1, remaining)


def _schema_error_field(exc: Exception) -> str | None:
    """The failing ANSWER field's dotted path for a field-located
    ``jsonschema.ValidationError`` (``count``, ``a.b``), or ``None`` when the fault
    has no answer-field location. Only ``jsonschema.ValidationError`` locates a
    fault in the answer: its ``.json_path`` (``$``-rooted, e.g. ``$.count``) names
    the field. ``SchemaError`` also carries a ``.json_path``, but it points INTO the
    stored schema (e.g. ``properties.x.type``) — a location no answering human owns
    — so it is never surfaced; a malformed schema, a root-level ValidationError with
    ``json_path == "$"``, and ``RecursionError`` all yield ``None``."""
    json_path = exc.json_path if isinstance(exc, jsonschema.ValidationError) else None
    if isinstance(json_path, str) and json_path not in ("", "$"):
        # Drop the ``$`` root and a leading ``.`` so a top-level field reads as
        # ``count`` rather than ``$.count``; a nested path keeps its dotted shape.
        return json_path[1:].removeprefix(".")
    return None


def _schema_error_message(exc: Exception) -> str:
    """The 400 message for a schema mismatch, naming the failing ANSWER field (via
    ``_schema_error_field``) when the error locates one so a human on any surface can
    tell WHICH field failed; a pathless fault falls back to the bare message."""
    message = getattr(exc, "message", None) or str(exc)
    field = _schema_error_field(exc)
    if field is not None:
        return f"answer does not match schema at {field}: {message}"
    return f"answer does not match schema: {message}"


# Failures of validating/parsing untrusted input convert to a loud 400; any
# exception outside these sets is a server bug and propagates as a 500.
# RecursionError covers recursive schemas / deeply-nested answers blowing up the
# validator.
_SCHEMA_VALIDATION_ERRORS = (jsonschema.ValidationError, jsonschema.SchemaError, RecursionError)


def _schema_mismatch(answer: Any, schema: dict) -> tuple[str, str | None] | None:
    """Validate ``answer`` against ``schema``; return ``(message, field)`` on a
    validation failure — ``message`` the 400 text, ``field`` the failing answer
    field's dotted path (``None`` for a root-level or otherwise non-locatable fault)
    — or ``None`` when the answer conforms."""
    try:
        jsonschema.validate(answer, schema)
    except _SCHEMA_VALIDATION_ERRORS as exc:
        return _schema_error_message(exc), _schema_error_field(exc)
    return None


def _validate_answer(request: InteractionRequest, answer: Any) -> Any:
    """Validate ``answer`` against the stored format; raise ``_AnswerInvalid``
    (mapped to 400) on mismatch. Returns the validated value."""
    fmt = request.answer_format
    if fmt is AnswerFormat.TEXT:
        if not isinstance(answer, str):
            raise _AnswerInvalid("answer must be a string")
        return answer
    if fmt is AnswerFormat.CONFIRM:
        if not isinstance(answer, bool):
            raise _AnswerInvalid("answer must be a boolean")
        return answer
    if fmt is AnswerFormat.SELECT:
        options = (request.format_payload or {}).get("options", [])
        if answer not in options:
            raise _AnswerInvalid(f"answer must be one of {options}")
        return answer
    if fmt is AnswerFormat.FORM:
        if not isinstance(answer, dict):
            raise _AnswerInvalid("answer must be an object")
        schema = (request.format_payload or {}).get("schema")
        if not isinstance(schema, dict):
            raise _AnswerInvalid("question schema is invalid: missing or non-object schema")
        mismatch = _schema_mismatch(answer, schema)
        if mismatch is not None:
            message, field = mismatch
            raise _AnswerInvalid(message, field=field)
        return answer
    # EXTERNAL is rejected by the answer door before validation runs; any other
    # member reaching here is a server bug, never a client error.
    raise RuntimeError(f"unhandled answer_format: {fmt}")


async def _claim_or_serialization_error(
    store: InteractionStore,
    r: Any,
    response: InteractionResponse,
    group_id: str,
    reply_ttl: int,
    *,
    ticket: str | None = None,
    ticket_ttl: int | None = None,
    continuation_due_ttl: int | None = None,
    continuation_first_attempt_at_ms: int | None = None,
) -> bool | None:
    """Call ``record_answer``, converting a serializer blowup on an untrusted
    answer into a loud-400 signal. A pathological answer (e.g. a deeply-nested JSON
    object that parsed fine but exceeds the serializer's depth) raises when the
    response is serialized — which happens at the top of ``record_answer`` before
    any Redis write, so catching it here leaves no partial state. Returns the
    claim result (``True``/``False``), or ``None`` to signal "serialization
    failed → answer the caller with a 400".

    ``continuation_due_ttl`` / ``continuation_first_attempt_at_ms`` are threaded to
    ``record_answer`` so an async park's durable continuation-due record is enqueued
    ATOMICALLY with the claim; a sync question passes ``None`` and enqueues nothing."""
    try:
        return await store.record_answer(
            r,
            response,
            group_id,
            reply_ttl,
            ticket=ticket,
            ticket_ttl=ticket_ttl,
            continuation_due_ttl=continuation_due_ttl,
            continuation_first_attempt_at_ms=continuation_first_attempt_at_ms,
        )
    except (PydanticSerializationError, RecursionError):
        return None


@operation(
    name="answer_interaction",
    summary="Answer a pending interaction",
    tags=["interactions"],
    destructive=True,
    errors=[BadRequestError, ConflictError, ForbiddenError, NotFoundError, PayloadTooLargeError],
    request_model=InteractionAnswer,
)
async def answer_interaction(interaction_id: str, answer: Any) -> dict:
    """Answer a pending interaction through the authenticated human door.

    Audience gate (after the existence/format/status guards): a question's
    ``audience`` identity OR any unrestricted caller (the operator can always unblock
    a stuck question) may answer; every OTHER restricted caller is a loud ``403``.
    An unaddressed question is answerable by any unrestricted caller and by no
    restricted caller. This gate is sound ONLY because a restricted caller can never
    obtain a question's callback ticket — the ticket is delivered exclusively over the
    configured channel (never on any read/stream frame), so the unauthenticated
    callback door stays the sole ticket-bearing surface and no filtered stream leaks
    it."""
    # OFF gate: with no store configured no interaction can exist — a 404
    # byte-identical to the genuine miss below, so the door is no oracle.
    if not interactions_store_configured():
        raise NotFoundError("Interaction not found")
    settings = interactions_settings()
    store = InteractionStore(settings.key_prefix)
    user_id, restricted = request_identity()

    async with client_ctx(RedisClient, settings.redis) as r:
        state = await store.get_state(r, interaction_id)
        if state is None:
            raise NotFoundError("Interaction not found")
        if state.request.answer_format is AnswerFormat.EXTERNAL:
            raise BadRequestError("external interactions are answered via their callback URL")
        if state.status == "answered":
            raise ConflictError("Interaction already answered")
        # A restricted caller may answer ONLY a question addressed to its identity;
        # an unrestricted caller may answer anything.
        if restricted is not None:
            if state.request.audience is None:
                raise ForbiddenError("restricted identities may answer only interactions addressed to them")
            if state.request.audience != restricted:
                raise ForbiddenError("interaction is addressed to another identity")
        try:
            validated = _validate_answer(state.request, answer)
        except _AnswerInvalid as exc:
            raise BadRequestError(str(exc)) from exc
        response = InteractionResponse(
            interaction_id=interaction_id,
            answer=validated,
            # The authenticated caller; with access control off
            # (ACCESS_CONTROL_ENABLE=false) no identity exists, so the reserved
            # no-auth sentinel is recorded.
            answered_by=user_id or _NO_AUTH_ANSWERED_BY,
            answered_at=datetime.now(UTC),
        )
        # An async park enqueues its durable continuation-due record atomically with
        # the claim; a sync question passes no timing and enqueues nothing.
        due_ttl, due_first_attempt_at_ms = (
            continuation_due_timing(settings) if state.request.mode == "async" else (None, None)
        )
        claimed = await _claim_or_serialization_error(
            store,
            r,
            response,
            state.group_id,
            _reply_ttl(state.request),
            continuation_due_ttl=due_ttl,
            continuation_first_attempt_at_ms=due_first_attempt_at_ms,
        )
        if claimed is None:
            raise BadRequestError("answer payload could not be serialized")
        if not claimed:
            raise ConflictError("Interaction already answered")
        # This door claimed the answer: if the question is an async park, fire its
        # stored continuation ONCE (the shared post-claim seam both answer doors run,
        # so the fire happens exactly once regardless of which door claimed).
        await fire_continuation_after_claim(r, store, state.request, validated)

    return {"interaction_id": interaction_id, "status": "answered"}


def _add_data(request: InteractionRequest) -> dict:
    """The add-frame shape shared by the paged list door and the live stream tail — the
    client shape of one pending question."""
    # A verifier config rides ``format_payload`` server-side; STRIP it from the
    # client frame (the browser never needs the verifier name / secret_env) and
    # in its place emit ``server_verified`` so the UI renders a non-actionable
    # "awaiting a verified server callback" card instead of a dead confirm link.
    format_payload = request.format_payload
    server_verified = False
    if format_payload is not None and "verifier" in format_payload:
        format_payload = {k: v for k, v in format_payload.items() if k != "verifier"}
        server_verified = True

    data = {
        "interaction_id": request.interaction_id,
        "group_id": request.group_id,
        "question": request.question,
        "answer_format": request.answer_format.value,
        "format_payload": format_payload,
        "created_at": request.created_at.isoformat(),
        "timeout_at": request.timeout_at.isoformat(),
        # Rides every add frame (list door + live tail) so the UI can label the
        # answered state of a sensitive question, whose body is never persisted.
        "sensitive": request.sensitive,
    }
    if server_verified:
        data["server_verified"] = True
    if request.channel is not None:
        data["channel"] = request.channel
    # Attribution, additive and absent-when-None (the channel/media idiom). The
    # feed is the channel operator's own authed surface already scoped by the
    # audience read-gate, so ``recipient`` (a delivery address) rides as-is,
    # unmasked. ``recipient``/``origin`` are display/binding-only; ``audience`` IS
    # the isolation axis, emitted here purely for display — the feed is already
    # audience-gated at the read query, so echoing it grants no extra reach.
    if request.recipient is not None:
        data["recipient"] = request.recipient
    if request.origin is not None:
        data["origin"] = request.origin
    if request.audience is not None:
        data["audience"] = request.audience
    # Display-only media rides the add frame as plain JSON dicts when present
    # (absent — no key — when the question has none); ``exclude_none`` keeps a
    # caption-less item lean, and the client treats a missing caption as absent.
    if request.media is not None:
        data["media"] = [item.model_dump(mode="json", exclude_none=True) for item in request.media]
    return data


#: The largest page the pending-list door serves. A larger ``page_size`` is capped to it —
#: valid data, not an error — so one read can never ask for an unbounded slice.
MAX_INTERACTIONS_PAGE_SIZE = 200

#: The highest page number the pending-list door serves. A page past it names a rank the
#: index cannot be sliced at; it is a malformed window and is refused as one.
MAX_INTERACTIONS_PAGE = 1_000_000


class InteractionWindowQuery(BaseModel):
    """The ``?page=``/``?pageSize=`` window the pending-list door takes.

    Spec metadata only — the door parses its query at the HTTP edge."""

    page: int = Field(default=1, ge=1, le=MAX_INTERACTIONS_PAGE, description="1-based page number, pending order.")
    page_size: int = Field(
        default=50,
        ge=1,
        alias="pageSize",
        description=f"Items per page. A larger value is capped to {MAX_INTERACTIONS_PAGE_SIZE}, never refused.",
    )


def _page_bounds(page: int, page_size: int) -> tuple[int, int]:
    """The ``(offset, limit)`` a page/pageSize pair names. Both must be at least 1 and
    ``page`` at most :data:`MAX_INTERACTIONS_PAGE`; a page size above the cap is capped,
    never refused."""
    if page < 1 or page_size < 1:
        raise BadRequestError(f"page and page_size must be >= 1, got page={page} page_size={page_size}")
    if page > MAX_INTERACTIONS_PAGE:
        raise BadRequestError(f"page must be <= {MAX_INTERACTIONS_PAGE}, got page={page}")
    limit = min(page_size, MAX_INTERACTIONS_PAGE_SIZE)
    return (page - 1) * limit, limit


def _next_page(page: int, limit: int, total: int) -> int | None:
    """The next page number, or ``None`` on the last page, read from the filtered total."""
    return page + 1 if page * limit < total else None


@operation(
    name="list_interactions",
    summary="List pending interactions",
    tags=["interactions"],
    errors=[BadRequestError],
    request_model=InteractionWindowQuery,
)
async def list_interactions(page: int = 1, page_size: int = 50) -> dict:
    """The pending questions the inbox shows, one page at a time — the initial-load
    surface a client reads BEFORE applying the live stream
    (``GET /api/interactions/stream``).

    Order is the store's pending order (each group's most-recent question ``created_at``,
    then stream order within a group). A RESTRICTED caller sees ONLY questions addressed
    to its identity — the audience filter runs BEFORE paging so ``total`` is honest; an
    UNRESTRICTED caller sees every pending question. A ``page`` or ``page_size`` below 1,
    or a ``page`` above the served maximum, is a 400; a ``page_size`` above the cap is
    capped, never refused. The pending read reconciles phantom/abandoned questions as it
    goes (phantom-group prune, abandoned past-deadline prune, answered/missing skip). Returns
    ``{"items", "total", "page", "page_size", "next_page", "truncated"}`` — ``items``
    carry the same shape as the stream's add frames, and ``truncated`` is always
    ``false`` (the pending index is the whole set, sliced in memory)."""
    offset, limit = _page_bounds(page, page_size)
    # OFF gate: with no store configured nothing is pending — the honest empty page
    # (the malformed-window 400 above still applies, so the door is no configured oracle).
    if not interactions_store_configured():
        return {"items": [], "total": 0, "page": page, "page_size": limit, "next_page": None, "truncated": False}
    settings = interactions_settings()
    store = InteractionStore(settings.key_prefix)
    _user_id, restricted = request_identity()
    async with client_ctx(RedisClient, settings.redis) as r:
        pending = await store.pending(r)
    if restricted is not None:
        # A restricted caller sees only its own addressed questions; filter BEFORE
        # paging so the total counts what the caller may actually see.
        pending = [req for req in pending if req.audience == restricted]
    total = len(pending)
    window = pending[offset : offset + limit]
    return {
        "items": [_add_data(req) for req in window],
        "total": total,
        "page": page,
        "page_size": limit,
        "next_page": _next_page(page, limit, total),
        "truncated": False,
    }


#: The largest ``?limit=`` the parked-interactions audit door serves. A larger value is
#: clamped to it (valid data, not an error), so one audit can never ask for an unbounded slice.
MAX_PENDING_INTERACTIONS_LIMIT = 1000

#: The parked-interactions audit door's default slice when a caller names no ``?limit=``.
DEFAULT_PENDING_INTERACTIONS_LIMIT = 500


class PendingInteractionsQuery(BaseModel):
    """The ``?limit=`` window the parked-interactions audit door takes.

    Spec metadata only — the door parses its query at the HTTP edge, and the operation
    clamps the value to ``1..``:data:`MAX_PENDING_INTERACTIONS_LIMIT`."""

    limit: int = Field(
        default=DEFAULT_PENDING_INTERACTIONS_LIMIT,
        description=(
            f"Max parked asks to return, soonest-expiry first. Bounded to "
            f"1..{MAX_PENDING_INTERACTIONS_LIMIT}; an out-of-range value is clamped, never refused."
        ),
    )


@operation(
    name="list_pending_interactions",
    summary="List parked (async) interactions awaiting an answer",
    tags=["interactions"],
    errors=[BadRequestError, ForbiddenError],
    request_model=PendingInteractionsQuery,
)
async def list_pending_interactions(limit: int = DEFAULT_PENDING_INTERACTIONS_LIMIT) -> dict:
    """A read-only admin audit of the parked async asks awaiting an answer — the native
    surface a scheduled watchdog flow reads to spot parks nearing (or past) their
    expiry.

    Operator-only: it lists parks addressed to EVERY identity (question preview,
    recipient, audience across identities), so a RESTRICTED caller — isolated to its own
    slice — is a loud ``403``; an unrestricted operator sees the whole set, exactly the
    cross-audience reach the operator inbox (``list_interactions``) already grants. Reads
    the ``pending:expiry`` index WITHOUT mutating it (no claim, no TTL change): purely an
    audit. ``limit`` is clamped to ``1..``:data:`MAX_PENDING_INTERACTIONS_LIMIT` (default
    :data:`DEFAULT_PENDING_INTERACTIONS_LIMIT`). Returns ``{"items", "count"}`` — each
    item carries ``interaction_id``, ``group_id``, ``question`` (truncated), ``channel``,
    ``recipient``, ``audience``, ``thread_id`` (when the park carries one), ``expiry_at``,
    ``created_at``, ``mode``."""
    _user_id, restricted = request_identity()
    if restricted is not None:
        # A cross-audience audit may never surface to a restricted caller (which sees
        # only its own addressed slice); the operator inbox is its authenticated
        # surface. Loud 403, never a silent empty page that would mask the denial.
        raise ForbiddenError("listing parked interactions is restricted to operators")
    limit = min(max(limit, 1), MAX_PENDING_INTERACTIONS_LIMIT)
    # OFF gate: with no store configured no park can exist — the honest empty audit.
    if not interactions_store_configured():
        return {"items": [], "count": 0}
    settings = interactions_settings()
    store = InteractionStore(settings.key_prefix)
    async with client_ctx(RedisClient, settings.redis) as r:
        items = await store.list_pending(r, now=datetime.now(UTC), limit=limit)
    return {"items": items, "count": len(items)}
