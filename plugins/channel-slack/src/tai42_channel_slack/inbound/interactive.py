"""The Slack interactivity door — ``POST /interactive``.

Public and signature-authenticated over the exact raw FORM-ENCODED body (the JSON
rides the ``payload`` field). Handles Block Kit interactivity for ``form``
questions: a ``block_actions`` ``tai42_form_open`` click opens the modal
(``views.open`` with the payload's ``trigger_id``), an option tap resolves a
select/reply, and a ``view_submission`` for ``tai42_form_submit`` coerces the
state per the stored schema and forwards ``{"answer": <dict>}`` to the callback
door, rendering the modal's own ack.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import parse_qs

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from tai42_contract.app import tai42_app
from tai42_contract.channels import InboundAnswerOutcome, InboundBridge
from tai42_kit.utils.data.form_text import render_form_text

from tai42_channel_slack.blocks import decode_reply_value, is_option_tap, is_reply_action
from tai42_channel_slack.channel import open_modal_view
from tai42_channel_slack.correlation import get_form_record, slack_form_correlation_store
from tai42_channel_slack.forms import (
    FORM_OPEN_ACTION_ID,
    FORM_SUBMIT_CALLBACK_ID,
    build_modal_view,
    extract_answer,
    first_field_name,
    is_declared_field,
)
from tai42_channel_slack.inbound.routing import _bridge, _recipients, _resolve_answer
from tai42_channel_slack.inbound.verification import _InboundRejected, _read_verified_body
from tai42_channel_slack.settings import slack_settings

logger = logging.getLogger(__name__)

# The generic participant-facing line rendered inline in the modal on a retryable rejection
# (RETRY_KEPT). The channel OWNS this correction surface (owns_retry_notice=True, so the
# ladder sent no separate notice — no double-messaging); the door's specific field
# reason rides the interactions_answer_rejected operator event the ladder emitted.
_MODAL_RETRY_TEXT = "That answer wasn't accepted. Please check your entries and submit again."

# Shown in the modal when the question's form record is gone (TTL lapsed between
# the message and the submission, or the door reports the ticket terminally gone).
_FORM_EXPIRED_TEXT = "This question is no longer available; it has expired."


@tai42_app.http.custom_route(
    "/interactive",
    methods=["POST"],
    summary="Slack interactivity door (Block Kit form modals, signature-authenticated)",
    tags=["channels"],
    response_model=None,
    no_body_reason="Slack interactivity webhook: vendor ack",
)
async def slack_interactive(request: Request) -> Response:
    """Receive a Slack interactivity POST, verify it, and act on it.

    Same transport auth as the events door: a bounded raw-body read, then the v0
    HMAC over those exact bytes (fail CLOSED; a missing signing secret a logged
    500). The body is ``application/x-www-form-urlencoded`` with the JSON in the
    ``payload`` field, and the signature covers that raw form body. A
    ``block_actions`` ``tai42_form_open`` opens the form modal; a
    ``view_submission`` for ``tai42_form_submit`` forwards the answer; every other
    payload is acked 200 and ignored (Slack needs a 2xx).
    """
    try:
        raw = await _read_verified_body(request)
    except _InboundRejected as rejected:
        return rejected.response

    payload = _parse_interactive_payload(raw)
    if payload is None:
        return JSONResponse({"error": "body must carry a JSON payload field"}, status_code=400)

    payload_type = payload.get("type")
    if payload_type == "block_actions":
        return await _handle_block_actions(payload)
    if payload_type == "view_submission":
        return await _handle_view_submission(payload)
    # A signed interactivity type we do not handle (shortcuts, other actions): ack
    # so Slack does not retry — matching the events door's unknown-type posture.
    return JSONResponse({"status": "ignored"})


def _parse_interactive_payload(raw: bytes) -> dict[str, Any] | None:
    """The JSON object in the ``payload`` field of a form-encoded interactivity
    body, or ``None`` for a body that carries no decodable JSON object."""
    try:
        fields = parse_qs(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return None
    values = fields.get("payload")
    if not values:
        return None
    try:
        payload = json.loads(values[0])
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _errors_response(block_id: str | None, text: str) -> JSONResponse:
    """A ``view_submission`` errors response pinning ``text`` under ``block_id``.

    Slack requires the id of a block present in the view; a submission that
    carried none is malformed and raised rather than silently closed.
    """
    if block_id is None:
        raise ValueError("view_submission carried no input block to report the error on")
    return JSONResponse({"response_action": "errors", "errors": {block_id: text}})


def _state_values(view: dict[str, Any]) -> dict[str, Any]:
    """The ``view.state.values`` map (block_id -> action -> value), or ``{}``."""
    state = view.get("state")
    values = state.get("values") if isinstance(state, dict) else None
    return values if isinstance(values, dict) else {}


def _field_has_block(schema: dict[str, Any], field: str | None) -> bool:
    """Whether the door-named ``field`` is a declared schema property, so its name is a
    valid modal block_id to pin the inline error under (else the caller falls back to the
    first field). A None/absent/nested field has no matching block."""
    return isinstance(field, str) and is_declared_field(schema, field)


async def _handle_block_actions(payload: dict[str, Any]) -> Response:
    """Route one ``block_actions`` click.

    A ``tai42_form_open`` click opens the form modal; an option tap that SUBMITS a reply —
    a ``tai42_select:<index>`` ask-path tap (select answer / suggested reply) or a
    ``tai42_reply:<index>`` notify-path reply tap — resolves the reply; every other action,
    including a ``tai42_link:<index>`` url-button tap (it opens a url and submits nothing),
    is acked-ignored.
    """
    actions = payload.get("actions")
    action = actions[0] if isinstance(actions, list) and actions and isinstance(actions[0], dict) else None
    if action is None:
        return JSONResponse({"status": "ignored"})
    action_id = action.get("action_id")
    if action_id == FORM_OPEN_ACTION_ID:
        return await _open_form_modal(payload, action)
    if isinstance(action_id, str) and is_option_tap(action_id):
        return await _resolve_option_tap(payload, action)
    return JSONResponse({"status": "ignored"})


async def _resolve_option_tap(payload: dict[str, Any], action: dict[str, Any]) -> Response:
    """Resolve an option-button tap to its submit text (and any author-set reply id).

    An ask-path ``tai42_select:<index>`` tap carries the option text verbatim in ``value``;
    a notify-path ``tai42_reply:<index>`` tap carries a JSON envelope of the submit text and
    the author-set option id (decoded via :func:`decode_reply_value`), and that id rides the
    resolved turn as ``params.reply_id`` — the opaque channel enrichment the shared answer
    ladder threads BOTH ways (to the ask callback and, when bridged, onto the turn). The
    anchor message's ``ts`` (from the payload container) is the correlation key the same as
    an in-thread reply. The tap is gated by the SAME allowlist the typed-reply path applies
    (``_process_event``): a tap from an allowlisted channel resolves against a live
    select/suggested-reply ask through the shared ladder, or (a notify option / a
    correlation miss) enters the conversation as a visitor message; a tap from a channel
    outside the allowlist bridges directly, exactly as a typed message from that channel
    would, never touching a pending ask. A malformed tap (no value, no anchor) is
    acked-ignored, never a 5xx.
    """
    raw_value = action.get("value")
    if not isinstance(raw_value, str) or not raw_value:
        return JSONResponse({"status": "ignored"})
    action_id = action.get("action_id")
    if isinstance(action_id, str) and is_reply_action(action_id):
        value, reply_id = decode_reply_value(raw_value)
        params = {"reply_id": reply_id} if reply_id else None
    else:
        value, params = raw_value, None
    container = payload.get("container")
    message_ts = container.get("message_ts") if isinstance(container, dict) else None
    channel_obj = payload.get("channel")
    channel = channel_obj.get("id") if isinstance(channel_obj, dict) else None
    if not isinstance(channel, str) or not channel:
        channel = container.get("channel_id") if isinstance(container, dict) else None
    if not isinstance(message_ts, str) or not message_ts or not isinstance(channel, str) or not channel:
        return JSONResponse({"status": "ignored"})
    # A tap has no Events ``event_id``; the per-tap ``action_ts`` (else the anchor ts)
    # is the conversation-seam dedupe key for a bridged (notify-option) turn.
    action_ts = action.get("action_ts")
    dedupe_id = action_ts if isinstance(action_ts, str) and action_ts else message_ts
    settings = slack_settings()
    if channel not in _recipients(settings):
        # Defense-in-depth, mirroring the typed-reply gate: a tap from a non-allowlisted
        # channel is not an answer to any ask — it bridges exactly as a typed message
        # from that channel would (carrying any reply id as enrichment params).
        return await _bridge(settings.bot_user_id, channel, value, dedupe_id, params=params)
    return await _resolve_answer(message_ts, value, settings.bot_user_id, channel, dedupe_id, params=params)


async def _open_form_modal(payload: dict[str, Any], action: dict[str, Any]) -> Response:
    """Open the form modal for a ``tai42_form_open`` click.

    A missing/expired form record (the button outlived its question) is acked-ignored
    — there is nothing to open. A live record opens the modal inline with the payload's
    short-lived ``trigger_id``; a failed ``views.open`` surfaces as a loud 500.
    """
    interaction_id = action.get("value")
    if not isinstance(interaction_id, str) or not interaction_id:
        raise ValueError("tai42_form_open action carried no interaction id")
    record = await get_form_record(interaction_id)
    if record is None:
        logger.info("slack interactive: form record %s missing or expired; nothing to open", interaction_id)
        return JSONResponse({"status": "ignored"})
    trigger_id = payload.get("trigger_id")
    if not isinstance(trigger_id, str) or not trigger_id:
        raise ValueError("block_actions payload carried no trigger_id")
    # The per-send prefill/choices and step layout reserved at delivery are what this
    # modal renders; absent (a plain ask) render the plain modal.
    data = record.get("data") or {}
    view = build_modal_view(
        interaction_id,
        record["question"],
        record["schema"],
        data.get("values"),
        data.get("options"),
        record.get("pages"),
    )
    await open_modal_view(trigger_id, view)
    return JSONResponse({"status": "opened"})


async def _handle_view_submission(payload: dict[str, Any]) -> Response:
    """Resolve one ``tai42_form_submit`` submission against its pending ask via the ONE
    shared ladder, rendering the modal's own ack.

    The interaction id rides in ``private_metadata`` and IS the correlation key. A gone
    form record shows the expired notice. Otherwise the state is coerced per the stored
    schema and handed to the ladder as ``{"answer": <dict>}`` with
    ``owns_retry_notice=True`` — a completed modal has no re-reply surface, so the
    channel OWNS the participant correction (the ladder sends no separate notice, avoiding a
    double message) and renders it as the modal's inline Block-Kit error on RETRY_KEPT
    (the modal stays open). Outcome -> modal ack: FORWARDED closes the modal (the ladder
    released the record); BRIDGED_KEPT also closes it (the reply was bridged as a fresh
    turn and the ask stays parked — no expired notice, which would be a lie); RETRY_KEPT
    shows the inline error under the first field (record kept); NO_CORRELATION / BRIDGED
    (the ask is gone — the ladder released it and, on a 404, bridged the submission so it
    is never lost) show the expired notice; an AnswerForwardError (5xx) propagates (loud
    500, record kept).
    """
    view = payload.get("view")
    if not isinstance(view, dict) or view.get("callback_id") != FORM_SUBMIT_CALLBACK_ID:
        return JSONResponse({"status": "ignored"})
    interaction_id = view.get("private_metadata")
    if not isinstance(interaction_id, str) or not interaction_id:
        raise ValueError("tai42_form_submit view carried no private_metadata")
    state_values = _state_values(view)
    record = await get_form_record(interaction_id)
    if record is None:
        return _errors_response(next(iter(state_values), None), _FORM_EXPIRED_TEXT)

    schema = record["schema"]
    answer = extract_answer(schema, state_values)
    first_block = first_field_name(schema)

    user = payload.get("user")
    user_id = user.get("id") if isinstance(user, dict) and isinstance(user.get("id"), str) else None
    our_identity = slack_settings().bot_user_id
    raw_view_id = view.get("id")
    view_id = raw_view_id if isinstance(raw_view_id, str) and raw_view_id else interaction_id
    result = await tai42_app.channels.handle_inbound_answer(
        channel_id="slack",
        correlation_key=interaction_id,
        answer=answer,
        store=slack_form_correlation_store,
        bridge=InboundBridge(
            channel_id="slack",
            # The submitting user is the participant; the bot is the operator identity a
            # bridged (gone-ask) submission answers from. Both are addresses only.
            our_identity=our_identity or "",
            client_address=user_id or interaction_id,
            cap_key=user_id or interaction_id,
            provider_message_id=view_id,
            bridge_text=render_form_text(answer, schema),
            owns_retry_notice=True,
        ),
    )
    if result.outcome is InboundAnswerOutcome.FORWARDED:
        # An empty body closes the modal.
        return JSONResponse({})
    if result.outcome is InboundAnswerOutcome.BRIDGED_KEPT:
        # A bridge-policy ask KEPT the correlation (still parked) and bridged this
        # submission as a fresh turn — a digression, not the answer, carrying no participant
        # notice. Closing the modal with an empty body is the accurate ack: the reply was
        # consumed as a turn (the flow replies in-thread) and the parked ask stays
        # answerable via its original message. The expired notice would be a lie — the ask
        # is not gone.
        logger.info("slack interactive: form %s reply bridged as a fresh turn; ask kept parked", interaction_id)
        return JSONResponse({})
    if result.outcome is InboundAnswerOutcome.RETRY_KEPT:
        # The channel owns the correction: the modal stays open with an inline error
        # carrying the door's OWN reason, pinned under the door-named field's block when
        # it is a declared schema property, else the first field.
        logger.warning("slack interactive: door rejected form %s (retry-in-place); record kept", interaction_id)
        error_text = result.retry_reason or _MODAL_RETRY_TEXT
        block = result.retry_field if _field_has_block(schema, result.retry_field) else first_block
        return _errors_response(block, error_text)
    # NO_CORRELATION / BRIDGED: the ask is gone. The ladder released the record (and, on
    # a 404, bridged the submission); tell the participant the question is closed.
    logger.warning(
        "slack interactive: form %s ask is gone (%s); showing the expired notice", interaction_id, result.outcome
    )
    return _errors_response(first_block, _FORM_EXPIRED_TEXT)
