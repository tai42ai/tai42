"""One ``data_exchange`` step of an endpoint-driven WhatsApp form, translated onto the platform's seams.

This module is a pure TRANSLATOR between Meta's decrypted step request and the platform's two form
seams — the one form-shape check (``tai42_app.interactions.form_shape_errors``) and the one reaction
chokepoint (``tai42_app.interactions.react``) — it holds no business logic:

* recover the form from its ``flow_token`` (an ask's interaction id → its
  :class:`~tai42_channel_whatsapp.correlation.EndpointForm` sidecar; an ask-less ``tai42-nf:``
  token → the notify sidecar of its schema hash; an unknown or expired token raises
  :class:`FlowTokenInvalidError`), read each per-case field's answer from its shown case, and
  coerce the values filled so far (:mod:`~tai42_channel_whatsapp.inbound.step_values`, over the
  form's screen geometry of :mod:`~tai42_channel_whatsapp.inbound.form_geometry`). A step advance
  checks THIS screen's fields (nothing demanded), a submit the whole answer; a broken shape keeps
  the person on the screen with each failing field's sentence bound to its ``error-message`` and a
  ``Label: sentence`` snackbar. Only a clean check
  runs a DECLARED consumer reaction, whose FORM UPDATE is translated into the vendor
  ``{screen, data}`` response (per-field errors → the snackbar; values/options/display → the screen
  data, a ``None`` value emptying its control). The ``data`` of a response becomes the screen's
  state, so every answer carries a screen's whole data: one that keeps the person on the screen
  starts from the whole data the action carried, each control holding the value the person left
  in it; a clean advance names the next screen, built from the carriers the request carried; a
  clean submit completes the Flow;
* anything a seam raises (closed/malformed/handler-fail/deadline) surfaces as the vendor's error
  surface (a screen ``error_message``) plus a LOUD log — never a stale or silent value.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelInputError
from tai42_contract.interactions import (
    AnswerFormat,
    FieldChangedEvent,
    FormReactionError,
    FormReactionEvent,
    FormReactionEventKind,
    FormReactions,
    FormShapeError,
    FormShapeKind,
    PageAdvancedEvent,
    QuestionFormat,
    SubmittedEvent,
)
from tai42_kit.interactions import display_slot_text, offered_picks

from tai42_channel_whatsapp.channel.forms import NOTIFY_FORM_TOKEN_PREFIX
from tai42_channel_whatsapp.correlation import CachedFlowForm, EndpointForm, get_cached_flow_form, get_endpoint_form
from tai42_channel_whatsapp.flow_pictures import embed_display_pictures
from tai42_channel_whatsapp.flows import FORM_ENTRY_SCREEN, SUCCESS_SCREEN, page_screen_id
from tai42_channel_whatsapp.flows_components import component_names, payload_labels
from tai42_channel_whatsapp.flows_data_budget import require_screen_data_fits
from tai42_channel_whatsapp.flows_dates import (
    forwarded_bound_carriers,
    schema_with_sent_date_bounds,
)
from tai42_channel_whatsapp.flows_endpoint import (
    FORM_LINE_MEMBERS,
    REACTION_EVENT_KEY,
    REACTION_FIELD_KEY,
    REACTION_PAGE_KEY,
    RESERVED_REACTION_KEYS,
    error_carriers,
    error_fields,
    form_line_carrier,
)
from tai42_channel_whatsapp.flows_resolve import (
    form_drawing_order,
    pages_without_picture_content,
    slot_datanames,
    slot_keys,
    slot_start_data,
)
from tai42_channel_whatsapp.flows_second_line import data_source_rows
from tai42_channel_whatsapp.flows_send_data import field_init_value, range_init_value
from tai42_channel_whatsapp.flows_shapes import channel_word
from tai42_channel_whatsapp.inbound.form_geometry import form_geometry, screen_fields
from tai42_channel_whatsapp.inbound.forms import coerce_value, parse_notify_form_token
from tai42_channel_whatsapp.inbound.step_values import (
    answer_with_update,
    filled_values,
    partial_values,
    range_pair_values,
    read_split_answers,
)
from tai42_channel_whatsapp.settings import whatsapp_settings

logger = logging.getLogger(__name__)

# The generic notice shown to the participant (via a screen ``error_message``) when a reaction
# or the shape check cannot be computed — the vendor's error surface, paired with a loud operator
# log — unless the caller set the form's ``update_failed`` line. A broken shape is never this
# notice: it names the field.
_REACTION_FAILURE_NOTICE = "Sorry, this form could not be updated right now. Please try again."


class FlowTokenInvalidError(Exception):
    """The request's ``flow_token`` names no open endpoint-driven form (the HTTP door answers 427)."""


async def _resolve_form(flow_token: str) -> tuple[EndpointForm, CachedFlowForm | None]:
    """The endpoint-driven form a flow token names, with its ask-less sidecar (``None`` for an ask).

    An ask's token is its interaction id, resolving its :class:`EndpointForm` sidecar; its reactions
    may run. An ask-less form's ``tai42-nf:`` token carries its schema hash, resolving the notify
    sidecar under the configured WABA; such a form reacts to nothing, and its sentences, lines and
    date bound values ride the request. Raises :class:`FlowTokenInvalidError` on a miss.
    """
    if flow_token.startswith(NOTIFY_FORM_TOKEN_PREFIX):
        schema_hash, _ = parse_notify_form_token(flow_token)
        waba_id = whatsapp_settings().waba_id
        cached = await get_cached_flow_form(waba_id, schema_hash) if waba_id else None
        if cached is None:
            logger.warning("whatsapp flow endpoint: no form sidecar for notify token %s", flow_token)
            raise FlowTokenInvalidError
        form = EndpointForm(
            schema=cached.schema,
            pages=cached.pages,
            reactions=None,
            option_fields=cached.option_fields,
            invalid={},
            required={},
            lines={},
        )
        return form, cached
    endpoint_form = await get_endpoint_form(flow_token)
    if endpoint_form is None:
        logger.warning("whatsapp flow endpoint: no open endpoint-driven form for flow token %s", flow_token)
        raise FlowTokenInvalidError
    return endpoint_form, None


async def handle_data_exchange(payload: dict[str, Any]) -> dict[str, Any]:
    """Run one ``data_exchange``: check the shapes, run any declared reaction, and answer the vendor."""
    flow_token = payload.get("flow_token")
    screen = payload.get("screen")
    screen = screen if isinstance(screen, str) and screen else FORM_ENTRY_SCREEN
    data = payload.get("data")
    data = data if isinstance(data, dict) else {}
    if not isinstance(flow_token, str) or not flow_token:
        raise FlowTokenInvalidError
    form, askless_sidecar = await _resolve_form(flow_token)
    # An ask-less endpoint-driven form declares every date field's bound carriers on every screen.
    every_screen = askless_sidecar is not None
    data = read_split_answers(form, screen, data)
    properties = form.schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    reactions = (
        FormReactions.model_validate(form.reactions) if askless_sidecar is None and form.reactions else FormReactions()
    )

    event = _build_event(data)
    if event is None:
        logger.error("whatsapp flow request for %s carried no recognised event marker", flow_token)
        return _error_response(form, screen, data, "")
    partial = partial_values(data, properties)
    try:
        if askless_sidecar is not None:
            form = _with_sent_date_bounds(form, askless_sidecar.date_bounds, data)
        if isinstance(event, FieldChangedEvent):
            return await _answer_field_change(event, reactions, screen, flow_token, form, partial, data)
        refusal = _shape_refusal(form, screen, event, partial, data)
        if refusal is not None:
            return refusal
        update: dict[str, Any] = {}
        declared = (isinstance(event, SubmittedEvent) and reactions.submitted) or (
            isinstance(event, PageAdvancedEvent) and event.page in reactions.page_advanced
        )
        if declared:
            update = await _react(flow_token, event, partial, form, data)
        return await _translate_update(
            event, update, screen, flow_token, form, partial, data, every_screen=every_screen
        )
    except FormReactionError as exc:
        # The reaction door refused or failed; its reason fills the caller's ``update_failed``.
        logger.exception("whatsapp flow request for %s: the reaction failed; surfacing the vendor error", flow_token)
        return _error_response(form, screen, data, exc.reason)
    except ChannelInputError:
        # A date bound carrier the request lacks or echoes in a shape the send never gave it, or a reaction
        # result this channel cannot render (e.g. an option second line past Meta's cap), is the same
        # class as a failed reaction: logged loudly, answered with the vendor error. Its text is this
        # channel's own vendor message, never shown to the person.
        logger.exception(
            "whatsapp flow request for %s carried a malformed date bound or an update WhatsApp cannot render",
            flow_token,
        )
        return _error_response(form, screen, data, "")
    except Exception:
        logger.exception("whatsapp flow request for %s failed; surfacing the vendor error", flow_token)
        return _error_response(form, screen, data, "")


async def _answer_field_change(
    event: FieldChangedEvent,
    reactions: FormReactions,
    screen: str,
    flow_token: str,
    form: EndpointForm,
    partial: dict[str, Any],
    data: dict[str, Any],
) -> dict[str, Any]:
    """The same-screen answer to a ``field_changed`` request: the reaction's update applied to ``screen``.

    A field the form declares no ``field_changed`` reaction for is logged and answered with the
    vendor error.
    """
    if event.field not in reactions.field_changed:
        logger.error("whatsapp flow request for %s names undeclared field trigger %r", flow_token, event.field)
        return _error_response(form, screen, data, "")
    update = await _react(flow_token, event, partial, form, data)
    return _fitting(
        form,
        {"screen": screen, "data": _screen_answer(form, screen, data, await _update_data(update, form, data))},
    )


def _with_sent_date_bounds(form: EndpointForm, declared: dict[str, list[str]], data: dict[str, Any]) -> EndpointForm:
    """An ask-less ``form`` whose schema carries the date bounds its send gave, read back from ``data``.

    The sidecar schema holds no bound values (each send carries them), so the shape check and every
    later step read the bounds ``declared`` (the sidecar's per-field bound keys, fixed per published
    Flow) from the carriers the request echoes
    (:func:`~tai42_channel_whatsapp.flows_dates.schema_with_sent_date_bounds`). Raises
    ``ChannelInputError`` naming a declared carrier the request lacks or echoes in another shape.
    """
    properties, _, _, merges = form_geometry(form)
    schema = schema_with_sent_date_bounds(form.schema, component_names(properties), merges, declared, data)
    return dataclasses.replace(form, schema=schema)


async def _react(
    flow_token: str, event: FormReactionEvent, partial: dict[str, Any], form: EndpointForm, data: dict[str, Any]
) -> dict[str, Any]:
    """Run the consumer's reaction; its FORM UPDATE (empty when it returned none) over the picks ``data`` holds.

    The update comes back with the held picks its replaced choice lists no longer offer dropped
    (:func:`_with_dropped_picks`), so every answer built from it — the same screen, the next one, or
    a submit's completion — reads the same fold.
    """
    update = await tai42_app.interactions.react(flow_token, event, partial) or {}
    return _with_dropped_picks(update, form, data)


def _with_dropped_picks(update: dict[str, Any], form: EndpointForm, data: dict[str, Any]) -> dict[str, Any]:
    """``update`` with each held pick its new choice lists no longer offer dropped before its own values.

    A field the update gives a new list holds the pick the request carried for it — its value, or
    for a field on a later screen its ``__init`` carrier — read as its schema type. What the new
    list no longer offers (:func:`~tai42_kit.interactions.offered_picks`) becomes a value of the
    update: a single pick ``None`` (cleared), a multiple pick the entries the list still offers. The
    update's own values are laid over those, never filtered.
    """
    options = update.get("options") or {}
    if not options:
        return update
    properties = form.schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    names = component_names(properties)
    held: dict[str, Any] = {}
    for name in options:
        prop = properties.get(name)
        if not isinstance(prop, dict):
            continue
        value = coerce_value(data[name] if name in data else data.get(f"{names[name]}__init"), prop)
        if value not in (None, "", []):
            held[name] = value
    kept = offered_picks(held, options)
    dropped = {name: kept.get(name) for name, value in held.items() if kept.get(name) != value}
    if not dropped:
        return update
    return {**update, "values": {**dropped, **(update.get("values") or {})}}


def _fitting(form: EndpointForm, answer: dict[str, Any]) -> dict[str, Any]:
    """``answer``, which keeps ``form`` open, once its data fits the form's data budget.

    Raises ``ChannelInputError`` when it does not (:func:`require_screen_data_fits`).
    """
    require_screen_data_fits(form.pages, answer["data"], endpoint=True)
    return answer


def _build_event(data: dict[str, Any]) -> FormReactionEvent | None:
    """The EVENT a ``data_exchange`` payload names via its markers, or ``None`` when malformed."""
    kind = data.get(REACTION_EVENT_KEY)
    if kind == FormReactionEventKind.FIELD_CHANGED:
        field = data.get(REACTION_FIELD_KEY)
        return FieldChangedEvent(field=field) if isinstance(field, str) and field else None
    if kind == FormReactionEventKind.PAGE_ADVANCED:
        page = data.get(REACTION_PAGE_KEY)
        return PageAdvancedEvent(page=page) if isinstance(page, str) and page else None
    if kind == FormReactionEventKind.SUBMITTED:
        return SubmittedEvent()
    return None


def _error_response(form: EndpointForm, screen: str, data: dict[str, Any], reason: str) -> dict[str, Any]:
    """Keep the form on its current screen, as the person left it, and show the could-not-update notice.

    The notice is the caller's ``update_failed`` line with each ``{reason}`` replaced by ``reason``
    (the reaction door's own sentence, or empty when the failure carries none a person may read),
    else the generic :data:`_REACTION_FAILURE_NOTICE`.
    """
    template = _form_line(form, data, "update_failed")
    notice = template.replace("{reason}", reason) if template is not None else _REACTION_FAILURE_NOTICE
    return {"screen": screen, "data": _screen_answer(form, screen, data, {"error_message": notice})}


def _form_line(form: EndpointForm, data: dict[str, Any], member: str) -> str | None:
    """The caller's form-level ``member`` line: an ask's sidecar, else the carrier an ask-less form forwarded.

    ``None`` when the caller set none (an ask-less form carries the empty string for an unset line).
    """
    own = form.lines.get(member)
    if own:
        return own
    carried = data.get(form_line_carrier(member))
    return carried if isinstance(carried, str) and carried else None


def _error_line(form: EndpointForm, data: dict[str, Any], items: list[tuple[str, str]]) -> str:
    """A refusal's pop-up summary: the caller's ``error_line``, else ``Label: sentence`` per failing field."""
    line = _form_line(form, data, "error_line")
    if line is not None:
        return line
    return "; ".join(f"{label}: {text}" for label, text in items)


def _screen_answer(
    form: EndpointForm,
    screen: str,
    data: dict[str, Any],
    overrides: dict[str, Any],
    *,
    clear_errors: bool = False,
) -> dict[str, Any]:
    """The ``data`` of an answer that keeps the person on ``screen``: the screen's whole data.

    The data an answer carries becomes the screen's state, so it starts from the screen's whole data
    the action carried (every request key but the values and the event markers). Each of the
    screen's controls gets its ``__init`` set to the value the person left in it (a merged range
    END has none — its START's value is the pair's object), so the answer never puts back a value
    they changed. With ``clear_errors`` (the screen's values passed the shape check) each of its
    ``error-message`` bindings is cleared (a merged range's to the object of two empty strings).
    ``overrides`` apply last.
    """
    properties = form.schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    names = component_names(properties)
    fields_by_screen, current = screen_fields(form, screen)
    this_fields = fields_by_screen[current] if current is not None else []
    out = {key: value for key, value in data.items() if key not in properties and key not in RESERVED_REACTION_KEYS}
    for name in this_fields:
        if name in data:
            out[f"{names[name]}__init"] = data[name]
    if clear_errors:
        bound, range_merges = _error_bindings(form, properties, names)
        cleared = error_carriers(this_fields, names, bound, range_merges, {})
        out.update({key: value for key, value in cleared.items() if key in out})
    out.update(overrides)
    return out


def _error_bindings(
    form: EndpointForm, properties: dict[str, Any], names: dict[str, str]
) -> tuple[frozenset[str], dict[str, str]]:
    # The fields binding an ``error-message`` and the form's ``END -> START`` range merge map.
    _, _, _, range_merges = form_geometry(form)
    required_raw = form.schema.get("required")
    required = {str(name) for name in required_raw} if isinstance(required_raw, list) else set()
    return error_fields(properties, required, set(form.option_fields), names, set(range_merges)), range_merges


def _sentence(form: EndpointForm, data: dict[str, Any], names: dict[str, str], name: str, member: str) -> str | None:
    # The caller's ``invalid`` or ``required`` sentence (``member``) for a field: an ask's sidecar,
    # else the ``__invalid`` / ``__required`` carrier an ask-less form's request forwarded; ``None``
    # when the caller set none.
    own = (form.required if member == "required" else form.invalid).get(name)
    if own:
        return own
    carried = data.get(f"{names[name]}__{member}")
    return carried if isinstance(carried, str) and carried else None


def _shape_refusal(
    form: EndpointForm, screen: str, event: FormReactionEvent, partial: dict[str, Any], data: dict[str, Any]
) -> dict[str, Any] | None:
    """The same-screen refusal for a step or submit whose filled values break a declared shape, else ``None``."""
    errors = _shape_errors(form, screen, event, filled_values(partial), folded=False)
    return _refusal(form, screen, data, errors, {}) if errors else None


def _shape_errors(
    form: EndpointForm, screen: str, event: FormReactionEvent, answer: dict[str, Any], *, folded: bool
) -> list[FormShapeError]:
    """Each field of ``answer`` that breaks a declared shape, as a step or a submit is checked.

    A step advance checks THIS screen's fields: the person's own values (``folded`` False) with
    nothing demanded, as WhatsApp demands the screen's required controls itself; the answer a
    reaction folded (``folded`` True) with the screen's required fields demanded, as the reaction
    can withdraw a pick. A submit checks the whole answer. A picker's membership in a per-send
    option list is never refused here (the list it drew is the list it offers; the answer door
    judges membership against the stored list).
    """
    question = QuestionFormat(
        answer_format=AnswerFormat.FORM,
        format_payload={"schema": form.schema, **({"reactions": form.reactions} if form.reactions else {})},
    )
    if isinstance(event, PageAdvancedEvent):
        fields_by_screen, current = screen_fields(form, screen)
        this_fields = fields_by_screen[current] if current is not None else []
        errors = tai42_app.interactions.form_shape_errors(question, answer, fields=this_fields, partial=not folded)
    else:
        errors = tai42_app.interactions.form_shape_errors(question, answer)
    return [error for error in errors if not (error.kind is FormShapeKind.ENUM and error.field in form.option_fields)]


def _refusal(
    form: EndpointForm,
    screen: str,
    data: dict[str, Any],
    errors: list[FormShapeError],
    update_data: dict[str, Any],
) -> dict[str, Any]:
    """Keep the form on ``screen`` with each of ``errors`` shown, over the screen data ``update_data`` sets.

    Each failing field on the screen shows its sentence — the caller's ``required`` for a missing
    value or ``invalid`` for any other shape, else this channel's word for the shape — in its
    ``error-message`` (a merged range end's on its own end of the pair's one error object); every
    other field's is cleared; the snackbar is the caller's ``error_line``, else each failing field's
    label and sentence (:func:`_error_line`). The answer carries the screen's whole data
    (:func:`_screen_answer`) with ``update_data`` (a reaction's update, :func:`_update_data`) laid on
    before the sentences.
    """
    properties = form.schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    fields_by_screen, current = screen_fields(form, screen)
    this_fields = fields_by_screen[current] if current is not None else []
    names = component_names(properties)
    labels = payload_labels(properties)
    bound, range_merges = _error_bindings(form, properties, names)
    shown: dict[str, str] = {}
    items: list[tuple[str, str]] = []
    for error in errors:
        member = "required" if error.kind is FormShapeKind.REQUIRED else "invalid"
        sentence = _sentence(form, data, names, error.field, member)
        if error.field in this_fields:
            shown[error.field] = sentence or channel_word(error.kind, error.params)
        items.append((labels.get(error.field, error.field), sentence or error.message))
    out: dict[str, Any] = {**update_data, **error_carriers(this_fields, names, bound, range_merges, shown)}
    out["error_message"] = _error_line(form, data, items)
    return {"screen": screen, "data": _screen_answer(form, screen, data, out)}


async def _translate_update(
    event: FormReactionEvent,
    update: dict[str, Any],
    screen: str,
    flow_token: str,
    form: EndpointForm,
    partial: dict[str, Any],
    data: dict[str, Any],
    every_screen: bool,
) -> dict[str, Any]:
    """Answer a clean step or submit, with any reaction's FORM UPDATE applied.

    An update that sets values is checked again on the answer it leaves (:func:`answer_with_update`),
    a step's with the screen's required fields demanded (:func:`_shape_errors`): a broken shape or a
    missing required value keeps the form on its screen, built from the update, with each failing
    field's sentence (:func:`_refusal`). Otherwise a clean
    ``submitted`` completes the Flow (the terminal SUCCESS response, carrying the answer
    as the completion ``params``); reaction errors keep the form on its screen with an
    ``error_message``. A clean ``page_advanced`` advances to the next screen, built from the
    carriers the request carried with the update applied; on the last screen it stays. Staying,
    the answer is the screen's whole data with its field sentences cleared (its values passed the
    shape check) and the update applied. ``every_screen`` is True for an ask-less form, whose every
    screen declares every date field's bound carriers.
    """
    errors = update.get("errors") or {}
    answer = answer_with_update(partial, update)
    if update.get("values") and not errors:
        refused = _shape_errors(form, screen, event, answer, folded=True)
        if refused:
            return _fitting(form, _refusal(form, screen, data, refused, await _update_data(update, form, data)))
    if isinstance(event, SubmittedEvent) and not errors:
        properties, _, _, merges = form_geometry(form)
        return _completion(flow_token, properties, payload_labels(properties), merges, answer)
    if isinstance(event, PageAdvancedEvent) and not errors:
        advanced = await _advance_screen(screen, form, data, update, every_screen)
        if advanced is not None:
            return _fitting(form, advanced)
    return _fitting(
        form,
        {
            "screen": screen,
            "data": _screen_answer(form, screen, data, await _update_data(update, form, data), clear_errors=True),
        },
    )


async def _update_data(update: dict[str, Any], form: EndpointForm, data: dict[str, Any]) -> dict[str, Any]:
    """The screen-``data`` keys a FORM UPDATE sets: field values, option lists, display slots, error message.

    A field value is shaped for its control by the one shaper the send uses (a ``None`` value is the
    control's empty default); a member of a merged range pair re-shapes the START's pair object over
    the pair the request carried, and the END writes nothing of its own. A display value on an image
    slot that is a link is fetched, and one that is a stored-media reference is read, and embedded
    (:func:`embed_display_pictures`). A slot's data is a string: a ``None`` value leaves the slot
    unfilled (a text block's own text, or empty; an image's empty source, so its alt text shows),
    and a number or boolean is written as the web widget writes it
    (:func:`~tai42_kit.interactions.display_slot_text`). The error message of an update carrying
    field errors is the caller's ``error_line`` (an ask's sidecar or the carrier of ``data``, the
    request's), else ``Label: message`` per field.
    """
    properties, _, _, range_merges = form_geometry(form)
    names = component_names(properties)
    labels = payload_labels(properties)
    slots = slot_datanames(form.pages)
    update_values = update.get("values") or {}
    out: dict[str, Any] = {}
    for end, start in range_merges.items():
        if start in update_values or end in update_values:
            out[f"{names[start]}__init"] = range_init_value(
                start, end, range_pair_values(start, end, properties, data, update_values)
            )
    for name in update_values:
        if name in range_merges or name in range_merges.values():
            continue
        prop = properties.get(name)
        out[f"{names[name]}__init"] = field_init_value(name, prop if isinstance(prop, dict) else {}, update_values)
    for name, option_list in (update.get("options") or {}).items():
        prop = properties.get(name)
        prop = prop if isinstance(prop, dict) else {}
        out[f"{names[name]}__ds"] = data_source_rows(name, prop, option_list)
    unfilled = slot_start_data(pages_without_picture_content(form.pages) or [], slots)
    for slot, value in (await embed_display_pictures(update.get("display"), form.pages) or {}).items():
        if slot in slots:
            key = slots[slot]
            out[key] = unfilled[key] if value is None else display_slot_text(value)
    errors = update.get("errors") or {}
    if errors:
        out["error_message"] = _error_line(
            form, data, [(labels.get(str(field), str(field)), str(message)) for field, message in errors.items()]
        )
    return out


def _completion(
    flow_token: str,
    properties: dict[str, Any],
    labels: dict[str, str],
    merges: dict[str, str],
    answer: dict[str, Any],
) -> dict[str, Any]:
    """The terminal SUCCESS response that completes an endpoint-driven Flow and sends the answer back.

    The completed answer rides ``extension_message_response.params``, keyed by the same
    human-readable labels the static Flow's completion uses, so the inbound ``nfm_reply`` decode
    maps them back to the schema keys uniformly; ``params.flow_token`` is the request's own token
    (an ask's interaction id, or an ask-less form's ``tai42-nf:`` token). ``answer`` is the values
    filled with any the submit reaction set laid over them (:func:`answer_with_update`); a field the
    reaction cleared is not in it. The label keys come in the Flow's drawing
    order (:func:`form_drawing_order` over ``merges``, the form's ``END -> START`` range merges):
    each ``timeOf`` time right after its date, and a merged range END with its time right after
    the START's time.
    """
    params: dict[str, Any] = {"flow_token": flow_token}
    for name in form_drawing_order(list(properties), properties, merges):
        if name in answer:
            params[labels[name]] = answer[name]
    return {"screen": SUCCESS_SCREEN, "data": {"extension_message_response": {"params": params}}}


def _forwarded_carriers(
    data: dict[str, Any],
    names: dict[str, str],
    later_fields: list[str],
    later_slots: list[str],
    bound_carriers: list[str],
) -> dict[str, Any]:
    # The carriers the next screen declares that the request carried — each later field's
    # ``__init``/``__ds``/``__err``, every ``__invalid`` and ``__required``, the form-line carriers,
    # the later pages' display slots and the date ``bound_carriers`` it declares — passed on
    # verbatim. The rest of the screen's data (its own fields' carriers, the ``__val`` carriers
    # rebuilt here, its own page's slots) is not the next screen's.
    keys = {f"{names[name]}{suffix}" for name in later_fields for suffix in ("__init", "__ds", "__err")}
    keys |= {f"{cname}{suffix}" for cname in names.values() for suffix in ("__invalid", "__required")}
    keys |= {form_line_carrier(member) for member in FORM_LINE_MEMBERS}
    keys |= set(later_slots)
    keys |= set(bound_carriers)
    return {key: value for key, value in data.items() if key in keys}


async def _advance_screen(
    screen: str, form: EndpointForm, data: dict[str, Any], update: dict[str, Any], every_screen: bool
) -> dict[str, Any] | None:
    """The next-screen response for a clean page advance, or ``None`` when there is no next screen.

    Stateless: the next screen's whole data is the carriers the step footer carried for it (each
    later field's ``__init``/``__ds``/``__err`` and the bound carriers its ``CalendarPicker`` binds —
    every date field's bound carriers when ``every_screen`` — every ``__invalid`` and
    ``__required``, the form-line carriers, and the later pages' display slots — each as the send
    started it or a reaction since set it), a ``__val`` carrier for each field collected so far —
    the control-shaped value the request carried (a merged range START its pair's object), or the
    reaction's value shaped for the control (a merged pair the reaction touched re-shaped over the
    pair the request carried) — with the update applied on top.
    """
    properties, resolved, _, range_merges = form_geometry(form)
    names = component_names(properties)
    fields_by_screen, current = screen_fields(form, screen)
    if current is None or current + 1 >= len(fields_by_screen):
        return None
    next_screen = page_screen_id(current + 1)
    merged_ends = set(range_merges)
    merged_starts = {start: end for end, start in range_merges.items()}
    update_values = update.get("values") or {}
    later = [field for group in fields_by_screen[current + 1 :] for field in group]
    bound_carriers = forwarded_bound_carriers(
        properties, names, set(form.option_fields), range_merges, later, every_screen=every_screen
    )
    data_out = _forwarded_carriers(
        data, names, later, slot_keys(resolved[current + 1 :], slot_datanames(resolved)), bound_carriers
    )
    collected = [field for group in fields_by_screen[: current + 1] for field in group]
    for name in collected:
        if name in merged_ends:
            # A merged END carries no value of its own — its START holds the pair's object ``__val``.
            continue
        cname = names[name]
        if name in merged_starts:
            end = merged_starts[name]
            if name in update_values or end in update_values:
                data_out[f"{cname}__val"] = range_init_value(
                    name, end, range_pair_values(name, end, properties, data, update_values)
                )
            else:
                data_out[f"{cname}__val"] = data.get(name, {"start-date": "", "end-date": ""})
            continue
        prop = properties.get(name)
        prop = prop if isinstance(prop, dict) else {}
        if name in update_values or name not in data:
            # The ``__val`` carrier must carry the field's control shape exactly as its ``__init``
            # does; ``field_init_value`` is the one shaper both carriers share, so they never drift.
            data_out[f"{cname}__val"] = field_init_value(name, prop, update_values)
        else:
            data_out[f"{cname}__val"] = data[name]
    data_out.update(await _update_data(update, form, data))
    return {"screen": next_screen, "data": data_out}
