"""The unauthenticated external-answer callback door (GET page + POST claim) for the interactions surface.

Serves ``/api/interactions/callback/{ticket}``.
The callback ticket is a bearer capability minted by the ``ask`` helper; it is
never deleted, single-use is enforced by the answered-state guard in ``record_answer``,
and a duplicate callback resolves idempotently to 200. The door is NOT audience-gated —
the ticket IS the authorization (an external-answer flow deliberately addresses
outsiders); the ticket is delivered ONLY over the configured channel, so no stream or
read frame ever carries it.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, ValidationError
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from tai42_contract.hooks import TopicVerifierBinding
from tai42_contract.interactions import (
    CALLBACK_LINK_FORMATS,
    AnswerFormat,
    AnswerMismatchError,
    FormReactionClosedError,
    FormReactionHandlerError,
    FormReactionRequestError,
    InteractionResponse,
    InteractionState,
    QuestionFormat,
    Wording,
)
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.conversations.entry_params import OverBoundParamsMode, bound_entry_params
from tai42_kit.net.request_body import RequestBodyTooLargeError, read_bounded_body

from tai42_skeleton.app.http import http_surface
from tai42_skeleton.app.route_registry import DeclaredRouteMetadata
from tai42_skeleton.conversations.cache import get_conversations_manager
from tai42_skeleton.conversations.models import AskReply
from tai42_skeleton.conversations.reply_to import resolve_ask_reply_to
from tai42_skeleton.conversations.settings import conversations_settings

# The answer check is the shared ``check_answer`` (the one check every door reaches). The
# still-handler callback door shares the human answer door's reply-TTL and serializer-guarded
# claim helpers, imported from the operations module.
from tai42_skeleton.interactions.answer_check import check_answer
from tai42_skeleton.interactions.caller_ask import CALLER_ASK_RESOLUTION_REFUSED, is_caller_ask
from tai42_skeleton.interactions.continuation_dispatch import continuation_due_timing, fire_continuation_after_claim
from tai42_skeleton.interactions.reaction import (
    SubmittedCheckRejectedError,
    enforce_submitted_check,
    react,
)
from tai42_skeleton.interactions.settings import InteractionsSettings
from tai42_skeleton.interactions.store import InteractionStore
from tai42_skeleton.middleware.rate_limit import SKELETON_DOOR_BUDGET
from tai42_skeleton.operations.adapter import parse_json_object
from tai42_skeleton.operations.errors import BadRequestError
from tai42_skeleton.operations.interactions import (
    _claim_or_serialization_error,
    _reply_ttl,
)
from tai42_skeleton.webhooks.verify import VERIFY_ERROR, WebhookVerifyFailure, verify_webhook

from .form_render import _FormRenderError, _render_form_page
from .pages import (
    _BASE_HEADERS,
    _FORM_HTML_HEADERS,
    _HTML_HEADERS,
    _render_awaiting_page,
    _render_confirm_page,
    _render_done_page,
)

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


def _callback_gone(settings: InteractionsSettings) -> Response:
    """The 404 every GET answers when it will not serve a page.

    Whether the ticket is unknown, expired, pruned, or bound to a verifier-only question —
    answered or still live — must not be distinguishable from outside, or a ticket holder
    would have a silent state oracle on a capability URL. So the body is ONE constant: the
    deployment's ``INTERACTIONS_CALLBACK_GONE_TEXT``.
    """
    return PlainTextResponse(settings.callback_gone_text, status_code=404, headers=_BASE_HEADERS)


def _callback_fault(settings: InteractionsSettings, interaction_id: str, case: str) -> Response:
    """The loud 500 a GET answers when a stored record cannot be drawn.

    A record that bypassed the ask door's checks is a server bug: the reason is logged with
    the interaction id and the case, and the end user reads the deployment's
    ``INTERACTIONS_CALLBACK_FAULT_TEXT`` as plain text — never the record's own words, which
    ride the broken record, and never the generic JSON error envelope.
    """
    logger.exception("form callback GET: %s for interaction %s", case, interaction_id)
    return PlainTextResponse(settings.callback_fault_text, status_code=500, headers=_BASE_HEADERS)


def _callback_json(payload: dict, status_code: int) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code, headers=dict(_BASE_HEADERS))


async def _callback_post_size_guard(request: Request, settings: InteractionsSettings) -> Response | bytes:
    """Enforce ``callback_max_body_bytes`` on BOTH the raw query string and the actual body bytes.

    The confirm-flow answer rides the URL, so both are capped. Return the 413 response
    when either exceeds the cap, else the bounded body bytes — the one read of the body.
    Runs identically in the OFF gate and the configured POST path so an oversized
    POST answers the SAME 413 whether or not the store is configured — the public
    door leaks no configured-vs-off oracle through the size cap.
    """
    if len(request.url.query.encode()) > settings.callback_max_body_bytes:
        return _callback_json({"error": "payload too large"}, 413)
    try:
        return await read_bounded_body(request, settings.callback_max_body_bytes)
    except RequestBodyTooLargeError:
        return _callback_json({"error": "payload too large"}, 413)


def _params_to_answer(request: Request) -> dict:
    """The query params as the delivered answer, never silent last-wins.

    A single occurrence yields the scalar string (``?a=1`` -> ``{"a": "1"}``), a
    repeated key yields a list (``?tag=a&tag=b`` -> ``{"tag": ["a", "b"]}``).
    """
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
    medium_message_id: str | None = None,
    reply_to_medium_message_id: str | None = None,
) -> JSONResponse:
    """Atomically claim ``answer`` for the ticketed question, then record it as the ask's channel reply.

    A lost race maps to the same idempotent 200 already_answered. ``params`` is the
    OPTIONAL channel enrichment the inbound-answer ladder forwarded alongside the answer
    (the answer-seam counterpart of a bridged turn's entry params); it rides the stored
    :class:`InteractionResponse.params` for the asking run to read beside ``answer``.
    ``None`` keeps the envelope byte-identical to a plain answer. ``medium_message_id`` is
    the id the medium gave the reply, when it gave one, and ``reply_to_medium_message_id`` the
    medium's id of the message the reply quoted, when it quoted one; both ride the stored
    :class:`InteractionResponse`, which holds the ids only, never a quoted text. The asker receives
    ``params`` as is and the quote the platform resolves from the conversation record as
    ``reply_to``, both beside ``answer``.

    This is the one door a reply over the channel is claimed through, so a won claim here is
    written onto the send record that carried the ask (see :func:`_record_ask_reply`).
    """
    response = InteractionResponse(
        interaction_id=interaction_id,
        answer=answer,
        answered_by=_EXTERNAL_ANSWERED_BY,
        answered_at=datetime.now(UTC),
        params=params,
        medium_message_id=medium_message_id,
        reply_to_medium_message_id=reply_to_medium_message_id,
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
    await fire_continuation_after_claim(r, store, state.request, response)
    await _record_ask_reply(interaction_id, answer, medium_message_id, reply_to_medium_message_id)
    return _callback_json({"data": {"interaction_id": interaction_id, "status": "answered"}}, 200)


async def _record_ask_reply(
    interaction_id: str, answer: Any, medium_message_id: str | None, reply_to_medium_message_id: str | None
) -> None:
    """Write a claimed channel reply onto the send record that carried its ask.

    The message the reply quoted, when it quoted one, is resolved from the ask's thread and written
    beside the answer as ``reply_to``. Nothing is looked up without a conversations store. An ask
    sent outside any conversation has no send record (DEBUG); a record removed with its forgotten
    thread is a WARNING. A store fault, reading the quote included, is logged at ERROR and not
    raised: the claim is already won and the answer goes on to its waiter and continuation, which a
    raise here would not undo.
    """
    if conversations_settings().in_memory:
        return
    try:
        reply_to = (
            None
            if reply_to_medium_message_id is None
            else await resolve_ask_reply_to(interaction_id, reply_to_medium_message_id)
        )
        reply = AskReply(
            answer=answer, medium_message_id=medium_message_id, reply_to=reply_to, answered_at=datetime.now(UTC)
        )
        outcome, message_id = await get_conversations_manager().records.record_ask_reply(interaction_id, reply)
    except Exception:
        logger.exception("ask reply for interaction %s not recorded: the conversations store failed", interaction_id)
        return
    if outcome == "unknown":
        logger.debug("ask reply for interaction %s: no send record carries its ask", interaction_id)
    elif outcome == "gone":
        logger.warning(
            "ask reply for interaction %s not recorded: its send record %s is gone (its thread was forgotten)",
            interaction_id,
            message_id,
        )


async def _claim_external(
    r: Any,
    store: InteractionStore,
    settings: InteractionsSettings,
    ticket: str,
    interaction_id: str,
    state: InteractionState,
    answer: Any,
) -> JSONResponse:
    """Validate (only against a declared schema), then atomically claim the answer."""
    try:
        check_answer(
            QuestionFormat(answer_format=AnswerFormat.EXTERNAL, format_payload=state.request.format_payload),
            answer,
        )
    except AnswerMismatchError as exc:
        return _callback_json({"error": str(exc)}, 400)
    return await _record_callback_answer(r, store, settings, ticket, interaction_id, state, answer)


def _verifier_bound(state: InteractionState) -> bool:
    """Whether the external question carries a verifier binding at all.

    Presence of the ``verifier`` key is the only test: a present value that does not validate is
    bound (and refused by :func:`_verify_callback`), never read as an unbound ticket-only question.
    """
    return "verifier" in (state.request.format_payload or {})


async def _verify_callback(request: Request, raw: bytes, state: InteractionState) -> tuple[Response | None, bool]:
    """Run the question's bound verifier over the raw callback body.

    Return ``(deny_response, False)`` on any failure (nothing recorded, ticket
    unconsumed), or ``(None, post_only)`` when the question is unbound or verification
    passes — ``post_only`` tells the caller whether the verifier signs only the body
    (True for a body-signature verifier), so an empty-body POST must not draw its answer
    from the unauthenticated query string.

    The unbound question returns ``(None, False)`` so its ticket-only query-param
    path stays open; only a passing body-signature verifier returns
    ``(None, True)``.

    Fails CLOSED (:func:`verify_webhook`): a signature failure -> 401 (constant message); an
    unknown verifier name / missing secret env / verifier bug -> 500. A stored binding that is
    not a :class:`TopicVerifierBinding` is a 500 too, so a binding of another shape can never
    open an unverified door.
    """
    if not _verifier_bound(state):
        return None, False
    interaction_id = state.request.interaction_id
    try:
        binding = TopicVerifierBinding.model_validate((state.request.format_payload or {})["verifier"])
    except ValidationError:
        binding = None
    if binding is None:
        logger.error("callback verify: malformed verifier binding for interaction %s", interaction_id)
        return _callback_json({"error": VERIFY_ERROR}, 500), False
    try:
        verdict = await verify_webhook(
            binding,
            raw,
            request.headers,
            method=request.method,
            subject=f"interaction {interaction_id}",
            with_replay_defense=False,
        )
    except WebhookVerifyFailure as exc:
        return _callback_json({"error": exc.message}, exc.status), False
    return None, verdict.post_only


class _TypedAnswer(NamedTuple):
    """A channel-delivered typed answer body: the answer and what the channel sent beside it."""

    answer: Any
    params: dict[str, str] | None
    medium_message_id: str | None
    reply_to_medium_message_id: str | None


def _parse_typed_answer(raw: bytes, fmt: AnswerFormat) -> Response | _TypedAnswer:
    """Parse a channel-delivered typed answer body into a :class:`_TypedAnswer`, or a 400.

    A malformed body yields the 400. An empty body is an affirmative confirm tap
    (CONFIRM) or a required-answer 400 (text/select); a JSON object supplies ``answer``,
    OPTIONAL ``params`` (channel enrichment validated against the shared transport
    bounds), OPTIONAL ``medium_message_id`` (a string: the id the medium gave the
    reply) and OPTIONAL ``reply_to_medium_message_id`` (a non-blank string: the medium's id of
    the message the reply quoted). Query params never carry the answer here.
    """
    if not raw:
        if fmt is AnswerFormat.CONFIRM:
            # The GET-confirm page's form POSTs an empty body — an affirmative tap.
            return _TypedAnswer(True, None, None, None)
        # text/select: an answer is required.
        return _callback_json({"error": "body must contain 'answer'"}, 400)
    try:
        parsed = parse_json_object(raw)
    except BadRequestError as exc:
        return _callback_json({"error": exc.message}, 400)
    if "answer" not in parsed:
        return _callback_json({"error": "body must contain 'answer'"}, 400)
    answer_params: dict[str, str] | None = None
    if "params" in parsed:
        raw_params = parsed["params"]
        if not isinstance(raw_params, dict):
            return _callback_json({"error": "params must be a JSON object"}, 400)
        try:
            answer_params = bound_entry_params(
                raw_params, mode=OverBoundParamsMode.REFUSE, door="interactions-callback"
            )
        except ValueError as exc:
            return _callback_json({"error": f"invalid params: {exc}"}, 400)
    medium_message_id = parsed.get("medium_message_id")
    if medium_message_id is not None and not isinstance(medium_message_id, str):
        return _callback_json({"error": "medium_message_id must be a string"}, 400)
    reply_to_medium_message_id = parsed.get("reply_to_medium_message_id")
    if reply_to_medium_message_id is not None and (
        not isinstance(reply_to_medium_message_id, str) or not reply_to_medium_message_id.strip()
    ):
        return _callback_json({"error": "reply_to_medium_message_id must be a non-blank string"}, 400)
    return _TypedAnswer(parsed["answer"], answer_params, medium_message_id, reply_to_medium_message_id)


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
    """The non-EXTERNAL branch: a ticketed channel-delivered question.

    The plugin forwards the human's reply as ``{"answer": <value>}``, with the optional
    ``params``, ``medium_message_id`` and ``reply_to_medium_message_id`` beside it. The value is
    validated against the STORED format (the authed door's exact rules) and the TYPED
    value recorded.
    """
    parsed = _parse_typed_answer(raw, state.request.answer_format)
    if isinstance(parsed, Response):
        return parsed
    value = parsed.answer
    try:
        # The returned value is the answer the consumer receives — identical but for a
        # FORM, whose conditionally-hidden fields are dropped at the one facet, so the
        # channel-forwarded answer is recorded with the same effective shape the
        # authenticated door records.
        value = check_answer(
            QuestionFormat(answer_format=state.request.answer_format, format_payload=state.request.format_payload),
            value,
        )
    except AnswerMismatchError as exc:
        # The failing field's dotted path rides as an optional ``field`` key so a
        # channel can pin the error on the right control; absent when unlocated.
        # ``retry_in_place`` is the door's policy signal to a correlated channel:
        # every current validation rejection is re-answerable in place (the live
        # ask stands and the participant can answer again), so it is always True here.
        body: dict[str, Any] = {"error": str(exc), "retry_in_place": True}
        if exc.field is not None:
            body["field"] = exc.field
        return _callback_json(body, 400)
    # The consumer's submit check runs HERE, server-side, before the record: a reacting form that
    # declares ``submitted`` runs its handler through the one reaction chokepoint. Per-field errors
    # refuse the answer re-answerable in place (the ask stands); a handler raise/timeout is a loud
    # 502; a form no longer open a 409. A channel that ran the check first never lets the server skip it.
    try:
        await enforce_submitted_check(interaction_id, state.request.answer_format, state.request.format_payload, value)
    except SubmittedCheckRejectedError as exc:
        return _callback_json({"error": exc.reason, "errors": exc.errors, "retry_in_place": True}, 400)
    except FormReactionClosedError as exc:
        return _callback_json({"error": exc.reason}, 409)
    except FormReactionHandlerError as exc:
        return _callback_json({"error": exc.reason}, 502)
    return await _record_callback_answer(
        r,
        store,
        settings,
        ticket,
        interaction_id,
        state,
        value,
        params=parsed.params,
        medium_message_id=parsed.medium_message_id,
        reply_to_medium_message_id=parsed.reply_to_medium_message_id,
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
    """The EXTERNAL branch: verbatim payload semantics.

    Dispatch on the body FIRST — the empty-body branch first (``json.loads("")``
    raises). Body wins: query params alongside a JSON-object body are ignored as routing
    metadata (webhook providers decorate the URL with their own params while POSTing the
    event body). A body-signature verifier signs only the raw body, so a replayed
    signature over an empty body must never let ``?approved=true`` inject an answer.
    """
    if not raw:
        if post_only:
            return _callback_json({"error": _POST_ONLY_EMPTY_BODY_DENY}, 400)
        answer = _params_to_answer(request)
    else:
        try:
            answer = parse_json_object(raw)
        except BadRequestError as exc:
            return _callback_json({"error": exc.message}, 400)
    return await _claim_external(r, store, settings, ticket, interaction_id, state, answer)


async def _callback_post(
    request: Request, r: Any, store: InteractionStore, settings: InteractionsSettings, raw: bytes
) -> Response:
    """The answer claim over ``raw``, the body bytes the caller's size guard already read."""
    ticket = request.path_params["ticket"]
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
    if is_caller_ask(state):
        # A caller ask is addressed to the calling run and resolved only by that run
        # resuming; the public answer door refuses it loudly (both the typed and the
        # external claim branch flow through here). A caller ask carries no channel
        # delivery and no ticket, so this is defence-in-depth on the ticket resolution.
        return _callback_json({"error": CALLER_ASK_RESOLUTION_REFUSED}, 409)
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


def _page_wording(state: InteractionState) -> Wording | None:
    # The parked ask's per-send ``wording`` catalogue off the stored ``format_payload``, parsed into
    # the model once so each page draws the caller's heading/sentence/button. Reading the record
    # already validated it against ``Wording`` (a record that fails is answered by
    # :func:`_callback_fault` before any page is chosen).
    raw = (state.request.format_payload or {}).get("wording")
    return Wording.model_validate(raw) if raw is not None else None


async def _callback_get(request: Request, r: Any, store: InteractionStore, settings: InteractionsSettings) -> Response:
    ticket = request.path_params["ticket"]
    interaction_id = await store.resolve_ticket(r, ticket)
    if interaction_id is None:
        return _callback_gone(settings)
    try:
        state = await store.get_state(r, interaction_id)
    except ValidationError:
        # A stored record that fails its model (its ``wording`` among it) cannot be drawn.
        return _callback_fault(settings, interaction_id, "stored record fails its model")
    if state is None:
        # State pruned while the ticket lives out its TTL tail — plain 404, never
        # a confirm page for a dead interaction.
        return _callback_gone(settings)
    if _verifier_bound(state):
        # A verifier-bound question is server-to-server ONLY: the browser confirm
        # form posts an EMPTY body (answer in query params) and can never carry a
        # provider signature, so no page is ever served here — not a confirm page
        # while the question is live, and not a done page after it is answered.
        # Both states answer the same constant 404 every other non-page branch
        # sends: a caller must not learn from the response whether the ticket is
        # live, answered, or dead. The ticket for a bound question is never
        # exported to a human, so no legitimate reader loses anything.
        return _callback_gone(settings)
    if state.status == "answered":
        return HTMLResponse(_render_done_page(_page_wording(state)), headers=_HTML_HEADERS)
    if state.request.answer_format is AnswerFormat.FORM:
        # A channel-delivered form needs a real answer surface: render its schema
        # server-side. An unrenderable schema on a form record is a server bug —
        # answer a loud 500 with a logged reason, never a blank/half-rendered page.
        try:
            page = _render_form_page(state.request.format_payload)
        except _FormRenderError:
            return _callback_fault(settings, interaction_id, "unrenderable schema")
        return HTMLResponse(page, headers=_FORM_HTML_HEADERS)
    if state.request.answer_format in CALLBACK_LINK_FORMATS:
        # Only these formats map the confirm form's empty-body POST to an
        # answer (confirm -> True, external -> the query-param payload). The page reads the caller's
        # ``wording.answer_page.confirm`` copy and ``wording.answer.confirm`` button when set.
        return HTMLResponse(_render_confirm_page(_page_wording(state)), headers=_HTML_HEADERS)
    # A ticketed text/select question needs a VALUE answer, which the channel
    # plugin forwards as a POST body — never a bare confirm tap, so no page
    # with an action that would be rejected.
    return HTMLResponse(_render_awaiting_page(_page_wording(state)), headers=_HTML_HEADERS)


def _parse_react_body(raw: bytes) -> Response | tuple[Any, Any]:
    """Parse a mid-form reaction body into ``(event, values)``, or a 400 ``Response``.

    The reacting channel forwards ``{"event": {...}, "values": {...}}`` — the triggering
    event and the values filled so far. A malformed or incomplete body is a 400.
    """
    if not raw:
        return _callback_json({"error": "body must contain 'event' and 'values'"}, 400)
    try:
        parsed = parse_json_object(raw)
    except BadRequestError as exc:
        return _callback_json({"error": exc.message}, 400)
    if "event" not in parsed or "values" not in parsed:
        return _callback_json({"error": "body must contain 'event' and 'values'"}, 400)
    return parsed["event"], parsed["values"]


async def _callback_react(
    request: Request, r: Any, store: InteractionStore, settings: InteractionsSettings, raw: bytes
) -> Response:
    """The mid-form reaction claim for a ticketed channel-delivered reacting form.

    A SIBLING of the answer callback door under the SAME ticket (the ticket is the
    capability, exactly as for the answer claim) and the SAME size caps: resolve the ticket,
    parse the event + partial values, and run the ONE ``react`` chokepoint. STATELESS — no
    answer is recorded and the durable record is untouched. A handler failure/deadline is a
    loud 502 (never a stale/silent value). ``raw`` is the body the caller's size guard read.
    """
    ticket = request.path_params["ticket"]
    interaction_id = await store.resolve_ticket(r, ticket)
    if interaction_id is None:
        return _callback_json({"error": "not found"}, 404)
    parsed = _parse_react_body(raw)
    if isinstance(parsed, Response):
        return parsed
    event, values = parsed
    try:
        update = await react(interaction_id, event, values)
    except FormReactionClosedError as exc:
        return _callback_json({"error": exc.reason}, 409)
    except FormReactionRequestError as exc:
        return _callback_json({"error": exc.reason}, 400)
    except FormReactionHandlerError as exc:
        logger.exception("reaction callback: handler failed for interaction %s", interaction_id)
        return _callback_json({"error": exc.reason}, 502)
    return _callback_json({"data": {"update": update}}, 200)


class InteractionReactionAck(BaseModel):
    """The reaction door's JSON body: the validated form ``update`` a reaction returned."""

    update: dict[str, Any]


@http_surface().custom_route(
    "/api/interactions/callback/{ticket}/react",
    methods=["POST"],
    summary="External interaction mid-form reaction door",
    tags=["interactions"],
    response_model=InteractionReactionAck,
    authed=False,
    declared=DeclaredRouteMetadata(
        reload_gated=False,
        reads_body=False,
        error_statuses=(400, 404, 409, 413, 502),
        success_status=200,
        rate_limit_budget=SKELETON_DOOR_BUDGET,
    ),
)
async def callback_react(request: Request) -> Response:
    """Serve the unauthenticated mid-form reaction door (POST only), under the ticket capability."""
    settings = _pkg.interactions_settings()
    store = InteractionStore(settings.key_prefix)
    # OFF gate — same uniform-404 (after the shared size guard) the answer callback door gives,
    # so an unconfigured store is no oracle on this public door.
    if not _pkg.interactions_store_configured():
        oversized = await _callback_post_size_guard(request, settings)
        if isinstance(oversized, Response):
            return oversized
        return _callback_json({"error": "not found"}, 404)
    async with _pkg.client_ctx(RedisClient, settings.redis) as r:
        # Size caps (query string + actual body bytes) come first — the SAME guard the OFF
        # gate runs, so an oversized POST answers 413 in both states.
        guarded = await _callback_post_size_guard(request, settings)
        if isinstance(guarded, Response):
            return guarded
        return await _callback_react(request, r, store, settings, guarded)


class InteractionCallbackAck(BaseModel):
    """The POST callback door's JSON body.

    ``answered`` (with the ``interaction_id`` just recorded) or the idempotent
    ``already_answered`` (no id — someone else's answer already landed). The GET method
    of this route serves an HTML page, not this body.
    """

    interaction_id: str | None = None
    status: Literal["answered", "already_answered"]


@http_surface().custom_route(
    "/api/interactions/callback/{ticket}",
    methods=["GET", "POST"],
    summary="External interaction callback door",
    tags=["interactions"],
    response_model=InteractionCallbackAck,
    success_media_types={"GET": ("text/html",)},
    authed=False,
    declared=DeclaredRouteMetadata(
        reload_gated=False,
        reads_body=False,
        error_statuses=(400, 401, 404, 409, 413, 500),
        success_status=200,
        rate_limit_budget=SKELETON_DOOR_BUDGET,
    ),
)
async def callback(request: Request) -> Response:
    """Serve the external interaction callback door (GET page, POST answer claim)."""
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
            return _callback_gone(settings)
        # A POST still runs the SAME size guard the configured path runs BEFORE the
        # uniform 404, so an oversized POST answers 413 identically in both states —
        # the size cap is not a configured-vs-off oracle on this public door.
        oversized = await _callback_post_size_guard(request, settings)
        if isinstance(oversized, Response):
            return oversized
        return _callback_json({"error": "not found"}, 404)

    async with _pkg.client_ctx(RedisClient, settings.redis) as r:
        if request.method == "GET":
            return await _callback_get(request, r, store, settings)
        # Size caps (query string + actual body bytes) come first — the SAME guard the OFF
        # gate runs, so an oversized POST answers 413 in both states.
        guarded = await _callback_post_size_guard(request, settings)
        if isinstance(guarded, Response):
            return guarded
        return await _callback_post(request, r, store, settings, guarded)
