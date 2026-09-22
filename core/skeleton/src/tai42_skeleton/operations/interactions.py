"""The human answer operation for the ask interactions surface.

``answer_interaction`` is the authenticated human answer door
(``POST /api/interactions/{interaction_id}/answer``): the value is validated
server-side against the stored question's ``answer_format`` before the blocked
caller is woken; an invalid answer is rejected loudly and the caller stays
blocked. An EXTERNAL question is answered through its callback URL, never here.

The answer is validated through the shared ``check_answer``
(``tai42_app.interactions.check_answer`` /
:mod:`tai42_skeleton.interactions.answer_check`), the one check every door reaches. The
reply-TTL clamp and the serializer-guarded claim live here because the router's still-handler
callback door shares them (the store claim, the reply TTL). The router's HTTP-edge extractor
reads/parses the request body (the byte cap → 413, invalid JSON / missing ``answer`` → 400) and
hands this operation the already-parsed ``answer`` value.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field
from pydantic_core import PydanticSerializationError
from tai42_contract.interactions import (
    AnswerFormat,
    AnswerMismatchError,
    InteractionRequest,
    InteractionResponse,
    InteractionState,
    QuestionFormat,
)
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.access_control.user import request_identity
from tai42_skeleton.interactions.answer_check import check_answer
from tai42_skeleton.interactions.caller_ask import CALLER_ASK_RESOLUTION_REFUSED, is_caller_ask
from tai42_skeleton.interactions.continuation import continuation_due_timing, fire_continuation_after_claim
from tai42_skeleton.interactions.kill import kill_park
from tai42_skeleton.interactions.settings import interactions_settings, interactions_store_configured
from tai42_skeleton.interactions.store import KILL_ACT_ON_PENDING, InteractionStore
from tai42_skeleton.operations import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    PayloadTooLargeError,
    operation,
)
from tai42_skeleton.operations.response_models_group_c import (
    InteractionActionResult,
    InteractionWindow,
    PendingInteractionListing,
)

# The synthetic label recorded when access control is disabled and no caller
# identity exists. A namespaced ``system:`` sentinel (mirroring the
# ``external-callback`` label the callback door records) cannot collide with a
# looked-up user id.
_NO_AUTH_ANSWERED_BY = "system:no-auth"


class InteractionAnswer(BaseModel):
    """An answer to a pending interaction.

    The ``answer`` value is validated at runtime against the interaction's own answer schema.
    """

    answer: Any


def _reply_ttl(request: InteractionRequest) -> int:
    """Short TTL for the reply key ≈ the remaining timeout budget.

    So a late answer to a timed-out question expires instead of resurrecting it.
    """
    remaining = int((request.timeout_at - datetime.now(UTC)).total_seconds())
    return max(1, remaining)


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
    """Call ``record_answer``, converting a serializer blowup on an untrusted answer into a loud-400 signal.

    A pathological answer (e.g. a deeply-nested JSON object that parsed fine but exceeds the
    serializer's depth) raises when the response is serialized — which happens at the top of
    ``record_answer`` before any Redis write, so catching it here leaves no partial state.
    Returns the claim result (``True``/``False``), or ``None`` to signal "serialization
    failed → answer the caller with a 400".

    ``continuation_due_ttl`` / ``continuation_first_attempt_at_ms`` are threaded to
    ``record_answer`` so an async park's durable continuation-due record is enqueued
    ATOMICALLY with the claim; a sync question passes ``None`` and enqueues nothing.
    """
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


async def _load_answerable_state(store: InteractionStore, r: Any, interaction_id: str) -> InteractionState:
    """Read the interaction state and run the pre-audience guards.

    A missing state is a 404, an EXTERNAL question a 400 (answered via its callback URL), an
    already-answered question a 409. Returns the answerable state.
    """
    state = await store.get_state(r, interaction_id)
    if state is None:
        raise NotFoundError("Interaction not found")
    if is_caller_ask(state):
        # A caller ask is addressed to the calling run and resolved only by that run
        # resuming; the human answer door never answers it.
        raise ConflictError(CALLER_ASK_RESOLUTION_REFUSED)
    if state.request.answer_format is AnswerFormat.EXTERNAL:
        raise BadRequestError("external interactions are answered via their callback URL")
    if state.status == "answered":
        raise ConflictError("Interaction already answered")
    return state


def _authorize_answerer(state: InteractionState, restricted: str | None) -> None:
    """The audience gate.

    A restricted caller may answer ONLY a question addressed to its identity (an unaddressed
    question, or one addressed elsewhere, is a loud 403); an unrestricted caller may answer
    anything.
    """
    if restricted is not None:
        if state.request.audience is None:
            raise ForbiddenError("restricted identities may answer only interactions addressed to them")
        if state.request.audience != restricted:
            raise ForbiddenError("interaction is addressed to another identity")


@operation(
    name="answer_interaction",
    summary="Answer a pending interaction",
    tags=["interactions"],
    destructive=True,
    errors=[BadRequestError, ConflictError, ForbiddenError, NotFoundError, PayloadTooLargeError],
    request_model=InteractionAnswer,
    response_model=InteractionActionResult,
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
    it.
    """
    # OFF gate: with no store configured no interaction can exist — a 404
    # byte-identical to the genuine miss below, so the door is no oracle.
    if not interactions_store_configured():
        raise NotFoundError("Interaction not found")
    settings = interactions_settings()
    store = InteractionStore(settings.key_prefix)
    user_id, restricted = request_identity()

    async with client_ctx(RedisClient, settings.redis) as r:
        state = await _load_answerable_state(store, r, interaction_id)
        _authorize_answerer(state, restricted)
        try:
            check_answer(
                QuestionFormat(answer_format=state.request.answer_format, format_payload=state.request.format_payload),
                answer,
            )
        except AnswerMismatchError as exc:
            raise BadRequestError(str(exc)) from exc
        response = InteractionResponse(
            interaction_id=interaction_id,
            answer=answer,
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
        await fire_continuation_after_claim(r, store, state.request, answer)

    return {"interaction_id": interaction_id, "status": "answered"}


@operation(
    name="cancel_interaction",
    summary="Cancel a pending interaction",
    tags=["interactions"],
    destructive=True,
    errors=[ConflictError, ForbiddenError, NotFoundError],
    response_model=InteractionActionResult,
)
async def cancel_interaction(interaction_id: str) -> dict:
    """Cancel a pending interaction — WITHDRAW one specific ask without answering or deleting its thread.

    The mirror of ``answer_interaction`` for the terminal-without-an-answer case: it
    tears the pending question down through the status-gated kill seam (``kill_park``), so
    NO continuation fires — a parked async flow is never resumed — and the removed event
    rides tagged ``reason="cancelled"`` so a live operator surface tells a deliberate
    withdrawal apart from a timeout/expiry removal.

    Status-gated exactly like the answer door: only a PENDING (or parked) question cancels.
    A question already ``answered`` (its state retained) is a loud ``409`` conflict; a
    question whose state is GONE — expired, already cancelled, or never existed (all
    leave no distinguishable tombstone, the same limit the answer door has) — is a ``404``.
    Idempotent at the store seam: the teardown re-run on a withdrawn question is a
    clean no-op, so a re-cancel simply reports the question gone (``404``) rather than
    double-tearing anything down. It is a TEARDOWN door, so unlike the user-facing answer
    door it accepts a CALLER ask (``to="caller"``) as well as a user ask, and unlike the
    answer door it is answer-format-AGNOSTIC: an EXTERNAL ask is a pending ask an operator
    may withdraw, so it is cancellable too.

    Channel-blind by construction: a channel-side pending correlation is NOT proactively
    torn down. A later participant reply forwarded to the callback door finds the state gone and
    the door answers ``404``, which the inbound ladder maps to a fresh bridged turn — the
    identical path a timeout/expiry removal already takes.

    Audience gate identical to ``answer_interaction`` (after the existence/status guards):
    a question's ``audience`` identity OR any unrestricted caller (the operator can always
    withdraw a stuck question) may cancel; every other restricted caller is a loud ``403``.
    """
    # OFF gate: with no store configured no interaction can exist — a 404 byte-identical
    # to the genuine miss below, so the door is no oracle (mirrors the answer door).
    if not interactions_store_configured():
        raise NotFoundError("Interaction not found")
    settings = interactions_settings()
    store = InteractionStore(settings.key_prefix)
    _user_id, restricted = request_identity()

    async with client_ctx(RedisClient, settings.redis) as r:
        state = await store.get_state(r, interaction_id)
        if state is None:
            raise NotFoundError("Interaction not found")
        if state.status == "answered":
            raise ConflictError("Interaction already answered")
        # A restricted caller may cancel ONLY a question addressed to its identity;
        # an unrestricted caller may cancel anything (the answer door's exact gate).
        if restricted is not None:
            if state.request.audience is None:
                raise ForbiddenError("restricted identities may cancel only interactions addressed to them")
            if state.request.audience != restricted:
                raise ForbiddenError("interaction is addressed to another identity")
        # Status-gated teardown routed through the kill seam on the single-park precondition
        # (``KILL_ACT_ON_PENDING``): it fires NO continuation and tags the removed event
        # ``cancelled``. An answer that committed between the read and the atomic claim leaves the
        # park to its own continuation — the kill writes nothing and surfaces as ``"answered"`` here
        # (a loud 409 conflict, no teardown, no FAILED); a state that vanished between the read and
        # the teardown surfaces as ``"gone"`` (a 404, the same terminal answer the answer door gives
        # for a missing interaction).
        result = await kill_park(
            r, store, interaction_id, state.group_id, reason="cancelled", act_on=KILL_ACT_ON_PENDING
        )
        if result == "answered":
            raise ConflictError("Interaction already answered")
        if result == "gone":
            raise NotFoundError("Interaction not found")

    return {"interaction_id": interaction_id, "status": "cancelled"}


def _add_data(request: InteractionRequest) -> dict:
    """The add-frame shape shared by the paged list door and the live stream tail.

    The client shape of one pending question.
    """
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

    Spec metadata only — the door parses its query at the HTTP edge.
    """

    page: int = Field(default=1, ge=1, le=MAX_INTERACTIONS_PAGE, description="1-based page number, pending order.")
    page_size: int = Field(
        default=50,
        ge=1,
        alias="pageSize",
        description=f"Items per page. A larger value is capped to {MAX_INTERACTIONS_PAGE_SIZE}, never refused.",
    )


def _page_bounds(page: int, page_size: int) -> tuple[int, int]:
    """The ``(offset, limit)`` a page/pageSize pair names.

    Both must be at least 1 and ``page`` at most :data:`MAX_INTERACTIONS_PAGE`; a page size
    above the cap is capped, never refused.
    """
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
    response_model=InteractionWindow,
)
async def list_interactions(page: int = 1, page_size: int = 50) -> dict:
    """The pending questions the inbox shows, one page at a time.

    The initial-load surface a client reads BEFORE applying the live stream
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
    ``false`` (the pending index is the whole set, sliced in memory).
    """
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
    clamps the value to ``1..``:data:`MAX_PENDING_INTERACTIONS_LIMIT`.
    """

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
    errors=[BadRequestError],
    request_model=PendingInteractionsQuery,
    response_model=PendingInteractionListing,
)
async def list_pending_interactions(limit: int = DEFAULT_PENDING_INTERACTIONS_LIMIT) -> dict:
    """A read-only admin audit of the parked async asks awaiting an answer.

    The native surface a scheduled watchdog flow reads to spot parks nearing (or past) their
    expiry.

    An UNRESTRICTED operator sees the whole cross-audience set — exactly the reach the
    operator inbox (``list_interactions``) already grants. A RESTRICTED caller sees ONLY
    the parks addressed to its identity, the same audience filter the inbox applies —
    never a 403: this operation is projected, and a projected route must agree with the
    gate for every identity it is projected to (the owned-keys e2e pins that invariant).
    Reads the ``pending:expiry`` index WITHOUT mutating it (no claim, no TTL change):
    purely an audit. ``limit`` is clamped to ``1..``:data:`MAX_PENDING_INTERACTIONS_LIMIT`
    (default :data:`DEFAULT_PENDING_INTERACTIONS_LIMIT`); it bounds the underlying index
    scan, so a restricted caller's filtered slice may hold fewer items. Returns
    ``{"items", "count"}`` — each item carries ``interaction_id``, ``group_id``,
    ``question`` (truncated), ``channel``, ``recipient``, ``audience``, ``thread_id``
    (when the park carries one), ``expiry_at``, ``created_at``, ``mode``.
    """
    _user_id, restricted = request_identity()
    limit = min(max(limit, 1), MAX_PENDING_INTERACTIONS_LIMIT)
    # OFF gate: with no store configured no park can exist — the honest empty audit.
    if not interactions_store_configured():
        return {"items": [], "count": 0}
    settings = interactions_settings()
    store = InteractionStore(settings.key_prefix)
    async with client_ctx(RedisClient, settings.redis) as r:
        items = await store.list_pending(r, now=datetime.now(UTC), limit=limit)
    if restricted is not None:
        # A restricted caller sees only its own addressed parks — the inbox's audience
        # filter, applied AFTER the bounded scan so the operator path stays one read.
        items = [item for item in items if item.get("audience") == restricted]
    return {"items": items, "count": len(items)}
