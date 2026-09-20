"""The answer door: forward a pending question's answer to its interactions callback.

Applies the callback's reply as the status policy.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from tai42_contract.app import tai42_app
from tai42_contract.conversations import validate_entry_params
from tai42_kit.clients.impl.http import HttpxClient

from tai42_channel_web.routes.dtos import AnswerResultResponse, _answer_refusal
from tai42_channel_web.routes.envelope import _error, _json_body, _ok
from tai42_channel_web.routes.session_access import (
    _CROSS_ORIGIN,
    _CROSS_ORIGIN_CODE,
    _SESSION_MISSING,
    _SESSION_MISSING_CODE,
    _serves,
    _session,
    _store_off,
)
from tai42_channel_web.session import is_cross_origin
from tai42_channel_web.settings import web_settings
from tai42_channel_web.store.questions import QuestionRecord, claim_question, peek_question, restore_question
from tai42_channel_web.store.transcript import append_answered, transcript_order

logger = logging.getLogger(__name__)

_QUESTION_NOT_FOUND = "question not found (unknown, expired, or already answered)"
_ALREADY_ANSWERED = "that question was already answered"
# What a visitor is told when the callback door refused their answer without an error
# message of its own — never the raw body an intermediary may have written.
_CALLBACK_REFUSED = "the answer was refused"
# Appended once the refused question has been restored as often as it may be: the
# record is gone, so the visitor is told rather than left retrying into a 404.
_ANSWER_RETRIES_SPENT = "that question can no longer be answered"


class AnswerForwardError(Exception):
    """The interactions callback door did not accept the forwarded answer (mapped to 500)."""


def _door_error_detail(response: httpx.Response) -> str:
    """The callback door's OWN error message, or a fixed refusal when the reply does not carry one.

    Only a body in this platform's envelope is relayed. Anything else — a proxy or WAF
    page that answered instead of the door, a framework traceback, an upstream banner
    — is replaced and logged: it is written by an intermediary, names hosts and
    software the visitor is not entitled to, and this is an anonymous public door.
    """
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("error"), str):
        return payload["error"][:500]
    logger.warning(
        "the interactions callback door answered HTTP %d with a body that is not this platform's error "
        "envelope; the visitor is told nothing of it: %s",
        response.status_code,
        response.text[:500],
    )
    return _CALLBACK_REFUSED


def _is_already_answered(response: httpx.Response) -> bool:
    """``True`` for the callback door's idempotent lost-claim reply.

    ``200 {"data": {"status": "already_answered"}}`` — the door's ONLY answer to a lost claim; it has no 409.
    """
    try:
        payload = response.json()
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    data = payload.get("data")
    return isinstance(data, dict) and data.get("status") == "already_answered"


async def _forward_answer(callback_url: str, answer: Any, params: dict[str, str] | None = None) -> httpx.Response:
    """POST the answer to the interaction's callback door and return its response.

    Sends ``{"answer": <value>}`` plus ``"params"`` when the session carried link enrichment; the caller
    applies the status policy. Mirrors the skeleton seam's body shape, so a web answer
    delivers the same enrichment beside its answer that a MESSAGE turn does; absent
    params keep the body byte-identical to a plain forward.
    """
    body: dict[str, Any] = {"answer": answer}
    if params:
        body["params"] = params
    async with tai42_app.clients.client_ctx(HttpxClient, timeout=web_settings().http_timeout_seconds) as client:
        return await client.post(callback_url, json=body)


def _answer_from_body(raw: Any) -> tuple[Any, JSONResponse | None]:
    """The forwardable answer value carried by a parsed answer body, or ``(None, <refusal>)``.

    The body must be a JSON object carrying ``answer``, and the value
    must pass the door's transport bound (a scalar or a size-capped object). The
    callback door stays authoritative on format and schema match.
    """
    if not isinstance(raw, dict) or "answer" not in raw:
        return None, _error("body must contain 'answer'", 400)
    answer = raw["answer"]
    bad_answer = _answer_refusal(answer)
    if bad_answer is not None:
        return None, _error(bad_answer, 422)
    return answer, None


async def _claim_owned_question(registration: Any, interaction_id: str) -> QuestionRecord | JSONResponse:
    """Claim the pending question for this caller, or a uniform 404.

    The record is READ first and refused as not-found unless BOTH the web route
    identity and the address are the caller's own — a foreign question is never
    revealed as existing — then claimed with ``GETDEL`` (a missing record, or a claim
    a racing duplicate already took, is the same 404).
    """
    pending = await peek_question(interaction_id)
    if pending is None or not _serves(registration, pending.identity) or pending.address != registration.visitor_id:
        return _error(_QUESTION_NOT_FOUND, 404)
    record = await claim_question(interaction_id)
    if record is None:
        return _error(_QUESTION_NOT_FOUND, 404)
    return record


async def _apply_forward_result(
    forwarded: httpx.Response, record: QuestionRecord, interaction_id: str, answer: Any, restores: int
) -> Response:
    """Apply the callback door's reply to a forwarded answer.

    A 2xx carrying ``already_answered`` recorded someone else's answer, so the record
    stays dropped and no ``chat.answered`` frame is written; any other 2xx appends the
    frame (under the write-order gate, like every agent-side append) and acks. A 404 is
    terminal (the ticket is gone). A 400 rejected THIS answer: the record is restored
    so the visitor can re-answer — until the restore cap is spent, when the question is
    gone and they are told so — and the door's OWN error is surfaced. Anything else
    restores the record and raises so the visitor can retry.
    """
    if forwarded.status_code // 100 == 2:
        if _is_already_answered(forwarded):
            logger.info("interaction %s was already answered elsewhere; this forward recorded nothing", interaction_id)
            return _error(_ALREADY_ANSWERED, 409)
        async with transcript_order(record.identity, record.address):
            await append_answered(record.identity, record.address, interaction_id, answer)
        return _ok({"status": "answered"})
    if forwarded.status_code == 404:
        logger.warning("callback door returned terminal HTTP 404 for %s; dropping the question", interaction_id)
        return _error("question expired or withdrawn", 404)
    if forwarded.status_code == 400:
        detail = _door_error_detail(forwarded)
        if not await restore_question(interaction_id, record, restores):
            return _error(f"{detail}; {_ANSWER_RETRIES_SPENT}", 400)
        return _error(detail, 400)
    await restore_question(interaction_id, record, restores)
    raise AnswerForwardError(
        f"interactions callback rejected the answer: HTTP {forwarded.status_code}: {forwarded.text[:500]}"
    )


@tai42_app.http.custom_route(
    "/questions/{interaction_id}/answer",
    methods=["POST"],
    summary="Answer a pending web chat question",
    tags=["channels"],
    response_model=AnswerResultResponse,
)
async def web_answer(request: Request) -> Response:
    """Forward an answer to a pending web question's interactions callback.

    The record is READ first and answered only when its conversation — BOTH the web
    route identity and the address — is the caller's own; a question is answerable
    solely from the conversation it was asked in, and a foreign one is reported as not
    found. Only then is it claimed with ``GETDEL``. The callback door's reply drives
    the status policy (see ``_apply_forward_result``); a transport failure restores the
    record so the visitor's retry still resolves it, then re-raises.

    One record may be restored only ``max_answer_restores`` times: a forward spends a
    slot of the interactions callback door's own rate limit, which is keyed on this
    server's egress IP and shared with every other channel's answer forwards, so an
    unbounded re-answer loop here drains a bucket the whole deployment rides.
    """
    if is_cross_origin(request):
        return _error(_CROSS_ORIGIN, 403, _CROSS_ORIGIN_CODE)
    off = _store_off()
    if off is not None:
        return off
    settings = web_settings()
    registration = await _session(request, settings)
    if registration is None:
        return _error(_SESSION_MISSING, 401, _SESSION_MISSING_CODE)
    interaction_id = request.path_params["interaction_id"]

    raw, refusal = await _json_body(request, settings)
    if refusal is not None:
        return refusal
    answer, refusal = _answer_from_body(raw)
    if refusal is not None:
        return refusal
    # The session's captured link params ride the answer as enrichment beside it, the
    # SAME params a MESSAGE turn from this session carries — re-bounded by the shared
    # entry-param validator so an over-count/over-size set is a clean 422 rather than
    # an opaque callback error. No reply_id is invented here: on web an option tap
    # flows through the message door, so the answer body is the raw scalar/object plus
    # these params only.
    try:
        params = validate_entry_params(dict(registration.params))
    except ValueError as exc:
        return _error(str(exc), 422)

    claimed = await _claim_owned_question(registration, interaction_id)
    if isinstance(claimed, JSONResponse):
        return claimed
    record = claimed

    restores = settings.max_answer_restores
    try:
        forwarded = await _forward_answer(record.callback_url, answer, params)
    except httpx.HTTPError:
        # Transport failure — restore so the visitor's retry still resolves it.
        await restore_question(interaction_id, record, restores)
        raise
    return await _apply_forward_result(forwarded, record, interaction_id, answer, restores)
