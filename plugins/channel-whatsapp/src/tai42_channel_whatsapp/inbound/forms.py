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

from tai42_contract.app import tai42_app
from tai42_contract.channels import AnswerForwardError, SendOutcome
from tai42_contract.conversations import AnswerPart
from tai42_contract.interactions.models import Wording
from tai42_kit.utils.data.form_text import render_form_text

from tai42_channel_whatsapp.channel import _NOTIFY_FORM_TOKEN_PREFIX, send_form_ask_flow
from tai42_channel_whatsapp.channel.forms import (
    _DOOR_REJECTION_MAX_CHARS,
    _FLOW_BODY_MAX_CHARS,
    _boolean_words,
    _field_words,
)
from tai42_channel_whatsapp.client import send_message, send_outcome
from tai42_channel_whatsapp.correlation import (
    PendingQuestion,
    bump_rejections,
    get_cached_flow_form,
    mark_seen,
    peek_pending,
    release_pending,
)
from tai42_channel_whatsapp.flows_components import _chips_single_pick, split_range_values
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
# The lead + bounded door line alone always fit the cap, so the overflow fallback
# (drop the whole question) is guaranteed deliverable — verified, not assumed. The door line
# is bounded at ``_DOOR_REJECTION_MAX_CHARS`` and the Flow body cap is ``_FLOW_BODY_MAX_CHARS``
# (both owned beside the deliver-time rejection-template guard in ``channel.forms``).
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
    when the value is a string (an OptIn may already deliver a bool). A CheckboxGroup (an array
    field) returns a list of selected strings — delivered as a JSON list, so it passes through
    unchanged. A value that fails coercion is returned raw — the door's 400 path then restores
    the pending ask.
    """
    if isinstance(prop, dict) and prop.get("type") == "array":
        # A CheckboxGroup delivers a JSON list of the selected option values; pass it through
        # as the list answer (a non-list arrival is returned raw so the door 400s and restores).
        return value
    if isinstance(prop, dict) and _chips_single_pick(prop):
        # A single-pick ChipsSelector delivers a ONE-element list; unwrap it to the string the
        # schema declares. A list of any other length is forwarded raw so the door 400s and
        # restores (the existing non-coercible convention).
        return value[0] if isinstance(value, list) and len(value) == 1 else value
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


def _coerce_form_answer(
    response: dict[str, Any], schema: dict[str, Any] | None, labels: dict[str, str]
) -> dict[str, Any]:
    """The form answer forwarded to the door.

    ``response`` minus ``flow_token``, each key mapped from its completion-payload label
    back to the schema key through ``labels`` (the payload-label → schema-key reverse map),
    then each value coerced by that schema key's type. A response key absent from
    ``labels`` is a payload label the map does not know: it is forwarded under its OWN
    key with a WARNING (never dropped), uncoerced.

    A ``rangeStart`` pair drawn as ONE range control returns its value as a ``{start-date,
    end-date}`` object under the START's key; :func:`split_range_values` splits it back onto the
    two schema date keys, so the forwarded answer is always the two scalar date strings.
    """
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    props = properties if isinstance(properties, dict) else {}
    coerced: dict[str, Any] = {}
    for key, value in response.items():
        if key == "flow_token":
            continue
        schema_key = labels.get(key)
        if schema_key is None:
            logger.warning("whatsapp nfm_reply carried unknown payload label %r; forwarding under its own key", key)
            schema_key = key
        coerced[schema_key] = _coerce_value(value, props.get(schema_key))
    return split_range_values(props, coerced)


async def _handle_form_reply(
    interactive: dict[str, Any],
    phone_number_id: str,
    wa_id: str,
    wamid: str,
    params: dict[str, str],
    *,
    reply_to_medium_message_id: str | None,
) -> None:
    """A completed Flow form (``nfm_reply``): forward the coerced answer dict to the pending form question, else bridge.

    The reply matches ONLY when its ``flow_token`` equals the pending ask's
    ``interaction_id``; a malformed ``response_json``, a missing/mismatched token,
    or no pending ask bridges the message (its shape carries no human-readable
    title, so a blank bridge — the same shape an uncorrelated interactive takes).
    ``params`` are the message-level referral entries, carried onto a bridged turn (the
    correlated-answer path takes none); ``reply_to_medium_message_id`` is the ``wamid`` the
    reply quotes, carried on both the answer and the bridged turn. The pending ask is peeked
    before the destructive pop so a non-answer never claims a live ask a concurrent
    genuine reply could still answer.

    A token in the ``tai42-nf:`` namespace is an ASK-LESS form (a ``notify``
    Flow) and branches out BEFORE any pending peek: it has no reservation and
    must never answer — or disturb — a question pending on the same pair.
    """
    response = _extract_form_response(interactive)
    if response is None:
        logger.warning("whatsapp nfm_reply %s carried no JSON-object response_json; bridging", wamid)
        await _bridge_inbound(
            phone_number_id,
            wa_id,
            "",
            wamid,
            params=params or None,
            reply_to_medium_message_id=reply_to_medium_message_id,
        )
        return

    flow_token = response.get("flow_token")
    if isinstance(flow_token, str) and flow_token.startswith(_NOTIFY_FORM_TOKEN_PREFIX):
        await _handle_notify_form_reply(
            response,
            flow_token,
            phone_number_id,
            wa_id,
            wamid,
            params,
            reply_to_medium_message_id=reply_to_medium_message_id,
        )
        return

    pending = await peek_pending(phone_number_id, wa_id)
    if pending is None or not isinstance(flow_token, str) or flow_token != pending.interaction_id:
        await _bridge_inbound(
            phone_number_id,
            wa_id,
            "",
            wamid,
            params=params or None,
            reply_to_medium_message_id=reply_to_medium_message_id,
        )
        return

    # A matched completed form: map the label-keyed response back to the schema keys,
    # coerce to the schema's types, and resolve via the shared ladder. The wamid dedupe
    # upstream guards a redelivery, so no destructive claim is needed before the ladder's
    # own peek. A form ask always reserves its payload-label map; a record that lost it is
    # corruption, raised loudly (never a silent identity map).
    if pending.form_names is None:
        raise AnswerForwardError(
            f"cannot decode the form reply for {wamid}: the pending record for "
            f"({phone_number_id}, {wa_id}) is missing its payload-label map"
        )
    answer = _coerce_form_answer(response, pending.schema, pending.form_names)
    await _resolve_answer(
        phone_number_id,
        wa_id,
        wamid,
        answer,
        pending,
        params=params or None,
        reply_to_medium_message_id=reply_to_medium_message_id,
    )


async def _handle_notify_form_reply(
    response: dict[str, Any],
    flow_token: str,
    phone_number_id: str,
    wa_id: str,
    wamid: str,
    params: dict[str, str],
    *,
    reply_to_medium_message_id: str | None,
) -> None:
    """A completed ASK-LESS form (a ``notify`` Flow) entered into the conversation as a structured message.

    The token sits in the ``tai42-nf:`` namespace.
    No reservation exists for it — the token itself carries the schema hash, which
    resolves the answer schema AND its payload-label reverse map from the durable
    sidecar. On a hit the response's label-keyed answers are mapped back to the schema
    keys and coerced to its types; on a miss (or an unset WABA id, without which the
    sidecar cannot even be addressed) the values are forwarded RAW under their
    label keys — the reply DEGRADES, never drops, and never 5xx's into a
    permanent Meta redelivery loop. The rendered ``label: value`` text (compact JSON for
    an empty form — never blank) is the turn every consumer sees; the structured copy
    rides beside it through the bridge's ``form`` seam, ``params`` carry any
    message-level referral entries and ``reply_to_medium_message_id`` the ``wamid`` it quotes.
    """
    schema_hash, form_tag = _parse_notify_form_token(flow_token)
    waba_id = whatsapp_settings().waba_id
    cached = await get_cached_flow_form(waba_id, schema_hash) if waba_id else None
    if cached is None:
        logger.warning(
            "no cached answer schema for whatsapp notify-form reply %s (hash %s); forwarding raw values",
            wamid,
            schema_hash,
        )
        schema = None
        form = {key: value for key, value in response.items() if key != "flow_token"}
    else:
        schema = cached.schema
        form = _coerce_form_answer(response, schema, cached.names)
    await _bridge_inbound(
        phone_number_id,
        wa_id,
        render_form_text(form, schema),
        wamid,
        form=form,
        params=params or None,
        reply_to_medium_message_id=reply_to_medium_message_id,
        form_tag=form_tag,
    )


def _parse_notify_form_token(flow_token: str) -> tuple[str, str | None]:
    """The schema hash and OPTIONAL caller-set form tag carried by a notify-form flow token.

    The token is ``tai42-nf:{schema_hash}:{nonce}[:{form_tag}]`` — the hash is the first segment
    after the ``tai42-nf:`` prefix, the nonce the second, and the caller-set tag (charset excludes
    ``:``, so always one segment) the OPTIONAL 4th. A 3-segment token (no tag) returns ``None`` for
    the tag; the tag is read back verbatim, never interpreted.
    """
    parts = flow_token[len(_NOTIFY_FORM_TOKEN_PREFIX) :].split(":")
    schema_hash = parts[0]
    form_tag = parts[2] if len(parts) >= 3 else None
    return schema_hash, form_tag


def _door_error_line(retry_reason: str | None, retry_field: str | None, pending: PendingQuestion) -> str | None:
    """The door's own error line for the re-sent Flow, used when the caller set no ``form.error_line``.

    The door's OWN reason — the plain sentence for the refused field — led by that field's label
    (``"Label: sentence"``, the label the person saw on the field, read through the pending record's
    payload-label map) when the door names a declared field; the line is bounded at
    ``_DOOR_REJECTION_MAX_CHARS`` (the ladder already bounds the reason; a line past the bound is
    never sent cut: the label is then left off, else the reason alone is re-capped). ``None`` when
    the door gave no reason.
    """
    if not retry_reason:
        return None
    labels = {key: label for label, key in (pending.form_names or {}).items()}
    label = labels.get(retry_field) if retry_field is not None else None
    if label is not None:
        line = f"{label}: {retry_reason}"
        if len(line) <= _DOOR_REJECTION_MAX_CHARS:
            return line
    return retry_reason[:_DOOR_REJECTION_MAX_CHARS]


async def _recover_form_rejection(
    phone_number_id: str,
    wa_id: str,
    wamid: str,
    pending: PendingQuestion,
    retry_reason: str | None,
    retry_field: str | None = None,
) -> None:
    """Recover a door-rejected form answer by re-sending a fresh Flow for the SAME interaction.

    Bounded by ``_MAX_FORM_REJECTIONS``.

    The shared ladder returned RETRY_KEPT: it KEPT the reservation and — because this
    channel owns the retry notice — sent NO participant message, so the fresh Flow is the
    participant's single correction message (no double-messaging). ``retry_reason`` is the
    door's own (already-truncated) sentence and ``retry_field`` the field it refused. The line the
    re-sent Flow's body carries is the caller's ``form.error_line`` when set, else the door's own
    reason led by the field label (``"Label: sentence"``, :func:`_door_error_line`); it fills each
    ``{reason}`` of the caller's ``answer.rejected`` (empty when there is no line), else follows the
    default lead (a fixed opaque line when there is none). Ordering is
    load-bearing for Meta's redelivery:

    * Under the cap — re-send a fresh Flow (same ``flow_token`` = ``interaction_id``,
      the SAME published Flow reproduced from the pending record's inputs, re-created if
      the cache was lost), then count the rejection on the STILL-HELD record and mark the
      wamid seen. A re-send that itself fails does NOT mark the wamid seen and leaves the
      counter unchanged, then raises — so Meta's redelivery re-runs the ladder, re-hits
      the 400, and re-enters this path (the counter is spent only by a re-send that
      reached the participant).
    * At the cap — tell the participant once the form could not be processed (the caller's
      ``answer.rejected_final`` with the same line in each ``{reason}``, else the default sentence)
      and mark the wamid seen; the ask times out on its side.
    """
    if pending.interaction_id is None or pending.schema is None or pending.question is None:
        raise AnswerForwardError(
            f"cannot recover the form rejection for {wamid}: the pending record is missing its "
            "interaction id, schema, or question text"
        )
    interaction_id, schema = pending.interaction_id, pending.schema
    # Reproduce every per-send text from the stored wording catalogue: the form chapter's open
    # button, screen title, submit label and readback words so the re-sent Flow looks identical, the
    # caller's error line in place of the door's reason, and the caller's refusal sentences
    # (``wording.answer.rejected`` / ``rejected_final``) carrying that line. A stored catalogue that
    # fails to validate is a loud server-bug backstop, never a silent default.
    wording = Wording.model_validate(pending.wording) if pending.wording is not None else None
    form = wording.form if wording is not None else None
    answer = wording.answer if wording is not None else None
    door_line = form.error_line if form is not None and form.error_line is not None else None
    if door_line is None:
        door_line = _door_error_line(retry_reason, retry_field, pending)
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
        final_template = answer.rejected_final if answer is not None else None
        final = (
            final_template.replace("{reason}", door_line or "") if final_template is not None else _FORM_UNPROCESSABLE
        )

        async def send_final() -> SendOutcome:
            sent = await send_message(phone_number_id=phone_number_id, to=wa_id, body=final)
            return send_outcome(phone_number_id, [sent])

        await tai42_app.conversations.send_recorded(
            channel="whatsapp",
            our_identity=phone_number_id,
            client_address=wa_id,
            content=AnswerPart(message=final),
            send=send_final,
        )
        await mark_seen(wamid)
        return

    rejected_template = answer.rejected if answer is not None else None
    tail = (
        rejected_template.replace("{reason}", door_line or "")
        if rejected_template is not None
        else f"{_FORM_REJECTION_LEAD} {door_line or _CALLBACK_REJECTION_OPAQUE}"
    )
    body_text = _rejection_body(pending.question, tail)

    # A re-send that fails must NOT mark the wamid seen and must NOT count the rejection:
    # the record is still held (the ladder kept it), so letting the error propagate is
    # enough — Meta's redelivery re-runs the ladder and re-enters this path. The one
    # sender reproduces the SAME Flow from the pending record's inputs (re-creating it if
    # the published-Flow cache was lost) and navigates to the entry screen with the same
    # prefill and options the first send carried.
    async def resend_flow() -> SendOutcome:
        sent = await send_form_ask_flow(
            phone_number_id=phone_number_id,
            to=wa_id,
            body_text=body_text,
            flow_token=interaction_id,
            schema=schema,
            pages=pending.form_pages,
            values=pending.form_values or {},
            options=pending.form_options or {},
            reactions=pending.form_reactions,
            open_label=form.open if form is not None else None,
            form_title=form.title if form is not None else None,
            form_submit=form.submit if form is not None else None,
            field_invalid=_field_words(form, "invalid"),
            boolean_words=_boolean_words(form),
        )
        return send_outcome(phone_number_id, [sent])

    await tai42_app.conversations.send_recorded(
        channel="whatsapp",
        our_identity=phone_number_id,
        client_address=wa_id,
        content=AnswerPart(message=body_text),
        send=resend_flow,
    )
    await bump_rejections(phone_number_id, wa_id, pending)
    await mark_seen(wamid)


def _rejection_body(question: str, tail: str) -> str:
    """The re-sent Flow's body: the question, then the refusal ``tail``.

    When the composed body would exceed ``_FLOW_BODY_MAX_CHARS`` the question is
    dropped WHOLE — the fresh Flow re-presents the fields, so a mid-string ellipsis
    (forbidden by the no-silent-truncation posture) is never needed. ``tail`` is the refusal
    sentence (the caller's ``wording.answer.rejected`` with the door's reason substituted, else the
    generic lead + reason); its reason is bounded by :func:`_door_error_line` to
    ``_DOOR_REJECTION_MAX_CHARS`` and a caller template is capped at deliver, so the tail always
    fits the cap even when the question is dropped.
    """
    full = f"{question}\n\n{tail}"
    return full if len(full) <= _FLOW_BODY_MAX_CHARS else tail
