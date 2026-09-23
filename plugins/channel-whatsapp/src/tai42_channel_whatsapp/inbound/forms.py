"""Flow-form reply handling and door-rejection recovery.

A completed WhatsApp Flow form arrives as an ``nfm_reply``. A reply whose flow
token matches a pending ask is coerced to the schema's types and resolved through
the shared ladder; a token in the ``tai42-nf:`` namespace is an ASK-LESS form (a
``notify`` Flow) that enters the conversation as a structured participant message.
A door-rejected answer is recovered by re-sending a fresh Flow, bounded by a cap.
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any

from tai42_contract.channels import AnswerForwardError
from tai42_kit.utils.data.form_text import render_form_text

from tai42_channel_whatsapp.channel import _NOTIFY_FORM_TOKEN_PREFIX, send_form_ask_flow
from tai42_channel_whatsapp.client import send_message
from tai42_channel_whatsapp.correlation import (
    PendingQuestion,
    bump_rejections,
    get_cached_flow_schema,
    mark_seen,
    peek_pending,
    release_pending,
)
from tai42_channel_whatsapp.inbound.answers import _bridge_inbound, _resolve_answer
from tai42_channel_whatsapp.settings import whatsapp_settings

logger = logging.getLogger(__name__)

# How many times a door-rejected form answer is recovered by re-sending a fresh
# Flow before the participant is told it could not be processed and the ask is left to
# time out. Each re-send spends a slot of the callback door's own rate limit
# (keyed on this server's egress IP, shared across every channel), so the loop is
# bounded — matching the web channel's answer-restore cap in spirit.
_MAX_FORM_REJECTIONS = 5

# The lead-in prefixed to the door's own rejection message in a re-sent Flow body.
_FORM_REJECTION_LEAD = "Your last answer could not be accepted:"
# Shown once when the re-send cap is spent — the ask then times out on its side.
_FORM_UNPROCESSABLE = "Sorry, your form could not be processed."
# Shown in place of a 400 body that is not this platform's error envelope (a proxy
# or WAF page); the participant is never shown an intermediary's content.
_CALLBACK_REJECTION_OPAQUE = "the answer could not be accepted"
# The door's own rejection line is bounded before it rides the participant-facing re-sent
# Flow body — it names the failing field, never an intermediary's whole page.
_DOOR_REJECTION_MAX_CHARS = 500
# Meta caps interactive.body.text at 1024 chars (WhatsApp Cloud API interactive
# message limit); a longer body is a non-retryable param error that fails the
# re-send forever, stranding the participant with neither the Flow nor a final message.
_FLOW_BODY_MAX_CHARS = 1024
# The lead + bounded door line alone always fit the cap, so the overflow fallback
# (drop the whole question) is guaranteed deliverable — verified, not assumed.
if len(_FORM_REJECTION_LEAD) + 1 + _DOOR_REJECTION_MAX_CHARS > _FLOW_BODY_MAX_CHARS:
    raise AssertionError


def _extract_form_response(interactive: dict[str, Any]) -> dict[str, Any] | None:
    """The parsed form response from an ``nfm_reply``, or ``None`` when malformed.

    Meta wraps the completed form as a JSON string in
    ``interactive.nfm_reply.response_json``; a missing field, a non-string value,
    invalid JSON, or a JSON value that is not an object all yield ``None`` (the
    caller bridges instead of forwarding).
    """
    nfm = interactive.get("nfm_reply")
    if not isinstance(nfm, dict):
        return None
    raw = nfm.get("response_json")
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _coerce_value(value: Any, prop: Any) -> Any:
    """One form value coerced to its schema type.

    Flow number inputs arrive as strings, so ``integer``/``number``/``boolean`` are coerced ONLY
    when the value is a string (an OptIn may already deliver a bool). A value that fails coercion is
    returned raw — the door's 400 path then restores the pending ask.
    """
    if not isinstance(prop, dict) or not isinstance(value, str):
        return value
    prop_type = prop.get("type")
    try:
        if prop_type == "integer":
            return int(value)
        if prop_type == "number":
            number = float(value)
            # A non-finite float (inf/nan from e.g. "1e999"/"nan") passes jsonschema
            # yet serializes to null downstream; forward the raw string so the door
            # 400s and restores, the same convention a non-numeric string takes.
            return number if math.isfinite(number) else value
        if prop_type == "boolean":
            return _coerce_bool(value)
    except ValueError:
        return value
    return value


def _coerce_bool(value: str) -> bool:
    """A ``"true"``/``"false"`` string as a bool, else raise ``ValueError``."""
    lowered = value.strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    raise ValueError(f"not a boolean string: {value!r}")


def _coerce_form_answer(response: dict[str, Any], schema: dict[str, Any] | None) -> dict[str, Any]:
    """The form answer forwarded to the door: ``response`` minus ``flow_token``, each value coerced by schema type."""
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    props = properties if isinstance(properties, dict) else {}
    return {key: _coerce_value(value, props.get(key)) for key, value in response.items() if key != "flow_token"}


async def _handle_form_reply(
    interactive: dict[str, Any], phone_number_id: str, wa_id: str, wamid: str, params: dict[str, str]
) -> None:
    """A completed Flow form (``nfm_reply``): forward the coerced answer dict to the pending form question, else bridge.

    The reply matches ONLY when its ``flow_token`` equals the pending ask's
    ``interaction_id``; a malformed ``response_json``, a missing/mismatched token,
    or no pending ask bridges the message (its shape carries no human-readable
    title, so a blank bridge — the same shape an uncorrelated interactive takes).
    ``params`` are the message-level referral/reply-context entries, carried onto a
    bridged turn (the correlated-answer path takes none). The pending ask is peeked
    before the destructive pop so a non-answer never claims a live ask a concurrent
    genuine reply could still answer.

    A token in the ``tai42-nf:`` namespace is an ASK-LESS form (a ``notify``
    Flow) and branches out BEFORE any pending peek: it has no reservation and
    must never answer — or disturb — a question pending on the same pair.
    """
    response = _extract_form_response(interactive)
    if response is None:
        logger.warning("whatsapp nfm_reply %s carried no JSON-object response_json; bridging", wamid)
        await _bridge_inbound(phone_number_id, wa_id, "", wamid, params=params or None)
        return

    flow_token = response.get("flow_token")
    if isinstance(flow_token, str) and flow_token.startswith(_NOTIFY_FORM_TOKEN_PREFIX):
        await _handle_notify_form_reply(response, flow_token, phone_number_id, wa_id, wamid, params)
        return

    pending = await peek_pending(phone_number_id, wa_id)
    if pending is None or not isinstance(flow_token, str) or flow_token != pending.interaction_id:
        await _bridge_inbound(phone_number_id, wa_id, "", wamid, params=params or None)
        return

    # A matched completed form: coerce the response to the schema's types and resolve
    # it via the shared ladder. The wamid dedupe upstream guards a redelivery, so no
    # destructive claim is needed before the ladder's own peek.
    answer = _coerce_form_answer(response, pending.schema)
    await _resolve_answer(phone_number_id, wa_id, wamid, answer, pending, params=params or None)


async def _handle_notify_form_reply(
    response: dict[str, Any], flow_token: str, phone_number_id: str, wa_id: str, wamid: str, params: dict[str, str]
) -> None:
    """A completed ASK-LESS form (a ``notify`` Flow) entered into the conversation as a structured message.

    The token sits in the ``tai42-nf:`` namespace.
    No reservation exists for it — the token itself carries the schema hash, which
    resolves the answer schema from the durable schema sidecar. On a hit the values
    are coerced to the schema's types; on a miss (or an unset WABA id, without which
    the sidecar cannot even be addressed) they are forwarded RAW — the reply
    DEGRADES, never drops, and never 5xx's into a permanent Meta redelivery loop.
    The rendered ``label: value`` text (compact JSON for an empty form — never
    blank) is the turn every consumer sees; the structured copy rides beside it
    through the bridge's ``form`` seam, and ``params`` carry any message-level
    referral/reply-context entries.
    """
    schema_hash = flow_token[len(_NOTIFY_FORM_TOKEN_PREFIX) :].partition(":")[0]
    waba_id = whatsapp_settings().waba_id
    schema = await get_cached_flow_schema(waba_id, schema_hash) if waba_id else None
    if schema is None:
        logger.warning(
            "no cached answer schema for whatsapp notify-form reply %s (hash %s); forwarding raw values",
            wamid,
            schema_hash,
        )
    form = _coerce_form_answer(response, schema)
    await _bridge_inbound(
        phone_number_id, wa_id, render_form_text(form, schema), wamid, form=form, params=params or None
    )


def _door_error_line(retry_reason: str | None) -> str:
    """The participant-facing error line for the re-sent Flow.

    The door's OWN reason (already length-bounded by the ladder, re-capped here defensively at
    ``_DOOR_REJECTION_MAX_CHARS`` which names the failing field), or the fixed opaque line when the
    door gave none.
    """
    return retry_reason[:_DOOR_REJECTION_MAX_CHARS] if retry_reason else _CALLBACK_REJECTION_OPAQUE


async def _recover_form_rejection(
    phone_number_id: str, wa_id: str, wamid: str, pending: PendingQuestion, retry_reason: str | None
) -> None:
    """Recover a door-rejected form answer by re-sending a fresh Flow for the SAME interaction.

    Bounded by ``_MAX_FORM_REJECTIONS``.

    The shared ladder returned RETRY_KEPT: it KEPT the reservation and — because this
    channel owns the retry notice — sent NO participant message, so the fresh Flow is the
    participant's single correction message (no double-messaging). ``retry_reason`` is the
    door's own (already-truncated) message, which names the failing field and rides the
    re-sent Flow's body; a missing reason falls back to a fixed opaque line. Ordering is
    load-bearing for Meta's redelivery:

    * Under the cap — re-send a fresh Flow (same ``flow_token`` = ``interaction_id``,
      the SAME published Flow reproduced from the pending record's inputs, re-created if
      the cache was lost), then count the rejection on the STILL-HELD record and mark the
      wamid seen. A re-send that itself fails does NOT mark the wamid seen and leaves the
      counter unchanged, then raises — so Meta's redelivery re-runs the ladder, re-hits
      the 400, and re-enters this path (the counter is spent only by a re-send that
      reached the participant).
    * At the cap — tell the participant once the form could not be processed and mark the
      wamid seen; the ask times out on its side.
    """
    if pending.interaction_id is None or pending.schema is None or pending.question is None:
        raise AnswerForwardError(
            f"cannot recover the form rejection for {wamid}: the pending record is missing its "
            "interaction id, schema, or question text"
        )
    if pending.rejections >= _MAX_FORM_REJECTIONS:
        logger.error(
            "form answer for %s rejected %d times (cap %d); not re-sending — telling the participant and "
            "letting the ask time out",
            wamid,
            pending.rejections,
            _MAX_FORM_REJECTIONS,
        )
        # Release the reservation the ladder kept: no re-Flow surface remains, so a
        # later inbound must bridge (not re-answer) while the ask times out server-side.
        await release_pending(phone_number_id, wa_id)
        await send_message(phone_number_id=phone_number_id, to=wa_id, body=_FORM_UNPROCESSABLE)
        await mark_seen(wamid)
        return

    body_text = _rejection_body(pending.question, _door_error_line(retry_reason))
    # A re-send that fails must NOT mark the wamid seen and must NOT count the rejection:
    # the record is still held (the ladder kept it), so letting the error propagate is
    # enough — Meta's redelivery re-runs the ladder and re-enters this path. The one
    # sender reproduces the SAME Flow from the pending record's inputs (re-creating it if
    # the published-Flow cache was lost) and navigates to the entry screen with the same
    # prefill and options the first send carried.
    await send_form_ask_flow(
        phone_number_id=phone_number_id,
        to=wa_id,
        body_text=body_text,
        flow_token=pending.interaction_id,
        schema=pending.schema,
        pages=pending.form_pages,
        values=pending.form_values or {},
        options=pending.form_options or {},
    )
    await bump_rejections(phone_number_id, wa_id, pending)
    await mark_seen(wamid)


def _rejection_body(question: str, error_line: str) -> str:
    """The re-sent Flow's body: the question, then the door's rejection line.

    When the composed body would exceed ``_FLOW_BODY_MAX_CHARS`` the question is
    dropped WHOLE — the fresh Flow re-presents the fields, so a mid-string ellipsis
    (forbidden by the no-silent-truncation posture) is never needed. ``error_line`` is
    the door's OWN reason (which names the failing field), bounded by
    :func:`_door_error_line` to ``_DOOR_REJECTION_MAX_CHARS`` — or the fixed opaque line
    when the door gave none — so the lead + tail always fits the cap.
    """
    tail = f"{_FORM_REJECTION_LEAD} {error_line}"
    full = f"{question}\n\n{tail}"
    return full if len(full) <= _FLOW_BODY_MAX_CHARS else tail
