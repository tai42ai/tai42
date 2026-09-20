"""The authed message/event submission doors that run a conversation turn."""

from __future__ import annotations

import sys

from fastapi import Request
from pydantic import BaseModel, ValidationError
from starlette.responses import JSONResponse, Response
from tai42_contract.conversations import ConversationAnswer, ConversationEventSubmission, ConversationMessage

from tai42_skeleton.app.http import http_surface
from tai42_skeleton.app.route_registry import DeclaredRouteMetadata
from tai42_skeleton.conversations.caps import AddressRateLimitedError, ThreadQueueOverflowError
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.conversations.turn import (
    ApiSubmitResult,
    ConversationRouteResolutionError,
    EventTargetNotToolError,
    ThreadNotFoundError,
)
from tai42_skeleton.operations.errors import NotSupportedError

# This submodule's OWN package object (the one it was imported as part of), captured at
# import time from ``sys.modules`` — NOT ``from tai42_skeleton.routers import conversations``,
# whose parent-attribute read is stale mid-reload. The seam symbols
# (``get_current_user_id``/``reload_gate``) are read through it at call time, so a test's
# patch on this package alias is the single point the submission doors read.
_pkg = sys.modules["tai42_skeleton.routers.conversations"]

# The shared exception-type -> HTTP-status table for turn-submission failures; the
# event door extends it with the two event-only terminals.
_TURN_SUBMISSION_STATUS: dict[type[Exception], int] = {
    ConversationRouteResolutionError: 404,
    AddressRateLimitedError: 429,
    ThreadQueueOverflowError: 503,
    NotSupportedError: 501,
}
_EVENT_SUBMISSION_STATUS: dict[type[Exception], int] = {
    ThreadNotFoundError: 404,
    EventTargetNotToolError: 409,
    **_TURN_SUBMISSION_STATUS,
}


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


def _door_caller_principal() -> str | None:
    """The accountable principal for an api/event-door turn — the id the turn keys its thread and bucket on.

    With access control ON the auth gate binds the caller's id before the door runs. With
    it OFF no id is bound, so the door acts AS the platform's synthetic no-auth identity
    (the same principal the gate-off projection names), keyed exactly as a real caller's id
    would be. ``None`` is returned only with the gate ON and no id bound — a state the auth
    gate makes unreachable — which the turn seam refuses.
    """
    principal = _pkg.get_current_user_id()
    if principal is not None and principal.strip():
        return principal
    from tai42_skeleton.access_control.projection import NO_AUTH_USER_ID
    from tai42_skeleton.access_control.settings import access_control_settings

    if not access_control_settings().enable:
        return NO_AUTH_USER_ID
    return None


def _turn_submission_error(exc: Exception, status_map: dict[type[Exception], int]) -> JSONResponse | None:
    """Map a caught turn-submission failure to its plain-envelope error response by the status table.

    Returns ``None`` when the failure is not a mapped turn-submission error (the caller re-raises it —
    nothing is swallowed).
    """
    for exc_type, status in status_map.items():
        if isinstance(exc, exc_type):
            return _error(str(exc), status)
    return None


def _turn_ack_response(result: ApiSubmitResult) -> JSONResponse:
    """Shape the submission ack from the turn's outcome.

    ``200`` with the answer present (``exclude_none`` drops the answer's own null fields, never the outer
    ``answer`` key, so a silent turn answers 200 with its silent marker), or the default deferred ``202``
    when the turn produced no outcome yet.
    """
    payload: dict[str, object] = {"message_id": result.message_id, "thread_id": result.thread_id}
    if result.answer is not None:
        payload["answer"] = result.answer.model_dump(mode="json", exclude_none=True)
        return JSONResponse({"data": payload}, status_code=200)
    return JSONResponse({"data": payload}, status_code=202)


class ConversationTurnAck(BaseModel):
    """The ack a message/event submission returns: the accepted turn's ``message_id`` and ``thread_id``.

    ``answer`` is present on every inline-waited turn that finished in time (a 200) — including a silent one,
    which carries the silent marker (status ``silent``, no answer text, dumped ``exclude_none``); it is
    absent only on the default deferred 202, whose turn produced no outcome yet.
    """

    message_id: str
    thread_id: str
    answer: ConversationAnswer | None = None


@http_surface().custom_route(
    "/api/conversations/{route_name}/messages",
    methods=["POST"],
    summary="Send a message to a conversation route",
    tags=["conversations"],
    request_model=ConversationMessage,
    response_model=ConversationTurnAck,
    declared=DeclaredRouteMetadata(
        reload_gated=True,
        reads_body=True,
        # The declared 503 is the plain-envelope one this handler answers on a full
        # thread queue; the reload gate's own 503 comes from ``reload_gated``.
        error_statuses=(400, 401, 404, 429, 501, 503),
        success_status=202,
        additional_success_statuses=(200,),
    ),
    action="write",
)
async def send_conversation_message(request: Request) -> Response:
    """Accept one authed message for ``route_name`` and run its turn AS the route's execution key.

    The auth gate authorizes who may SEND; the turn's own authority is the route's
    execution key, not the caller. Default answer is ``202 {message_id, thread_id}``; the
    answer is then delivered later by signed callback when the route declares a
    ``callback_url``, and otherwise read back from the poll door
    (``GET .../messages/{message_id}``). With a ``wait_seconds`` body field (clamped here to
    ``sync_wait_max_seconds``) a turn finishing in time answers ``200`` inline — an
    answered/error turn with the answer, a silent turn with the silent marker (status
    ``silent``, no answer text) — and any callback (which otherwise carries
    answered/error/silent) is suppressed the same way, so it never double-fires; a turn
    still running when the wait elapses falls back to ``202``.
    """
    if _pkg.reload_gate.locked:
        return _pkg.reload_gate.reject_response()
    route_name = request.path_params["route_name"]
    try:
        body = await request.json()
    except ValueError:
        return _error("invalid JSON body", 400)
    if not isinstance(body, dict):
        return _error("body must be a JSON object", 400)
    try:
        message = ConversationMessage.model_validate(body)
    except ValidationError as exc:
        # VALUE-FREE: a params failure's pydantic error dict embeds ``input_value``, so the
        # body renders each error's ``msg`` only (which names the violated bound, never the
        # value) — never ``str(exc)`` or the error dicts, both of which reflect the input.
        detail = "; ".join(error["msg"] for error in exc.errors())
        return _error(f"invalid conversation message: {detail}", 400)

    cap = ConversationsSettings().sync_wait_max_seconds
    wait_seconds = 0 if message.wait_seconds is None else min(message.wait_seconds, cap)

    from tai42_skeleton.conversations import submit_api_message

    try:
        result = await submit_api_message(
            route_name,
            message.external_user_id,
            message.text,
            _door_caller_principal(),
            wait_seconds,
            params=message.params,
            form=message.form,
            attachments=message.attachments,
            location=message.location,
            locale=message.locale,
        )
    except Exception as exc:
        mapped = _turn_submission_error(exc, _TURN_SUBMISSION_STATUS)
        if mapped is None:
            raise
        return mapped
    return _turn_ack_response(result)


@http_surface().custom_route(
    "/api/conversations/{route_name}/events",
    methods=["POST"],
    summary="Deliver a structured event to a conversation thread as a turn",
    tags=["conversations"],
    request_model=ConversationEventSubmission,
    response_model=ConversationTurnAck,
    declared=DeclaredRouteMetadata(
        reload_gated=True,
        reads_body=True,
        error_statuses=(400, 401, 404, 409, 429, 501, 503),
        success_status=202,
        additional_success_statuses=(200,),
    ),
    action="write",
)
async def send_conversation_event(request: Request) -> Response:
    """Run a structured event as a turn on an EXISTING thread of ``route_name``.

    The thread must already exist: an event enters a conversation as a turn and never mints a
    thread. Address it by the ``address`` its listing shows or by its ``thread_id`` (exactly
    one). The turn runs the route's TOOL target AS the route's execution key — an
    agent-target route is refused (``409``) since an event carries no rendered text. The
    event is IDEMPOTENT on ``event_id``: a redelivery returns the original turn's
    ``message_id`` (``202``) and runs no second turn.

    The answer is delivered by the TARGET route's door — a channel route texts the thread's
    address, an api route POSTs the route's signed callback (a ``wait_seconds`` body field,
    clamped to ``sync_wait_max_seconds``, returns a finished turn's answer inline in the
    ``200`` and suppresses the callback). Default is ``202 {message_id, thread_id}``.

    This is a TRUSTED-integration door: an authorized writer may address ANY existing thread
    of the route by ``thread_id`` (a channel participant's included), so a deployment grants its
    write action to service principals, not to low-trust API keys.
    """
    if _pkg.reload_gate.locked:
        return _pkg.reload_gate.reject_response()
    route_name = request.path_params["route_name"]
    try:
        body = await request.json()
    except ValueError:
        return _error("invalid JSON body", 400)
    if not isinstance(body, dict):
        return _error("body must be a JSON object", 400)
    try:
        submission = ConversationEventSubmission.model_validate(body)
    except ValidationError as exc:
        # VALUE-FREE: render each error's ``msg`` (which names the violated bound, never the
        # value), never ``str(exc)`` or the error dicts — both reflect the opaque input.
        detail = "; ".join(error["msg"] for error in exc.errors())
        return _error(f"invalid conversation event: {detail}", 400)

    from tai42_skeleton.conversations import submit_event

    try:
        result = await submit_event(route_name, submission, _door_caller_principal())
    except Exception as exc:
        mapped = _turn_submission_error(exc, _EVENT_SUBMISSION_STATUS)
        if mapped is None:
            raise
        return mapped
    return _turn_ack_response(result)
