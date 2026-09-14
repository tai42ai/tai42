"""The unauthenticated external-answer callback door (GET page + POST claim) for the
interactions surface — ``/api/interactions/callback/{ticket}``.

The callback ticket is a bearer capability minted by the ``ask_user`` helper; it is
never deleted, single-use is enforced by the answered-state guard in ``record_answer``,
and a duplicate callback resolves idempotently to 200. The door is NOT audience-gated —
the ticket IS the authorization (an external-answer flow deliberately addresses
outsiders); the ticket is delivered ONLY over the configured channel, so no stream or
read frame ever carries it.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from tai42_contract.app import tai42_app
from tai42_contract.conversations import validate_entry_params
from tai42_contract.interactions import (
    AnswerFormat,
    InteractionResponse,
    InteractionState,
)
from tai42_contract.webhooks import WebhookVerificationError
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.net.request_body import PayloadTooLarge, read_bounded_body

from tai42_skeleton.app.http import http_surface
from tai42_skeleton.app.route_registry import DeclaredRouteMetadata
from tai42_skeleton.interactions.continuation import continuation_due_timing, fire_continuation_after_claim
from tai42_skeleton.interactions.settings import InteractionsSettings
from tai42_skeleton.interactions.store import InteractionStore

# The human answer door is an operation in ``tai42_skeleton.operations.interactions``;
# the still-handler callback door shares its answer-validation, reply-TTL, and
# serializer-guarded claim helpers, imported from that module.
from tai42_skeleton.operations.interactions import (
    _AnswerInvalid,
    _claim_or_serialization_error,
    _reply_ttl,
    _schema_mismatch,
    _validate_answer,
)

from .form_render import _FormRenderError, _render_form_page
from .pages import _BASE_HEADERS, _CONFIRM_PAGE, _DONE_PAGE, _FORM_HTML_HEADERS, _HTML_HEADERS, _REPLY_PAGE
from .parse import JSON_PARSE_ERRORS

logger = logging.getLogger(__name__)

# This submodule's OWN package object, captured at import time from ``sys.modules`` — NOT
# ``from tai42_skeleton.routers import interactions``, whose parent-attribute read is stale
# mid-reload. The seam symbols (``client_ctx``/``interactions_settings``/
# ``interactions_store_configured``) are read through it at call time.
_pkg = sys.modules["tai42_skeleton.routers.interactions"]

# The free-form label recorded on an answer delivered through the callback door.
_EXTERNAL_ANSWERED_BY = "external-callback"

# A ``post_only`` (body-signature) verifier authenticates the raw body only, so an
# empty-body POST carries no signed answer — the query string is unauthenticated
# and must never supply the answer. The deny is a constant, request-independent
# message: the answer must ride the signed body.
_POST_ONLY_EMPTY_BODY_DENY = "verified callback requires a signed request body"

# Every GET that will not serve a page answers ONE constant body: whether the
# ticket is unknown, expired, pruned, or bound to a verifier-only question —
# answered or still live — must not be distinguishable from outside. A ticket
# holder would otherwise have a silent state oracle on a capability URL.
_CALLBACK_GET_NOT_FOUND = "Not Found"


def _callback_json(payload: dict, status_code: int) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code, headers=dict(_BASE_HEADERS))


async def _callback_post_size_guard(request: Request, settings: InteractionsSettings) -> Response | None:
    """Enforce ``callback_max_body_bytes`` on BOTH the raw query string (the
    confirm-flow answer rides the URL) and the actual body bytes. Return the 413
    response when either exceeds the cap, or ``None`` when both are within it.

    Runs identically in the OFF gate and the configured POST path so an oversized
    POST answers the SAME 413 whether or not the store is configured — the public
    door leaks no configured-vs-off oracle through the size cap."""
    if len(request.url.query.encode()) > settings.callback_max_body_bytes:
        return _callback_json({"error": "payload too large"}, 413)
    try:
        body = await read_bounded_body(request, settings.callback_max_body_bytes)
    except PayloadTooLarge:
        return _callback_json({"error": "payload too large"}, 413)
    # Cache the read bytes on the request (Starlette's own ``_body`` slot) so
    # ``_callback_post`` replays them instead of re-consuming the drained stream.
    request._body = body
    return None


def _params_to_answer(request: Request) -> dict:
    """The query params as the delivered answer: a single occurrence yields the
    scalar string (``?a=1`` -> ``{"a": "1"}``), a repeated key yields a list
    (``?tag=a&tag=b`` -> ``{"tag": ["a", "b"]}``) — never silent last-wins."""
    result: dict[str, Any] = {}
    for key, value in request.query_params.multi_items():
        if key in result:
            existing = result[key]
            if isinstance(existing, list):
                existing.append(value)
            else:
                result[key] = [existing, value]
        else:
            result[key] = value
    return result


async def _record_callback_answer(
    r: Any,
    store: InteractionStore,
    settings: InteractionsSettings,
    ticket: str,
    interaction_id: str,
    state: InteractionState,
    answer: Any,
    params: dict[str, str] | None = None,
) -> JSONResponse:
    """Atomically claim ``answer`` for the ticketed question. A lost race maps
    to the same idempotent 200 already_answered. ``params`` is the OPTIONAL channel enrichment the
    inbound-answer ladder forwarded alongside the answer (the answer-seam counterpart of a bridged
    turn's entry params); it rides the stored :class:`InteractionResponse.params` for the asking
    flow to read beside ``answer``. ``None`` keeps the envelope byte-identical to a plain answer."""
    response = InteractionResponse(
        interaction_id=interaction_id,
        answer=answer,
        answered_by=_EXTERNAL_ANSWERED_BY,
        answered_at=datetime.now(UTC),
        params=params,
    )
    # An async park enqueues its durable continuation-due record atomically with the
    # claim; a sync question passes no timing and enqueues nothing.
    due_ttl, due_first_attempt_at_ms = (
        continuation_due_timing(settings) if state.request.mode == "async" else (None, None)
    )
    claimed = await _claim_or_serialization_error(
        store,
        r,
        response,
        state.group_id,
        _reply_ttl(state.request),
        ticket=ticket,
        ticket_ttl=settings.idle_ttl_seconds,
        continuation_due_ttl=due_ttl,
        continuation_first_attempt_at_ms=due_first_attempt_at_ms,
    )
    if claimed is None:
        return _callback_json({"error": "answer payload could not be serialized"}, 400)
    if not claimed:
        return _callback_json({"data": {"status": "already_answered"}}, 200)
    # This door claimed the answer: fire an async park's stored continuation ONCE
    # (the shared post-claim seam both answer doors run).
    await fire_continuation_after_claim(r, store, state.request, answer)
    return _callback_json({"data": {"interaction_id": interaction_id, "status": "answered"}}, 200)


async def _claim_external(
    r: Any,
    store: InteractionStore,
    settings: InteractionsSettings,
    ticket: str,
    interaction_id: str,
    state: InteractionState,
    answer: Any,
) -> JSONResponse:
    """Validate (if a schema was declared), then atomically claim the answer."""
    schema = (state.request.format_payload or {}).get("schema")
    if schema is not None:
        mismatch = _schema_mismatch(answer, schema)
        if mismatch is not None:
            return _callback_json({"error": mismatch[0]}, 400)
    return await _record_callback_answer(r, store, settings, ticket, interaction_id, state, answer)


def _callback_verifier(state: InteractionState) -> dict | None:
    """The verifier binding stashed in the external ``format_payload`` (``{"name",
    "config"}``), or ``None`` for an unbound ticket-only external question."""
    binding = (state.request.format_payload or {}).get("verifier")
    return binding if isinstance(binding, dict) else None


async def _verify_callback(request: Request, raw: bytes, state: InteractionState) -> tuple[Response | None, bool]:
    """Run the question's bound verifier over the raw callback body. Return
    ``(deny_response, False)`` on any failure (nothing recorded, ticket
    unconsumed), or ``(None, post_only)`` when the question is unbound or
    verification passes — ``post_only`` tells the caller whether the verifier
    signs only the body (True for a body-signature verifier), so an empty-body
    POST must not draw its answer from the unauthenticated query string.

    The unbound question returns ``(None, False)`` so its ticket-only query-param
    path stays open; only a passing body-signature verifier returns
    ``(None, True)``.

    Fails CLOSED: a signature failure -> 401 (constant message); an unknown
    verifier name / missing secret env / verifier bug -> 500."""
    binding = _callback_verifier(state)
    if binding is None:
        return None, False
    name = binding.get("name")
    config = binding.get("config") or {}
    interaction_id = state.request.interaction_id
    if not isinstance(name, str):
        logger.error("callback verify: malformed verifier binding (no name) for interaction %s", interaction_id)
        return _callback_json({"error": "webhook verification error"}, 500), False
    try:
        verifier = tai42_app.webhook_verifiers.get(name)
    except Exception:
        logger.error("callback verify: no registered verifier %r for interaction %s", name, interaction_id)
        return _callback_json({"error": "webhook verification error"}, 500), False
    post_only = bool(getattr(verifier, "post_only", False))
    try:
        await verifier.verify(raw, request.headers, config)
    except WebhookVerificationError as exc:
        # Log the reason (never the raw body verbatim); return a constant message.
        logger.warning("callback verification failed for interaction %s: %s", interaction_id, exc)
        return _callback_json({"error": "webhook verification failed"}, 401), False
    except Exception:
        logger.error("callback verifier error for interaction %s", interaction_id, exc_info=True)
        return _callback_json({"error": "webhook verification error"}, 500), False
    return None, post_only


def _parse_typed_answer(raw: bytes, fmt: AnswerFormat) -> Response | tuple[Any, dict[str, str] | None]:
    """Parse a channel-delivered typed answer body into ``(value, params)``, or a 400
    ``Response`` on a malformed body. An empty body is an affirmative confirm tap (CONFIRM)
    or a required-answer 400 (text/select); a JSON object supplies ``answer`` and OPTIONAL
    ``params`` (channel enrichment validated against the shared transport bounds). Query
    params never carry the answer here."""
    if not raw:
        if fmt is AnswerFormat.CONFIRM:
            # The GET-confirm page's form POSTs an empty body — an affirmative tap.
            return True, None
        # text/select: an answer is required.
        return _callback_json({"error": "body must contain 'answer'"}, 400)
    try:
        parsed = json.loads(raw)
    except JSON_PARSE_ERRORS:
        return _callback_json({"error": "body must be a JSON object"}, 400)
    if not isinstance(parsed, dict):
        return _callback_json({"error": "body must be a JSON object"}, 400)
    if "answer" not in parsed:
        return _callback_json({"error": "body must contain 'answer'"}, 400)
    answer_params: dict[str, str] | None = None
    if "params" in parsed:
        raw_params = parsed["params"]
        if not isinstance(raw_params, dict):
            return _callback_json({"error": "params must be a JSON object"}, 400)
        try:
            answer_params = validate_entry_params(raw_params)
        except ValueError as exc:
            return _callback_json({"error": f"invalid params: {exc}"}, 400)
    return parsed["answer"], answer_params


async def _claim_channel_typed(
    request: Request,
    r: Any,
    store: InteractionStore,
    settings: InteractionsSettings,
    ticket: str,
    interaction_id: str,
    state: InteractionState,
    raw: bytes,
) -> Response:
    """The non-EXTERNAL branch: a ticketed channel-delivered question. The plugin
    forwards the human's reply as ``{"answer": <value>}``. The value is validated against
    the STORED format (the authed door's exact rules) and the TYPED value recorded."""
    parsed = _parse_typed_answer(raw, state.request.answer_format)
    if isinstance(parsed, Response):
        return parsed
    value, answer_params = parsed
    try:
        validated = _validate_answer(state.request, value)
    except _AnswerInvalid as exc:
        # The failing field's dotted path rides as an optional ``field`` key so a
        # channel can pin the error on the right control; absent when unlocated.
        # ``retry_in_place`` is the door's policy signal to a correlated channel:
        # every current validation rejection is re-answerable in place (the live
        # ask stands and the participant can answer again), so it is always True here.
        body: dict[str, Any] = {"error": str(exc), "retry_in_place": True}
        if exc.field is not None:
            body["field"] = exc.field
        return _callback_json(body, 400)
    return await _record_callback_answer(
        r, store, settings, ticket, interaction_id, state, validated, params=answer_params
    )


async def _claim_verbatim_external(
    request: Request,
    r: Any,
    store: InteractionStore,
    settings: InteractionsSettings,
    ticket: str,
    interaction_id: str,
    state: InteractionState,
    raw: bytes,
    post_only: bool,
) -> Response:
    """The EXTERNAL branch: verbatim payload semantics. Dispatch on the body FIRST —
    the empty-body branch first (``json.loads("")`` raises). Body wins: query params
    alongside a JSON-object body are ignored as routing metadata (webhook providers
    decorate the URL with their own params while POSTing the event body). A
    body-signature verifier signs only the raw body, so a replayed signature over an
    empty body must never let ``?approved=true`` inject an answer."""
    if not raw:
        if post_only:
            return _callback_json({"error": _POST_ONLY_EMPTY_BODY_DENY}, 400)
        answer = _params_to_answer(request)
    else:
        try:
            parsed = json.loads(raw)
        except JSON_PARSE_ERRORS:
            return _callback_json({"error": "body must be a JSON object"}, 400)
        if not isinstance(parsed, dict):
            return _callback_json({"error": "body must be a JSON object"}, 400)
        answer = parsed
    return await _claim_external(r, store, settings, ticket, interaction_id, state, answer)


async def _callback_post(request: Request, r: Any, store: InteractionStore, settings: InteractionsSettings) -> Response:
    ticket = request.path_params["ticket"]

    # Size caps (query string + actual body bytes) come first — the SAME guard the OFF
    # gate runs, so an oversized POST answers 413 in both states. On success the body is
    # cached, so the re-read below replays it.
    oversized = await _callback_post_size_guard(request, settings)
    if oversized is not None:
        return oversized
    # The size guard cached the bounded bytes on the request, so this replays them
    # (never a re-consumed stream) and stays within the same cap.
    raw = await read_bounded_body(request, settings.callback_max_body_bytes)

    interaction_id = await store.resolve_ticket(r, ticket)
    if interaction_id is None:
        # Uniform 404: same status and body whether the ticket never existed or
        # expired — no timing or body distinction.
        return _callback_json({"error": "not found"}, 404)
    state = await store.get_state(r, interaction_id)
    if state is None:
        # A cancel/timeout prune deletes the state while the ticket lives out its
        # TTL — the same uniform 404, never a None dereference.
        return _callback_json({"error": "not found"}, 404)
    # Verify the signed server-to-server answer over the RAW body BEFORE the
    # answered-state check, before parsing, and before recording. A bound door
    # authenticates before it reports anything about the ticket's state, so a
    # caller without the secret cannot tell a live bound question from an
    # answered one — both deny identically. Failure denies without consuming the
    # ticket or recording the answer — the idempotency window is untouched, so a
    # legitimate retry still works.
    denied, post_only = await _verify_callback(request, raw, state)
    if denied is not None:
        return denied

    if state.status == "answered":
        # Idempotent duplicate handling (incl. provider retries after success);
        # the ticket is never deleted and its TTL was refreshed on claim. A bound
        # question reaches this only after passing verification above.
        return _callback_json({"data": {"status": "already_answered"}}, 200)

    if state.request.answer_format is not AnswerFormat.EXTERNAL:
        return await _claim_channel_typed(request, r, store, settings, ticket, interaction_id, state, raw)
    return await _claim_verbatim_external(request, r, store, settings, ticket, interaction_id, state, raw, post_only)


async def _callback_get(request: Request, r: Any, store: InteractionStore) -> Response:
    ticket = request.path_params["ticket"]
    interaction_id = await store.resolve_ticket(r, ticket)
    if interaction_id is None:
        return PlainTextResponse(_CALLBACK_GET_NOT_FOUND, status_code=404, headers=_BASE_HEADERS)
    state = await store.get_state(r, interaction_id)
    if state is None:
        # State pruned while the ticket lives out its TTL tail — plain 404, never
        # a confirm page for a dead interaction.
        return PlainTextResponse(_CALLBACK_GET_NOT_FOUND, status_code=404, headers=_BASE_HEADERS)
    if _callback_verifier(state) is not None:
        # A verifier-bound question is server-to-server ONLY: the browser confirm
        # form posts an EMPTY body (answer in query params) and can never carry a
        # provider signature, so no page is ever served here — not a confirm page
        # while the question is live, and not a done page after it is answered.
        # Both states answer the same constant 404 every other non-page branch
        # sends: a caller must not learn from the response whether the ticket is
        # live, answered, or dead. The ticket for a bound question is never
        # exported to a human, so no legitimate reader loses anything.
        return PlainTextResponse(_CALLBACK_GET_NOT_FOUND, status_code=404, headers=_BASE_HEADERS)
    if state.status == "answered":
        return HTMLResponse(_DONE_PAGE, headers=_HTML_HEADERS)
    if state.request.answer_format is AnswerFormat.FORM:
        # A channel-delivered form needs a real answer surface: render its schema
        # server-side. An unrenderable schema on a form record is a server bug —
        # answer a loud 500 with a logged reason, never a blank/half-rendered page.
        try:
            page = _render_form_page(state.request.format_payload)
        except _FormRenderError:
            logger.exception("form callback GET: unrenderable schema for interaction %s", interaction_id)
            return PlainTextResponse("Internal Server Error", status_code=500, headers=_BASE_HEADERS)
        return HTMLResponse(page, headers=_FORM_HTML_HEADERS)
    if state.request.answer_format in (AnswerFormat.CONFIRM, AnswerFormat.EXTERNAL):
        # Only these formats map the confirm form's empty-body POST to an
        # answer (confirm -> True, external -> the query-param payload).
        return HTMLResponse(_CONFIRM_PAGE, headers=_HTML_HEADERS)
    # A ticketed text/select question needs a VALUE answer, which the channel
    # plugin forwards as a POST body — never a bare confirm tap, so no page
    # with an action that would be rejected.
    return HTMLResponse(_REPLY_PAGE, headers=_HTML_HEADERS)


class InteractionCallbackAck(BaseModel):
    """The POST callback door's JSON body: ``answered`` (with the ``interaction_id``
    just recorded) or the idempotent ``already_answered`` (no id — someone else's
    answer already landed). The GET method of this route serves an HTML page, not
    this body."""

    interaction_id: str | None = None
    status: Literal["answered", "already_answered"]


@http_surface().custom_route(
    "/api/interactions/callback/{ticket}",
    methods=["GET", "POST"],
    summary="External interaction callback door",
    tags=["interactions"],
    response_model=InteractionCallbackAck,
    authed=False,
    declared=DeclaredRouteMetadata(
        reload_gated=False,
        reads_body=False,
        error_statuses=(400, 401, 404, 413, 500),
        success_status=200,
    ),
)
async def callback(request: Request) -> Response:
    # Rate limiting for this public door lives in the app-level
    # ``RateLimitMiddleware``, registered at app construction so it is always on
    # (tunable/disable via ``TAI_RATE_LIMIT_*``); it runs ahead of this route for
    # the interactions-callback door family.
    settings = _pkg.interactions_settings()
    store = InteractionStore(settings.key_prefix)

    # OFF gate — BEFORE client_ctx. This is an UNAUTHENTICATED door, so an
    # unconfigured store must answer the door's OWN uniform 404 (indistinguishable
    # from an unknown/expired ticket), never a discriminable 501 that would oracle
    # the store's absence to an outsider.
    if not _pkg.interactions_store_configured():
        if request.method == "GET":
            return PlainTextResponse(_CALLBACK_GET_NOT_FOUND, status_code=404, headers=_BASE_HEADERS)
        # A POST still runs the SAME size guard the configured path runs BEFORE the
        # uniform 404, so an oversized POST answers 413 identically in both states —
        # the size cap is not a configured-vs-off oracle on this public door.
        oversized = await _callback_post_size_guard(request, settings)
        if oversized is not None:
            return oversized
        return _callback_json({"error": "not found"}, 404)

    async with _pkg.client_ctx(RedisClient, settings.redis) as r:
        if request.method == "GET":
            return await _callback_get(request, r, store)
        return await _callback_post(request, r, store, settings)
