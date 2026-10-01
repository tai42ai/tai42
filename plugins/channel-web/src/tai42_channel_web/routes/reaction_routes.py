"""The reaction door: forward a pending form question's mid-form reaction to its ticket react door.

A SIBLING of the answer door for a reacting form. It runs the SAME session + cross-origin
+ question-record checks the answer door runs, then forwards the event and the values
filled so far SERVER-SIDE to the interaction's ticket-bearing react door — exactly as the
answer door forwards the answer to the callback. The ticket lives only inside the stored
``callback_url`` and never reaches the browser; the visitor sees only the returned form
update. The reaction is STATELESS: the record is READ (never claimed), so the question
stays open.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from tai42_contract.app import tai42_app
from tai42_kit.clients.impl.http import HttpxClient

from tai42_channel_web.routes.dtos import ReactionResultResponse, _reaction_refusal
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
from tai42_channel_web.store.questions import QuestionRecord, peek_question

logger = logging.getLogger(__name__)

_QUESTION_NOT_FOUND = "question not found (unknown, expired, or already answered)"
# What a visitor is told when the ticket react door refused the reaction without an error
# message of its own — never the raw body an intermediary may have written.
_REACTION_REFUSED = "the reaction was refused"


class ReactionForwardError(Exception):
    """The ticket react door did not accept the forwarded reaction (mapped to 500)."""


def _react_url(callback_url: str) -> str:
    """The interaction's ticket react door URL: its answer callback sink plus the ``/react`` sibling.

    The ticket lives ONLY inside ``callback_url`` (a stored, server-side value), so deriving
    the react sibling from it keeps the ticket off the browser exactly as the answer forward does.
    """
    return f"{callback_url}/react"


def _door_error_detail(response: httpx.Response) -> str:
    """The ticket react door's OWN error message, or a fixed refusal when the reply does not carry one.

    Only a body in this platform's envelope is relayed. Anything else — a proxy or WAF
    page, a framework traceback, an upstream banner — is replaced and logged: it is
    written by an intermediary, names hosts and software the visitor is not entitled to,
    and this is an anonymous public door.
    """
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("error"), str):
        return payload["error"][:500]
    logger.warning(
        "the ticket react door answered HTTP %d with a body that is not this platform's error "
        "envelope; the visitor is told nothing of it: %s",
        response.status_code,
        response.text[:500],
    )
    return _REACTION_REFUSED


def _reaction_from_body(raw: Any) -> tuple[Any, Any, JSONResponse | None]:
    """The forwardable ``(event, values)`` carried by a parsed reaction body, or ``(None, None, <refusal>)``.

    The body must be a JSON object carrying BOTH ``event`` and ``values``, and both must
    pass the door's transport bound. The ticket react door stays authoritative on the
    event's declared triggers and on the values' schema fit.
    """
    if not isinstance(raw, dict) or "event" not in raw or "values" not in raw:
        return None, None, _error("body must contain 'event' and 'values'", 400)
    event = raw["event"]
    values = raw["values"]
    bad = _reaction_refusal(event, values)
    if bad is not None:
        return None, None, _error(bad, 422)
    return event, values, None


async def _owned_question(registration: Any, interaction_id: str) -> QuestionRecord | JSONResponse:
    """The pending question PEEKED for this caller, or a uniform 404.

    The record is READ (never claimed — a reaction leaves the question open) and refused as
    not-found unless BOTH the web route identity and the address are the caller's own, so a
    foreign question is never revealed as existing — the SAME ownership rule the answer door
    applies before it claims.
    """
    pending = await peek_question(interaction_id)
    if pending is None or not _serves(registration, pending.identity) or pending.address != registration.visitor_id:
        return _error(_QUESTION_NOT_FOUND, 404)
    return pending


async def _forward_reaction(react_url: str, event: Any, values: Any) -> httpx.Response:
    """POST the event and the values filled so far to the interaction's ticket react door.

    The body is ``{"event": <event>, "values": <values>}`` — the exact shape the react door
    parses; the caller applies the result policy.
    """
    async with tai42_app.clients.client_ctx(HttpxClient, timeout=web_settings().http_timeout_seconds) as client:
        return await client.post(react_url, json={"event": event, "values": values})


def _apply_reaction_result(forwarded: httpx.Response) -> Response:
    """Apply the ticket react door's reply to a forwarded reaction.

    A 2xx carries the validated form update under the ticket door's ``{"data": {"update":
    <update>}}`` envelope; the update is returned to the visitor as this door's own
    ``{"data": <update>}`` body — the bare form update the browser's form widget applies
    (an absent/malformed update is a server fault, raised). A 404 is terminal (the ticket is
    gone). A 400/409/413 relays the door's OWN error at the same status (a malformed event, a
    closed interaction, an over-cap body). A 502 — the handler raised or missed its deadline
    — is surfaced LOUDLY as the door's error, never a stale/silent value. Anything else raises
    so the failure is visible.
    """
    if forwarded.status_code // 100 == 2:
        try:
            payload = forwarded.json()
        except ValueError:
            payload = None
        data = payload.get("data") if isinstance(payload, dict) else None
        update = data.get("update") if isinstance(data, dict) else None
        if not isinstance(update, dict):
            raise ReactionForwardError(
                f"ticket react door returned HTTP {forwarded.status_code} without a form update: {forwarded.text[:500]}"
            )
        return _ok(update)
    if forwarded.status_code == 404:
        return _error("question expired or withdrawn", 404)
    if forwarded.status_code in (400, 409, 413):
        return _error(_door_error_detail(forwarded), forwarded.status_code)
    if forwarded.status_code == 502:
        return _error(_door_error_detail(forwarded), 502)
    raise ReactionForwardError(
        f"ticket react door rejected the reaction: HTTP {forwarded.status_code}: {forwarded.text[:500]}"
    )


@tai42_app.http.custom_route(
    "/questions/{interaction_id}/react",
    methods=["POST"],
    summary="React to a pending web chat form question",
    tags=["channels"],
    response_model=ReactionResultResponse,
)
async def web_react(request: Request) -> Response:
    """Forward a mid-form reaction to a pending web form question's ticket react door.

    A SIBLING of the answer door: the record is reactable only from the conversation it was
    asked in — BOTH the web route identity and the address the caller's own — so a foreign
    one is reported as not found, under the SAME session + cross-origin + ownership checks
    the answer door applies. The record is PEEKED, never claimed: a reaction records nothing
    and leaves the question open. The event + the values filled so far are forwarded
    SERVER-SIDE to the interaction's ticket react door (the ticket, held only in the stored
    ``callback_url``, never reaches the browser), and its validated form update is returned
    to the visitor. The ticket react door stays authoritative on the event's declared
    triggers, the values' schema fit, the open-interaction check, and the handler deadline.
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
    event, values, refusal = _reaction_from_body(raw)
    if refusal is not None:
        return refusal

    owned = await _owned_question(registration, interaction_id)
    if isinstance(owned, JSONResponse):
        return owned
    record = owned

    forwarded = await _forward_reaction(_react_url(record.callback_url), event, values)
    return _apply_reaction_result(forwarded)
