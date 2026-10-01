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
from tai42_channel_slack.channel import open_modal_view, update_modal_view
from tai42_channel_slack.correlation import get_form_record, slack_form_correlation_store
from tai42_channel_slack.forms import (
    FIELD_ACTION_ID,
    FORM_OPEN_ACTION_ID,
    FORM_SUBMIT_CALLBACK_ID,
    build_modal_view,
    decode_private_metadata,
    extract_answer,
    first_field_name,
    is_declared_field,
)
from tai42_channel_slack.inbound.routing import _bridge, _recipients, _resolve_answer
from tai42_channel_slack.inbound.verification import _InboundRejectedError, _read_verified_body
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

# Shown in the modal (as a banner on a re-render, or an inline error on submit) when a form
# reaction cannot be computed — the participant's surface, paired with a loud operator log.
# Never a stale or silent value.
_REACTION_FAILURE_TEXT = "Sorry, this form could not be updated right now. Please try again."


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
    except _InboundRejectedError as rejected:
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
    """The JSON object in the ``payload`` field of a form-encoded interactivity body.

    ``None`` for a body that carries no decodable JSON object.
    """
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
    """Whether the door-named ``field`` is a declared schema property.

    So its name is a valid modal block_id to pin the inline error under (else the
    caller falls back to the first field). A None/absent/nested field has no
    matching block.
    """
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
    if action_id == FIELD_ACTION_ID:
        return await _handle_form_field_action(payload, action)
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
    # The per-send prefill/choices, step layout (display/review) and reaction triggers reserved
    # at delivery are what this modal renders; absent (a plain ask) render the plain modal.
    data = record.get("data") or {}
    view = build_modal_view(
        interaction_id,
        record["question"],
        record["schema"],
        data.get("values"),
        data.get("options"),
        record.get("pages"),
        reactions=record.get("reactions"),
    )
    await open_modal_view(trigger_id, view)
    return JSONResponse({"status": "opened"})


def _normalize_options(option_list: list[Any]) -> list[dict[str, Any]]:
    """A reaction's replaced choice list as the ``{value, label?}`` shape the modal renders from."""
    normalized: list[dict[str, Any]] = []
    for option in option_list:
        value = option["value"]
        normalized.append({"value": value, **({"label": option["label"]} if option.get("label") else {})})
    return normalized


async def _run_field_reaction(
    interaction_id: str,
    field: str,
    entered: dict[str, Any],
    merged_options: dict[str, Any],
    meta_options: dict[str, Any],
    merged_display: dict[str, Any],
    meta_display: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    """Run the consumer's reaction for a changed field through the ONE reaction chokepoint.

    Calls ``tai42_app.interactions.react`` with the values filled so far and folds its validated
    update into the re-render: set ``values`` are returned to overlay the render values; replaced
    option lists and filled display slots accumulate into both the render maps and the metadata
    carriers (so they survive the next re-render); per-field ``errors`` come back to render as
    context lines. A handler failure surfaces LOUDLY — a logged exception plus a banner notice —
    never a stale or silent value. Returns ``(values_update, field_errors, banner)``. ``entered``
    (the values passed to the handler) is never mutated.
    """
    event = {"kind": "field_changed", "field": field}
    try:
        update = await tai42_app.interactions.react(interaction_id, event, entered)
    except Exception:
        logger.exception("slack interactive: reaction for form %s field %r failed", interaction_id, field)
        return {}, {}, _REACTION_FAILURE_TEXT
    update = update or {}
    for name, option_list in (update.get("options") or {}).items():
        normalized = _normalize_options(option_list)
        merged_options[name] = normalized
        meta_options[name] = normalized
    for slot, value in (update.get("display") or {}).items():
        merged_display[slot] = value
        meta_display[slot] = value
    return update.get("values") or {}, update.get("errors") or {}, None


async def _handle_form_field_action(payload: dict[str, Any], action: dict[str, Any]) -> Response:
    """Re-render an open form modal on a reacting/conditional field change via ``views.update``.

    The changed field's block carried ``dispatch_action`` because it is a reaction trigger
    and/or a field another property's ``visibleWhen`` depends on.
    The current entries ride the payload's ``view.state.values``; the reserved record carries the
    schema/pages/options/reactions, and the accumulated reaction state rides ``private_metadata``.
    When the field triggers a consumer reaction, its update is folded in (values/options/errors/
    display); the conditional show/hide is re-evaluated here (platform logic, never a consumer
    call) because the rebuilt blocks hide the fields their predicate hides for the new values.
    A view the plugin did not stamp, or whose record lapsed, is acked without a re-render.
    """
    view = payload.get("view")
    if not isinstance(view, dict):
        return JSONResponse({"status": "ignored"})
    try:
        interaction_id, meta_options, meta_display = decode_private_metadata(view.get("private_metadata"))
    except ValueError:
        # A field dispatch from a view this plugin did not stamp: nothing to re-render.
        return JSONResponse({"status": "ignored"})
    record = await get_form_record(interaction_id)
    if record is None:
        logger.info("slack interactive: form record %s missing or expired; no re-render", interaction_id)
        return JSONResponse({"status": "ignored"})
    view_id = view.get("id")
    if not isinstance(view_id, str) or not view_id:
        raise ValueError("block_actions view carried no id")
    view_hash = view.get("hash")
    schema = record["schema"]
    entered = extract_answer(schema, _state_values(view))
    base_options = (record.get("data") or {}).get("options") or {}
    reactions = record.get("reactions")
    merged_options: dict[str, Any] = {**base_options, **meta_options}
    merged_display: dict[str, Any] = dict(meta_display)
    field_errors: dict[str, Any] = {}
    banner: str | None = None
    values_update: dict[str, Any] = {}
    changed = action.get("block_id")
    if reactions and isinstance(changed, str) and changed in (reactions.get("field_changed") or []):
        values_update, field_errors, banner = await _run_field_reaction(
            interaction_id, changed, entered, merged_options, meta_options, merged_display, meta_display
        )
    render_values = {**entered, **values_update}
    view_out = build_modal_view(
        interaction_id,
        record["question"],
        schema,
        render_values,
        merged_options,
        record.get("pages"),
        reactions=reactions,
        display_values=merged_display,
        field_errors=field_errors,
        metadata_options=meta_options or None,
        metadata_display=meta_display or None,
    )
    if banner is not None:
        view_out["blocks"].insert(1, {"type": "section", "text": {"type": "mrkdwn", "text": banner}})
    await update_modal_view(view_id, view_hash if isinstance(view_hash, str) else None, view_out)
    return JSONResponse({"status": "updated"})


def _submitted_errors_response(schema: dict[str, Any], errors: dict[str, Any], first_block: str) -> JSONResponse:
    """A ``view_submission`` errors response pinning each reaction error under its field's block.

    A declared field pins under its own block_id; an error naming an unknown field falls back to
    the first field (as a door-side rejection does), so no reaction error is ever silently dropped.
    """
    pinned: dict[str, str] = {}
    for field, message in errors.items():
        block = field if is_declared_field(schema, field) else first_block
        pinned[block] = str(message)
    return JSONResponse({"response_action": "errors", "errors": pinned})


async def _run_submitted_reaction(
    interaction_id: str, answer: dict[str, Any], schema: dict[str, Any], first_block: str
) -> JSONResponse | None:
    """Run the consumer's ``submitted`` check through the ONE reaction chokepoint.

    Returns a ``response_action: "errors"`` response (keeping the modal open) when the check
    reports per-field errors or itself fails — a handler failure surfaces LOUDLY as an inline
    error plus a logged exception, never a silently accepted answer. Returns ``None`` when the
    check passes, having folded any finalized values into ``answer`` for the forward.
    """
    try:
        update = await tai42_app.interactions.react(interaction_id, {"kind": "submitted"}, answer)
    except Exception:
        logger.exception("slack interactive: submitted reaction for form %s failed", interaction_id)
        return _errors_response(first_block, _REACTION_FAILURE_TEXT)
    update = update or {}
    errors = update.get("errors") or {}
    if errors:
        return _submitted_errors_response(schema, errors, first_block)
    answer.update(update.get("values") or {})
    return None


async def _handle_view_submission(payload: dict[str, Any]) -> Response:
    """Resolve one ``tai42_form_submit`` submission against its pending ask via the ONE shared ladder.

    Renders the modal's own ack. The interaction id rides in ``private_metadata`` and IS the correlation key. A gone
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
    interaction_id, _, _ = decode_private_metadata(view.get("private_metadata"))
    state_values = _state_values(view)
    record = await get_form_record(interaction_id)
    if record is None:
        return _errors_response(next(iter(state_values), None), _FORM_EXPIRED_TEXT)

    schema = record["schema"]
    answer = extract_answer(schema, state_values)
    first_block = first_field_name(schema)

    reactions = record.get("reactions")
    if reactions and reactions.get("submitted"):
        # The consumer's final check runs on submit (it owns membership for any reaction-fed
        # choice list). Its per-field errors keep the modal open as inline errors; a clean
        # check may finalize values before the answer is forwarded.
        submitted = await _run_submitted_reaction(interaction_id, answer, schema, first_block)
        if submitted is not None:
            return submitted

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
