"""WhatsApp-Flow form send: the form ask (ask delivery) and the ask-less notify.

Both build and validate the per-send Flow and its data before any network work,
resolve one published Flow per Flow identity
(:mod:`tai42_channel_whatsapp.channel.flow_publishing`), and send
the Flow message. The ask reserves a correlation; the ask-less notify rides a
``tai42-nf:`` token instead.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from tai42_contract.channels import ChannelDelivery, ChannelDeliveryError, ChannelInputError, ChannelNotification
from tai42_contract.interactions.models import FormReactions, FormWording

from tai42_channel_whatsapp.channel.flow_publishing import _resolve_flow_id
from tai42_channel_whatsapp.channel.media import _send_media_prelude
from tai42_channel_whatsapp.client import (
    SentMessage,
    send_flow,
)
from tai42_channel_whatsapp.correlation import (
    EndpointForm,
    cache_endpoint_form,
    cache_flow_form,
    drop_endpoint_form,
    release_pending,
    reserve_pending,
)
from tai42_channel_whatsapp.flows import (
    _FALSE_LABEL,
    _TRUE_LABEL,
    FORM_ENTRY_SCREEN,
    build_flow_data,
    build_form_flow,
    component_names,
    form_needs_endpoint,
    payload_labels,
)
from tai42_channel_whatsapp.flows_components import _field_control
from tai42_channel_whatsapp.flows_endpoint import FORM_LINE_MEMBERS
from tai42_channel_whatsapp.flows_shapes import ERROR_MESSAGE_MAX_CHARS
from tai42_channel_whatsapp.settings import (
    WhatsAppSettings,
    require_delivery_secret,
    require_delivery_setting,
    whatsapp_settings,
)

# The flow-token namespace for an ASK-LESS form notification: prefix + schema hash
# + a random suffix. Inbound routing branches on this prefix BEFORE any pending-ask
# lookup, so a notify-form submission can never answer (or disturb) a question
# pending on the same pair — while an ask's flow token stays its interaction id
# verbatim and never enters this namespace. The prefix is wire-visible in every
# delivered form's token; changing it orphans the forms already sitting in chats.
_NOTIFY_FORM_TOKEN_PREFIX = "tai42-nf:"  # noqa: S105 constant identifier, not a secret value

# Meta's cap on the Flow message's ``flow_cta`` (the chat button that opens the Flow): up to 20
# characters, no emoji. LIVE-CITE(flow_cta cap): the documented numbers disagree (pywa mirrors 20,
# Meta's "Sending a Flow" guide advises 30), so the stricter 20 is enforced here and the live API's
# actual refusal boundary is proved on the live leg before this constant is loosened.
_OPEN_LABEL_MAX_CHARS = 20

# Meta caps the Flow message's ``interactive.body.text`` at this many characters. A door-rejection
# re-send carries the caller's refusal sentence (``wording.answer.rejected``) with the door's own
# reason substituted into every ``{reason}`` token, so the caller's template must leave room for the
# reason: the door line is bounded at ``_DOOR_REJECTION_MAX_CHARS`` before it rides the body, and the
# template's worst-case expansion (its fixed text plus every token filled) is capped at the body size.
_FLOW_BODY_MAX_CHARS = 1024
_DOOR_REJECTION_MAX_CHARS = 500
# Meta caps a text message body at this many characters. The closing sentence a refused answer at
# the re-send cap draws (``wording.answer.rejected_final``) rides a text message with the door's line
# in every ``{reason}`` token, so its worst-case expansion is capped at the text body size.
_TEXT_BODY_MAX_CHARS = 4096


def _form_wording(send: ChannelDelivery | ChannelNotification) -> FormWording | None:
    """The send's ``wording.form`` chapter, or ``None`` when the send carried no form wording."""
    return send.wording.form if send.wording is not None else None


def _guard_open_label(label: str | None) -> None:
    """Refuse a caller ``wording.form.open`` over Meta's ``flow_cta`` cap, before any store or send.

    A label the chat button cannot carry is a permanent input refusal (:class:`ChannelInputError`),
    never truncated and never retried.
    """
    if label is not None and len(label) > _OPEN_LABEL_MAX_CHARS:
        raise ChannelInputError(
            f"form open-button label is {len(label)} characters; WhatsApp caps the flow button "
            f"(flow_cta) at {_OPEN_LABEL_MAX_CHARS} characters"
        )


def _field_words(form: FormWording | None, member: Literal["invalid", "required"]) -> dict[str, str]:
    """The caller's per-field ``wording.form.fields[<name>].<member>`` sentences, by property name.

    ``invalid`` words a value that breaks its field's shape, ``required`` a missing value. Every door
    that builds a form's Flow or its endpoint sidecar reads them here: the two sends pass the send's
    form chapter, the door-rejection re-send the stored one.
    """
    if form is None:
        return {}
    sentences = {name: getattr(words, member) for name, words in form.fields.items()}
    return {name: sentence for name, sentence in sentences.items() if sentence is not None}


def _boolean_words(form: FormWording | None) -> dict[str, tuple[str, str]]:
    """The caller's ``(true_label, false_label)`` words for each boolean field that sets at least one.

    The Flow's review readback bakes them in; an unset side takes the builder's default word.
    """
    if form is None:
        return {}
    return {
        name: (words.true_label or _TRUE_LABEL, words.false_label or _FALSE_LABEL)
        for name, words in form.fields.items()
        if words.true_label is not None or words.false_label is not None
    }


def _form_lines(form: FormWording | None) -> dict[str, str]:
    """The caller's form-level lines the data endpoint draws (``error_line``, ``update_failed``), by member."""
    if form is None:
        return {}
    lines = {member: getattr(form, member) for member in FORM_LINE_MEMBERS}
    return {member: line for member, line in lines.items() if line is not None}


def _guard_ask_error_line(lines: dict[str, str]) -> None:
    """Refuse an ask's ``wording.form.error_line`` too long to ride a door-rejection re-send, up front.

    A refused form answer is re-asked with a fresh Flow whose body carries the caller's error line in
    place of the door's own reason, which is bounded at ``_DOOR_REJECTION_MAX_CHARS`` so the body
    template's worst case (:func:`_guard_rejected_template`) holds. A longer line is a permanent input
    refusal (:class:`ChannelInputError`), checked at deliver. An ask-less form is never re-sent, so its
    line takes no such bound.
    """
    line = lines.get("error_line")
    if line is not None and len(line) > _DOOR_REJECTION_MAX_CHARS:
        raise ChannelInputError(
            f"wording.form.error_line is {len(line)} characters; it fills the re-sent Flow body in place of "
            f"the door's reason, which WhatsApp bounds at {_DOOR_REJECTION_MAX_CHARS}"
        )


def _guard_field_sentences(
    schema: dict[str, Any],
    option_fields: set[str],
    member: Literal["invalid", "required"],
    sentences: dict[str, str],
) -> None:
    """Refuse a caller per-field sentence over Meta's ``error-message`` cap for its field's control, up front.

    The data endpoint shows an ``invalid`` or ``required`` sentence in the failing field's
    ``error-message``, which Meta caps per control
    (:data:`~tai42_channel_whatsapp.flows_shapes.ERROR_MESSAGE_MAX_CHARS` — 30 characters on a text
    input or text area, 80 on a calendar or choice control). A sentence over its field's cap is a
    permanent input refusal (:class:`ChannelInputError`) naming the field, the member and the cap,
    checked before any store or send — never truncated. A field drawn without an ``error-message``
    (an opt-in, a chip grid) shows its sentence only in the screen's error line, which takes no cap here.
    """
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return
    names = component_names(properties)
    for name, sentence in sentences.items():
        prop = properties.get(name)
        if not isinstance(prop, dict):
            continue
        control = _field_control(name, prop, False, option_fields, names)["type"]
        cap = ERROR_MESSAGE_MAX_CHARS.get(control)
        if cap is not None and len(sentence) > cap:
            raise ChannelInputError(
                f"wording.form.fields[{name!r}].{member} is {len(sentence)} characters; WhatsApp caps the error "
                f"message of a {control} at {cap}"
            )


def _guard_rejected_template(template: str | None) -> None:
    """Refuse a caller ``wording.answer.rejected`` too long to ride a door-rejection re-send, up front.

    A refused form answer is re-asked with a fresh Flow whose body is the caller's refusal sentence
    with the door's own reason substituted for EACH ``{reason}`` token. Every substitution is bounded
    at ``_DOOR_REJECTION_MAX_CHARS`` and Meta caps the body at ``_FLOW_BODY_MAX_CHARS``, so the
    template's worst-case expansion — its fixed text plus every ``{reason}`` filled — must fit the
    body; otherwise the re-send would strand the participant with neither the Flow nor a message. A
    template over the cap is a permanent input refusal (:class:`ChannelInputError`), checked at
    deliver — never at inbound time, where a re-send Meta refuses has no recovery.
    """
    if template is None:
        return
    reason_count = template.count("{reason}")
    worst_case = len(template.replace("{reason}", "")) + reason_count * _DOOR_REJECTION_MAX_CHARS
    if worst_case > _FLOW_BODY_MAX_CHARS:
        raise ChannelInputError(
            f"wording.answer.rejected expands to up to {worst_case} characters once each of its "
            f"{reason_count} {{reason}} token(s) is filled (up to {_DOOR_REJECTION_MAX_CHARS} characters each); "
            f"WhatsApp caps the re-sent Flow body at Meta's {_FLOW_BODY_MAX_CHARS}-character limit"
        )


def _guard_rejected_final_template(template: str | None) -> None:
    """Refuse a caller ``wording.answer.rejected_final`` too long for the closing text message, up front.

    When a refused form answer reaches the re-send cap, the question closes with a text message: the
    caller's closing sentence with the door's line (bounded at ``_DOOR_REJECTION_MAX_CHARS``) in EACH
    ``{reason}`` token. Meta caps a text body at ``_TEXT_BODY_MAX_CHARS``, so the template's worst-case
    expansion must fit; a longer one is a permanent input refusal (:class:`ChannelInputError`), checked
    at deliver — never at inbound time, where nothing can recover the closing message.
    """
    if template is None:
        return
    reason_count = template.count("{reason}")
    worst_case = len(template.replace("{reason}", "")) + reason_count * _DOOR_REJECTION_MAX_CHARS
    if worst_case > _TEXT_BODY_MAX_CHARS:
        raise ChannelInputError(
            f"wording.answer.rejected_final expands to up to {worst_case} characters once each of its "
            f"{reason_count} {{reason}} token(s) is filled (up to {_DOOR_REJECTION_MAX_CHARS} characters each); "
            f"WhatsApp caps a text message body at Meta's {_TEXT_BODY_MAX_CHARS}-character limit"
        )


def _reverse_payload_labels(schema: dict[str, Any]) -> dict[str, str]:
    """The completion-payload-label → schema-key reverse map for a form schema.

    Inverts :func:`payload_labels` (schema key → human-readable payload label) — the single
    mapping definition both send paths, the re-send and the inbound decode share. The label
    map is injective, so the inversion is lossless. Stored on the pending record and the
    notify sidecar so the reply decode reads the map, never re-derives it.
    """
    return {label: key for key, label in payload_labels(schema["properties"]).items()}


def _form_pages_list(send: ChannelDelivery | ChannelNotification) -> list[dict[str, Any]] | None:
    """The form's step layout as plain JSON, or ``None`` when the send carried one page.

    Each page is ``{"title", "fields", "display", "kind"}`` — the display blocks (heading/body/
    image, each static or slotted) and the input/review kind ride alongside the field list, so
    the renderer draws display elements and a review screen. Shared by the form
    ask and the ask-less form notification, whose ``pages`` field is the identical shape.
    """
    if send.pages is None:
        return None
    return [
        {
            "title": page.title,
            "fields": list(page.fields),
            "kind": page.kind,
            "display": [block.model_dump(exclude_none=True) for block in page.display],
            **({"button_label": page.button_label} if page.button_label is not None else {}),
        }
        for page in send.pages
    ]


def _reactions_dict(reactions: FormReactions | None) -> dict[str, Any] | None:
    """The form's reaction triggers as plain JSON, or ``None`` when the form does not react.

    A reactions block with no trigger (no field change, page advance, or submit) is a static
    form, so it yields ``None`` — the Flow stays the navigate (endpoint-less) Flow.
    """
    if reactions is None or not reactions.has_trigger():
        return None
    return {
        "field_changed": list(reactions.field_changed),
        "page_advanced": list(reactions.page_advanced),
        "submitted": reactions.submitted,
        "choices": list(reactions.choices),
    }


def _require_reaction_settings(settings: WhatsAppSettings) -> str:
    """The data-endpoint URI an endpoint-driven form publishes against; raise loudly when key material is missing.

    An endpoint-driven form (one that reacts, carries a shape no Meta control checks natively, or
    draws a field per case) CANNOT publish or be served without the business private key (to
    decrypt the endpoint's requests) and the public endpoint URI (configured on the Flow so Meta
    routes data-exchange back here). Either unset is refused loudly — never a silent fall back to a
    static flow that would let a broken shape leave the form or leave a per-case answer unread. The
    public key must also be registered for the sending number (operator step; see
    :mod:`tai42_channel_whatsapp.provisioning_route`).
    """
    require_delivery_secret(settings.flow_private_key, "CHANNEL_WHATSAPP_FLOW_PRIVATE_KEY")
    return require_delivery_setting(settings.flow_endpoint_uri, "CHANNEL_WHATSAPP_FLOW_ENDPOINT_URI")


def _form_values_and_options(
    send: ChannelDelivery | ChannelNotification,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """The form's per-send ``values`` and ``options`` as plain JSON.

    Each option is ``{"value", "label"?, "description"?}`` (``label`` and the second-line
    ``description`` each omitted when absent). Empty when the send carried no data.
    Shared by the form ask and the ask-less form notification, whose ``data`` field is
    the identical shape.
    """
    if send.data is None:
        return {}, {}
    options: dict[str, list[dict[str, Any]]] = {}
    for name, choices in send.data.options.items():
        options[name] = [
            {
                "value": choice.value,
                **({"label": choice.label} if choice.label is not None else {}),
                **({"description": choice.description} if choice.description is not None else {}),
            }
            for choice in choices
        ]
    return dict(send.data.values), options


async def send_form_ask_flow(
    phone_number_id: str,
    to: str,
    body_text: str,
    flow_token: str,
    schema: dict[str, Any],
    pages: list[dict[str, Any]] | None,
    values: dict[str, Any],
    options: dict[str, list[dict[str, Any]]],
    reactions: dict[str, Any] | None = None,
    open_label: str | None = None,
    form_title: str | None = None,
    form_submit: str | None = None,
    field_invalid: dict[str, str] | None = None,
    boolean_words: dict[str, tuple[str, str]] | None = None,
) -> SentMessage:
    """Build, resolve and send a form ask's Flow; return its :class:`SentMessage`.

    The ONE sender for a form ask's Flow: the first send and every door-rejection
    re-send call it with the same inputs the pending record holds, so the re-send
    resolves the SAME published Flow through the same Flow identity — found on the WABA
    by name, else re-created, if the cache was lost, never raising a stale-lookup miss —
    and navigates to the entry screen with the same prefill and option lists the first
    send carried. An endpoint-driven form (``reactions`` declared, a shape no Meta control checks
    natively, or a field drawn per case — :func:`~tai42_channel_whatsapp.flows.form_needs_endpoint`)
    publishes the Flow against the configured data endpoint; the endpoint-driven flow and the
    endpoint URI fold into the Flow identity, so its published Flow is distinct from a static one and from one bound
    to another endpoint. ``field_invalid`` is the caller's per-field ``invalid`` sentence (the
    helper text a native pattern field without a second line shows); ``boolean_words`` the caller's
    words a review page reads a boolean back with (Flow JSON, so they re-key).

    ``open_label`` is the caller's text for the chat button that opens the Flow, drawn as the
    message ``flow_cta`` (not Flow JSON, so it never re-keys the Flow); ``form_title`` is the
    caller's screen title and ``form_submit`` the caller's terminal-button label for a page-less
    form (both Flow JSON, so they DO re-key). The re-send passes the stored values of all three, so
    the correction message draws the same button, title and submit label.
    """
    flow_json, schema_hash = build_form_flow(
        schema,
        pages,
        set(options),
        reactions,
        form_title,
        form_submit,
        field_invalid=field_invalid,
        boolean_words=boolean_words,
    )
    flow_data = build_flow_data(schema, values, options, pages)
    settings = whatsapp_settings()
    waba_id = require_delivery_setting(settings.waba_id, "CHANNEL_WHATSAPP_WABA_ID")
    endpoint = form_needs_endpoint(schema, pages, set(options), reactions)
    endpoint_uri = _require_reaction_settings(settings) if endpoint else None
    flow_id = await _resolve_flow_id(waba_id, schema_hash, flow_json, endpoint_uri)
    return await send_flow(
        phone_number_id=phone_number_id,
        to=to,
        body_text=body_text,
        flow_id=flow_id,
        flow_token=flow_token,
        screen=FORM_ENTRY_SCREEN,
        data=flow_data,
        open_label=open_label,
    )


async def _deliver_form(
    settings: WhatsAppSettings, phone_number_id: str, target: str, delivery: ChannelDelivery
) -> SentMessage:
    """Deliver a form ask as a WhatsApp Flow (Tier-2: reserve BEFORE send); return the Flow message.

    The Flow (one screen per page) and its per-send data are built and validated
    BEFORE any network work — an unsupported schema, an unmappable per-send option, or
    an unknown page field raises here, before the ``CHANNEL_WHATSAPP_WABA_ID`` gate, the
    reservation, or a send. The reservation carries the answer schema (so an inbound
    Flow response is coerced to its types), the question text (so a door-rejected answer
    is re-asked), the per-send pages/values/options (so a re-send reproduces the same
    Flow) and the payload-label reverse map (so the reply's label-keyed answers map
    back to the schema keys), and uses the ``interaction_id`` as the ``flow_token`` correlating the
    completed form. The published Flow is keyed by the ``(schema, pages, option_fields)``
    triple, the emitted shape (the option-bearing fields decide which string
    properties render as dropdowns) and the data-endpoint URI, and REUSED across sends
    (found on the WABA by name, else created, and cached under the WABA id); the prefilled values and
    per-send option lists ride the send's ``flow_action_payload.data`` (a dynamic
    data-source), never a new Flow. An ENDPOINT-DRIVEN form (one carrying ``reactions`` triggers
    or a shape no Meta control checks natively) publishes the Flow endpoint-driven and refuses
    loudly, before any reservation, when the data-endpoint key material is missing
    (:func:`_require_reaction_settings`); its :class:`~tai42_channel_whatsapp.correlation.EndpointForm`
    (schema, pages, reactions, option fields, the caller's ``invalid`` and ``required`` sentences and
    its ``error_line`` / ``update_failed`` lines) is cached under the ``interaction_id`` so the data
    endpoint can check its steps. A caller ``invalid`` or ``required`` sentence over its control's
    ``error-message`` cap, an ``error_line`` too long for the re-sent body and a ``rejected_final``
    too long for the closing text message are refused before any reservation. The send goes
    through the one sender :func:`send_form_ask_flow`; a failure writing the sidecar, resolving
    the Flow or sending releases the reservation, drops the sidecar and raises — never a fallback
    format.
    """
    if delivery.schema is None:
        raise ChannelDeliveryError(f"form delivery {delivery.interaction_id} is missing its schema")
    pages = _form_pages_list(delivery)
    values, options = _form_values_and_options(delivery)
    reactions = _reactions_dict(delivery.reactions)
    form = _form_wording(delivery)
    open_label = form.open if form is not None else None
    form_title = form.title if form is not None else None
    form_submit = form.submit if form is not None else None
    answer = delivery.wording.answer if delivery.wording is not None else None
    rejected_template = answer.rejected if answer is not None else None
    rejected_final_template = answer.rejected_final if answer is not None else None
    # Build + validate before any reservation (raises on an unsupported schema, an
    # unmappable per-send option, an unknown page field, an unrenderable reaction trigger, an
    # over-cap open-button label / footer label / per-field sentence / error line / readback line,
    # or an over-cap refusal template), so an unrenderable send reserves nothing.
    field_invalid = _field_words(form, "invalid")
    field_required = _field_words(form, "required")
    boolean_words = _boolean_words(form)
    lines = _form_lines(form)
    _guard_open_label(open_label)
    _guard_rejected_template(rejected_template)
    _guard_rejected_final_template(rejected_final_template)
    _guard_ask_error_line(lines)
    _guard_field_sentences(delivery.schema, set(options), "invalid", field_invalid)
    _guard_field_sentences(delivery.schema, set(options), "required", field_required)
    build_form_flow(
        delivery.schema,
        pages,
        set(options),
        reactions,
        form_title,
        form_submit,
        field_invalid=field_invalid,
        boolean_words=boolean_words,
    )
    build_flow_data(delivery.schema, values, options, pages)
    require_delivery_setting(settings.waba_id, "CHANNEL_WHATSAPP_WABA_ID")
    # An endpoint-driven form cannot publish or be served without its data-endpoint key material —
    # refuse loudly up front (never a silent fall back to a static flow).
    endpoint = form_needs_endpoint(delivery.schema, pages, set(options), reactions)
    if endpoint:
        _require_reaction_settings(settings)

    await reserve_pending(
        phone_number_id=phone_number_id,
        wa_id=target,
        callback_url=delivery.callback_url,
        timeout_at=delivery.timeout_at,
        interaction_id=delivery.interaction_id,
        schema=delivery.schema,
        question=delivery.question,
        form_pages=pages,
        form_values=values,
        form_options=options,
        form_names=_reverse_payload_labels(delivery.schema),
        form_reactions=reactions,
        on_mismatch=delivery.on_mismatch,
        wording=delivery.wording,
    )
    try:
        # An endpoint-driven form's data endpoint recovers its schema and inputs from this sidecar
        # by the flow token (the interaction id); it expires with the ask's own deadline.
        if endpoint:
            endpoint_form = EndpointForm(
                schema=delivery.schema,
                pages=pages,
                reactions=reactions,
                option_fields=sorted(options),
                invalid=field_invalid,
                required=field_required,
                lines=lines,
            )
            await cache_endpoint_form(delivery.interaction_id, endpoint_form, delivery.timeout_at)
        sent = await send_form_ask_flow(
            phone_number_id=phone_number_id,
            to=target,
            body_text=delivery.question,
            flow_token=delivery.interaction_id,
            schema=delivery.schema,
            pages=pages,
            values=values,
            options=options,
            reactions=reactions,
            open_label=open_label,
            form_title=form_title,
            form_submit=form_submit,
            field_invalid=field_invalid,
            boolean_words=boolean_words,
        )
    except Exception:
        # Any sidecar-write/create/publish/store/send failure frees the pair instead of holding it
        # until its TTL, and drops the sidecar (the id is this ask's own, so no other ask's entry
        # is touched; a missing one is a no-op); the original error then propagates.
        try:
            await release_pending(phone_number_id=phone_number_id, wa_id=target)
        finally:
            if endpoint:
                await drop_endpoint_form(delivery.interaction_id)
        raise
    return sent


async def _send_form_notification(
    settings: WhatsAppSettings, phone_number_id: str, target: str, notification: ChannelNotification
) -> list[SentMessage]:
    """Send an ask-less form notification, returning every :class:`SentMessage` in send order.

    Any display media rides as the standard prelude (the ``link`` items as one
    text line-block, each ``image`` as its own message), then the Flow message
    LAST — the actionable prompt stays at the foot of the chat. The Flow is
    resolved exactly like a form ask's (one published Flow per answer
    schema, found on the WABA by name, else created, and cached under the WABA id),
    and the answer schema itself (with its
    payload-label reverse map) is cached beside
    the flow id — the submission's reply carries only the schema hash inside its
    flow token, so that sidecar is the ONLY place the inbound side can recover the
    schema and map to decode and coerce the values (see :func:`cache_flow_form`). NO correlation is
    reserved: the token — minted in the ``tai42-nf:`` namespace as prefix + schema
    hash + a random suffix — routes the reply, not the pair, so any number of forms
    may be outstanding and a pending ask on the same pair is never touched.

    An ENDPOINT-DRIVEN form (one carrying a shape no Meta control checks natively) publishes
    against the data endpoint, which reads this sidecar's pages and option fields to check each
    step; the caller's ``invalid`` and ``required`` sentences ride the send data as ``__invalid`` /
    ``__required`` carriers and its ``error_line`` / ``update_failed`` lines as the form-line
    carriers (the sidecar is per schema hash, never per send). It refuses loudly, before any send,
    when the data-endpoint key material is missing, and a caller ``invalid`` or ``required``
    sentence over its control's ``error-message`` cap is refused before any send too.

    A caller-set ``form_tag`` rides the token as a 4th ``:``-delimited segment
    (``tai42-nf:{hash}:{nonce}:{form_tag}``) when set — its charset excludes ``:`` so it
    stays one segment. It NEVER enters the published Flow artefact, the schema hash, the
    cached flow id or the send's data, so it triggers no republish and leaves the artefact
    recipient-data-free; the reply side reads it straight back off the returned token.
    """
    if notification.schema is None:  # dispatch guard; notify() branches on the field
        raise ChannelDeliveryError("form notification is missing its schema")
    # Mirror the form ask: the per-send Flow (one screen per page) and its prefill/option
    # data are built and validated BEFORE any network work, and the send navigates to the
    # entry screen injecting that data — so an ask-less form opens already filled in. The
    # published Flow is keyed by the ``(schema, pages, option_fields)`` triple and reused
    # across sends; the prefilled values and per-send option lists ride the send's data.
    pages = _form_pages_list(notification)
    values, options = _form_values_and_options(notification)
    form = _form_wording(notification)
    open_label = form.open if form is not None else None
    form_title = form.title if form is not None else None
    form_submit = form.submit if form is not None else None
    field_invalid = _field_words(form, "invalid")
    field_required = _field_words(form, "required")
    _guard_open_label(open_label)
    _guard_field_sentences(notification.schema, set(options), "invalid", field_invalid)
    _guard_field_sentences(notification.schema, set(options), "required", field_required)
    endpoint = form_needs_endpoint(notification.schema, pages, set(options))
    flow_json, schema_hash = build_form_flow(
        notification.schema,
        pages,
        set(options),
        form_title=form_title,
        form_submit=form_submit,
        field_invalid=field_invalid,
        carry_invalid=endpoint,
        boolean_words=_boolean_words(form),
    )
    flow_data = build_flow_data(
        notification.schema,
        values,
        options,
        pages,
        field_invalid=field_invalid,
        carry_invalid=endpoint,
        field_required=field_required,
        form_lines=_form_lines(form),
    )
    waba_id = require_delivery_setting(settings.waba_id, "CHANNEL_WHATSAPP_WABA_ID")
    endpoint_uri = _require_reaction_settings(settings) if endpoint else None

    sent = await _send_media_prelude(phone_number_id, target, list(notification.media or []))
    flow_id = await _resolve_flow_id(waba_id, schema_hash, flow_json, endpoint_uri)
    # Written beside every flow-id use — the reply side cannot repopulate it. The
    # payload-label reverse map rides with the schema so the reply decodes to schema keys; the
    # pages and option fields let the data endpoint check an endpoint-driven form's steps.
    await cache_flow_form(
        waba_id,
        schema_hash,
        notification.schema,
        _reverse_payload_labels(notification.schema),
        pages,
        sorted(options),
    )
    flow_token = f"{_NOTIFY_FORM_TOKEN_PREFIX}{schema_hash}:{uuid4().hex}"
    if notification.form_tag is not None:
        # The caller-set tag rides as the 4th token segment; it never touches the Flow
        # artefact, the hash or the send data, so no republish and no recipient data in the artefact.
        flow_token = f"{flow_token}:{notification.form_tag}"
    sent.append(
        await send_flow(
            phone_number_id=phone_number_id,
            to=target,
            body_text=notification.message,
            flow_id=flow_id,
            flow_token=flow_token,
            screen=FORM_ENTRY_SCREEN,
            data=flow_data,
            open_label=open_label,
        )
    )
    return sent
