"""The WhatsApp Flow data endpoint — the in-form transport for an endpoint-driven form.

A form that declares a consumer reaction, carries a shape no Meta control checks natively, or draws
a field as several per-case controls is published endpoint-driven: every footer (and each reacting
field) fires a ``data_exchange`` that Meta sends here, encrypted. This route is a pure TRANSLATOR
between Meta's wire and the platform's two form seams — the one form-shape check
(``tai42_app.interactions.form_shape_errors``) and the one reaction chokepoint
(``tai42_app.interactions.react``) — it holds no business logic:

* authenticate (``X-Hub-Signature-256`` over the raw body; a mismatch is HTTP 432);
* decrypt the request (RSA-unwrap the AES key, AES-128-GCM the payload; undecryptable is 421);
* ``ping`` → the health response; ``INIT``/``BACK`` → a benign no-op (the send pre-populates
  the entry screen through its navigate payload);
* ``data_exchange`` → recover the form from its ``flow_token`` (an ask's interaction id → its
  :class:`~tai42_channel_whatsapp.correlation.EndpointForm` sidecar; an ask-less ``tai42-nf:``
  token → the notify sidecar of its schema hash; an unknown or expired token is HTTP 427), read
  each per-case field's answer from its shown case, and coerce the values filled so far. A step
  advance checks THIS screen's fields (nothing demanded), a submit the whole answer; a broken shape
  keeps the person on the screen with each failing field's sentence bound to its ``error-message``
  and a ``Label: sentence`` snackbar. Only a clean check runs a DECLARED consumer reaction, whose
  FORM UPDATE is translated into the vendor ``{screen, data}`` response (per-field errors → the
  snackbar; values/options/display → the screen data). The ``data`` of a response becomes the
  screen's state, so every answer carries a screen's whole data: one that keeps the person on the
  screen starts from the whole data the action carried, each control holding the value the person
  left in it; a clean advance names the next screen, built from the carriers the request carried; a
  clean submit completes the Flow;
* anything a seam raises (closed/malformed/handler-fail/deadline) surfaces as the vendor's error
  surface (a screen ``error_message``) plus a LOUD log — never a stale or silent value;
* encrypt the response under the same AES key with the bit-flipped IV.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelInputError
from tai42_contract.interactions import (
    AnswerFormat,
    FieldChangedEvent,
    FormReactionError,
    FormReactionEvent,
    FormReactionEventKind,
    FormReactions,
    FormShapeKind,
    PageAdvancedEvent,
    QuestionFormat,
    SubmittedEvent,
)
from tai42_kit.interactions import evaluate_visible_when
from tai42_kit.net.request_body import RequestBodyTooLargeError, read_bounded_body
from tai42_kit.settings import require_secret

from tai42_channel_whatsapp.channel import _NOTIFY_FORM_TOKEN_PREFIX
from tai42_channel_whatsapp.correlation import EndpointForm, get_cached_flow_form, get_endpoint_form
from tai42_channel_whatsapp.flow_crypto import (
    FlowPayloadDecryptError,
    decrypt_request,
    encrypt_response,
    load_private_key,
)
from tai42_channel_whatsapp.flows import (
    FORM_ENTRY_SCREEN,
    SUCCESS_SCREEN,
    _form_range_merges,
    _resolve_pages,
    _screen_id,
    component_names,
    data_source_rows,
    payload_labels,
    slot_datanames,
)
from tai42_channel_whatsapp.flows_components import _chips_single_pick, split_range_values
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
from tai42_channel_whatsapp.flows_resolve import form_drawing_order
from tai42_channel_whatsapp.flows_send_data import _field_init_value, _init_value
from tai42_channel_whatsapp.flows_shapes import channel_word
from tai42_channel_whatsapp.flows_visibility import split_answer_cases
from tai42_channel_whatsapp.inbound.auth import _SIGNATURE_HEADER, SignatureRejectedError, _validate_signature
from tai42_channel_whatsapp.inbound.forms import _coerce_value, _parse_notify_form_token
from tai42_channel_whatsapp.settings import whatsapp_settings

logger = logging.getLogger(__name__)

# Bound what the endpoint reads into memory — loud 413, never truncation.
_MAX_BODY_BYTES = 1 * 1024 * 1024

# The generic notice shown to the participant (via a screen ``error_message``) when a reaction
# or the shape check cannot be computed — the vendor's error surface, paired with a loud operator
# log — unless the caller set the form's ``update_failed`` line. A broken shape is never this
# notice: it names the field.
_REACTION_FAILURE_NOTICE = "Sorry, this form could not be updated right now. Please try again."


class _FlowTokenInvalidError(Exception):
    """The request's ``flow_token`` names no open endpoint-driven form (mapped to HTTP 427)."""


@tai42_app.http.custom_route(
    "/flow-data",
    methods=["POST"],
    summary="WhatsApp Flow data endpoint (endpoint-driven form data_exchange)",
    tags=["channels"],
    response_model=None,
    no_body_reason="Meta/WhatsApp Flow data endpoint: encrypted vendor request, encrypted vendor reply",
    success_media_types={"POST": ("text/plain",)},
)
async def whatsapp_flow_data(request: Request) -> Response:
    """Meta's Flow data endpoint: authenticate, decrypt, check and react, encrypt.

    The order is load-bearing: bounded body read (413) → ``X-Hub-Signature-256`` (432 on
    mismatch; nothing trusted before it) → decrypt (421) → dispatch → encrypt. An unset app
    secret or private key is a loud misconfiguration (500), never a skipped check. A
    no-longer-valid flow token is 427; anything the shape check or the react facet raises — or a
    reaction update this channel cannot render — becomes the vendor's encrypted error surface, never
    a 5xx that would have Meta retry a poison request forever.
    """
    try:
        raw = await read_bounded_body(request, _MAX_BODY_BYTES)
    except RequestBodyTooLargeError:
        return PlainTextResponse("payload too large", status_code=413)

    settings = whatsapp_settings()
    try:
        app_secret = require_secret(settings.app_secret, "WhatsApp channel", "CHANNEL_WHATSAPP_APP_SECRET")
    except ValueError:
        logger.exception("whatsapp flow endpoint: CHANNEL_WHATSAPP_APP_SECRET is unset; failing closed")
        return PlainTextResponse("channel misconfigured", status_code=500)
    try:
        _validate_signature(app_secret, raw, request.headers.get(_SIGNATURE_HEADER))
    except SignatureRejectedError as exc:
        logger.warning("rejected whatsapp flow endpoint request: %s", exc)
        return PlainTextResponse("signature verification failed", status_code=432)

    private_pem = settings.flow_private_key.get_secret_value() if settings.flow_private_key is not None else ""
    if not private_pem:
        logger.error("whatsapp flow endpoint: CHANNEL_WHATSAPP_FLOW_PRIVATE_KEY is unset; failing closed")
        return PlainTextResponse("channel misconfigured", status_code=500)
    passphrase = (
        settings.flow_private_key_passphrase.get_secret_value()
        if settings.flow_private_key_passphrase is not None
        else None
    )
    try:
        private_key = load_private_key(private_pem, passphrase)
    except ValueError:
        logger.exception("whatsapp flow endpoint: CHANNEL_WHATSAPP_FLOW_PRIVATE_KEY is not a readable RSA key")
        return PlainTextResponse("channel misconfigured", status_code=500)

    try:
        envelope = json.loads(raw)
        if not isinstance(envelope, dict):
            raise FlowPayloadDecryptError("flow endpoint body is not a JSON object")  # noqa: TRY301 caught by the shared 421 handler below alongside decrypt failures
        payload, aes_key, iv = decrypt_request(envelope, private_key)
    except (ValueError, FlowPayloadDecryptError) as exc:
        logger.warning("whatsapp flow endpoint could not decrypt the request: %s", exc)
        return PlainTextResponse("payload could not be decrypted", status_code=421)

    try:
        response_obj = await _dispatch(payload)
    except _FlowTokenInvalidError:
        return PlainTextResponse("flow token no longer valid", status_code=427)
    return PlainTextResponse(encrypt_response(response_obj, aes_key, iv))


async def _dispatch(payload: dict[str, Any]) -> dict[str, Any]:
    """Route a decrypted request by its ``action`` to the response object (to be encrypted).

    ``ping`` is the health check; ``data_exchange`` runs the form's step; ``INIT``/``BACK`` and any
    other action are a benign no-op keeping the current screen (the send pre-populates the entry
    screen, so the endpoint never has to seed it). Raises :class:`_FlowTokenInvalidError` (→ 427)
    when a ``data_exchange`` names no open endpoint-driven form.
    """
    action = payload.get("action")
    if action == "ping":
        return {"data": {"status": "active"}}
    if action == "data_exchange":
        return await _handle_data_exchange(payload)
    if action not in ("INIT", "BACK"):
        logger.warning("whatsapp flow endpoint got unexpected action %r; treating as a no-op", action)
    screen = payload.get("screen")
    return {"screen": screen if isinstance(screen, str) and screen else FORM_ENTRY_SCREEN, "data": {}}


async def _resolve_form(flow_token: str) -> tuple[EndpointForm, bool]:
    """The endpoint-driven form a flow token names and whether it is an ask (its reactions may run).

    An ask's token is its interaction id, resolving its :class:`EndpointForm` sidecar. An ask-less
    form's ``tai42-nf:`` token carries its schema hash, resolving the notify sidecar under the
    configured WABA; such a form reacts to nothing and its sentences and lines ride the request. Raises
    :class:`_FlowTokenInvalidError` on a miss.
    """
    if flow_token.startswith(_NOTIFY_FORM_TOKEN_PREFIX):
        schema_hash, _ = _parse_notify_form_token(flow_token)
        waba_id = whatsapp_settings().waba_id
        cached = await get_cached_flow_form(waba_id, schema_hash) if waba_id else None
        if cached is None:
            logger.warning("whatsapp flow endpoint: no form sidecar for notify token %s", flow_token)
            raise _FlowTokenInvalidError
        form = EndpointForm(
            schema=cached.schema,
            pages=cached.pages,
            reactions=None,
            option_fields=cached.option_fields,
            invalid={},
            required={},
            lines={},
        )
        return form, False
    endpoint_form = await get_endpoint_form(flow_token)
    if endpoint_form is None:
        logger.warning("whatsapp flow endpoint: no open endpoint-driven form for flow token %s", flow_token)
        raise _FlowTokenInvalidError
    return endpoint_form, True


async def _handle_data_exchange(payload: dict[str, Any]) -> dict[str, Any]:
    """Run one ``data_exchange``: check the shapes, run any declared reaction, and answer the vendor."""
    flow_token = payload.get("flow_token")
    screen = payload.get("screen")
    screen = screen if isinstance(screen, str) and screen else FORM_ENTRY_SCREEN
    data = payload.get("data")
    data = data if isinstance(data, dict) else {}
    if not isinstance(flow_token, str) or not flow_token:
        raise _FlowTokenInvalidError
    form, is_ask = await _resolve_form(flow_token)
    data = _read_split_answers(form, screen, data)
    properties = form.schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    reactions = FormReactions.model_validate(form.reactions) if is_ask and form.reactions else FormReactions()

    event = _build_event(data)
    if event is None:
        logger.error("whatsapp flow request for %s carried no recognised event marker", flow_token)
        return _error_response(form, screen, data, "")
    partial = _partial_values(data, properties)
    try:
        if isinstance(event, FieldChangedEvent):
            if event.field not in reactions.field_changed:
                logger.error("whatsapp flow request for %s names undeclared field trigger %r", flow_token, event.field)
                return _error_response(form, screen, data, "")
            update = await tai42_app.interactions.react(flow_token, event, partial)
            return {
                "screen": screen,
                "data": _screen_answer(form, screen, data, _update_data(update or {}, form, data)),
            }
        refusal = _shape_refusal(form, screen, event, partial, data)
        if refusal is not None:
            return refusal
        update: dict[str, Any] = {}
        declared = (isinstance(event, SubmittedEvent) and reactions.submitted) or (
            isinstance(event, PageAdvancedEvent) and event.page in reactions.page_advanced
        )
        if declared:
            update = await tai42_app.interactions.react(flow_token, event, partial) or {}
        return _translate_update(event, update, screen, flow_token, form, partial, data)
    except FormReactionError as exc:
        # The reaction door refused or failed; its reason fills the caller's ``update_failed``.
        logger.exception("whatsapp flow request for %s: the reaction failed; surfacing the vendor error", flow_token)
        return _error_response(form, screen, data, exc.reason)
    except ChannelInputError:
        # A reaction result this channel cannot render (e.g. an option second line past Meta's cap)
        # is the same class as a failed reaction: logged loudly, answered with the vendor error. Its
        # text is this channel's own vendor message, never shown to the person.
        logger.exception("whatsapp flow request for %s returned an update WhatsApp cannot render", flow_token)
        return _error_response(form, screen, data, "")
    except Exception:
        logger.exception("whatsapp flow request for %s failed; surfacing the vendor error", flow_token)
        return _error_response(form, screen, data, "")


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


def _read_split_answers(form: EndpointForm, screen: str, data: dict[str, Any]) -> dict[str, Any]:
    """The request data with each per-case field's answer read from its shown case.

    A field drawn as several per-case controls rides the payload one entry per case, keyed by the
    case's component name (:func:`~tai42_channel_whatsapp.flows_visibility.answer_refs`). The shown
    case is the one whose visibility predicates all hold for the values the request carries — this
    screen's (a per-case controller's own answer read first) and the earlier screens' — evaluated as
    the platform evaluates ``visibleWhen``; its value is kept under the field's schema name. A field
    none of whose cases holds is hidden and carries no value, whatever its undrawn cases carried.
    Every case entry is dropped, so the rest of the exchange reads the field as one value.
    """
    properties = form.schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    fields_by_screen, current = _screen_fields(form, screen)
    if current is None:
        return data
    earlier = [field for group in fields_by_screen[:current] for field in group]
    cases = split_answer_cases(fields_by_screen[current], earlier, properties, component_names(properties))
    if not cases:
        return data
    case_names = {case_name for field_cases in cases.values() for case_name, _ in field_cases}
    out = {key: value for key, value in data.items() if key not in case_names}
    _ShownCases(cases, properties, data, out).read_all()
    return out


class _ShownCases:
    """Writes each per-case field's shown-case value into ``out`` under its schema name."""

    def __init__(
        self,
        cases: dict[str, list[tuple[str, list[dict[str, Any]]]]],
        properties: dict[str, Any],
        data: dict[str, Any],
        out: dict[str, Any],
    ) -> None:
        self._cases = cases
        self._properties = properties
        self._data = data
        self._out = out
        self._read: set[str] = set()

    def read_all(self) -> None:
        for name in self._cases:
            self._pick(name)

    def _value(self, name: str) -> Any:
        # A controller's value as the platform's ``visibleWhen`` reads it: coerced to its schema type,
        # a per-case controller's own answer read first; ``None`` when the request carries none.
        if name in self._cases:
            self._pick(name)
        return _coerce_value(self._out[name], self._properties.get(name)) if name in self._out else None

    def _pick(self, name: str) -> None:
        if name in self._read:
            return
        self._read.add(name)
        for case_name, predicates in self._cases[name]:
            if all(evaluate_visible_when(p, {p["field"]: self._value(p["field"])}) for p in predicates):
                if case_name in self._data:
                    self._out[name] = self._data[case_name]
                return


def _partial_values(data: dict[str, Any], properties: dict[str, Any]) -> dict[str, Any]:
    """The values filled so far, keyed by schema name and coerced to schema types.

    Only declared properties are read (the markers and the forwarded carriers are not values);
    each value is coerced as the inbound answer path coerces (Flow numbers arrive as strings). A
    non-string field whose value is empty (an unfilled number/boolean-less/array) is genuinely not
    yet filled, so it is omitted — partial values are a subset. An empty text value is kept here
    (a reaction sees it as typed); :func:`_filled` drops it before the shape check.
    """
    partial: dict[str, Any] = {}
    for key, raw in data.items():
        prop = properties.get(key)
        if key in RESERVED_REACTION_KEYS or not isinstance(prop, dict):
            continue
        coerced = _coerce_value(raw, prop)
        ptype = prop.get("type")
        # A single-pick chips string's empty value is a ``[]`` (not ``""``), so treat it like the
        # other non-string unfilled defaults and omit it from the partial subset.
        unset_when_empty = ptype != "string" or _chips_single_pick(prop)
        if unset_when_empty and coerced in ("", [], None):
            continue
        partial[key] = coerced
    # A merged range control's object value under the START key splits back onto the two date keys.
    return split_range_values(properties, partial)


def _filled(partial: dict[str, Any]) -> dict[str, Any]:
    # The values a person actually filled: an empty text control is unfilled (a required one is
    # Meta's own native check), so it is neither shape-checked nor sent as the answer.
    return {name: value for name, value in partial.items() if value != ""}


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
    fields_by_screen, current = _screen_fields(form, screen)
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
    # The fields binding an ``error-message`` and the form's ``END -> START`` range merge map — the
    # same ones the published Flow and its send data were built from.
    resolved, fields_by_screen = _resolve_pages(properties, form.pages)
    range_merges = _form_range_merges(properties, resolved, fields_by_screen)
    required_raw = form.schema.get("required")
    required = {str(name) for name in required_raw} if isinstance(required_raw, list) else set()
    return error_fields(properties, required, set(form.option_fields), names, set(range_merges)), range_merges


def _screen_fields(form: EndpointForm, screen: str) -> tuple[list[list[str]], int | None]:
    # Every screen's field names and the index of ``screen`` among them (None when not a form screen).
    properties = form.schema.get("properties")
    _, fields_by_screen = _resolve_pages(properties if isinstance(properties, dict) else {}, form.pages)
    current = next((index for index in range(len(fields_by_screen)) if _screen_id(index) == screen), None)
    return fields_by_screen, current


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
    """The same-screen refusal for a step or submit whose values break a declared shape, else ``None``.

    A step advance checks THIS screen's fields with nothing demanded; a submit the whole answer.
    A picker's membership in a per-send option list is never refused here (the list it drew is
    the list it offers; the answer door judges membership against the stored list). Each failing
    field on the screen shows its sentence — the caller's ``required`` for a missing value or
    ``invalid`` for any other shape, else this channel's word for the shape — in its
    ``error-message`` (a merged range end's on its own end of the pair's one error object); every
    other field's is cleared; the snackbar is the caller's ``error_line``, else each failing field's
    label and sentence (:func:`_error_line`). The answer carries the screen's whole data
    (:func:`_screen_answer`).
    """
    properties = form.schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    fields_by_screen, current = _screen_fields(form, screen)
    this_fields = fields_by_screen[current] if current is not None else []
    question = QuestionFormat(
        answer_format=AnswerFormat.FORM,
        format_payload={"schema": form.schema, **({"reactions": form.reactions} if form.reactions else {})},
    )
    if isinstance(event, PageAdvancedEvent):
        errors = tai42_app.interactions.form_shape_errors(question, _filled(partial), fields=this_fields, partial=True)
    else:
        errors = tai42_app.interactions.form_shape_errors(question, _filled(partial))
    errors = [error for error in errors if not (error.kind is FormShapeKind.ENUM and error.field in form.option_fields)]
    if not errors:
        return None
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
    out: dict[str, Any] = error_carriers(this_fields, names, bound, range_merges, shown)
    out["error_message"] = _error_line(form, data, items)
    return {"screen": screen, "data": _screen_answer(form, screen, data, out)}


def _translate_update(
    event: FormReactionEvent,
    update: dict[str, Any],
    screen: str,
    flow_token: str,
    form: EndpointForm,
    partial: dict[str, Any],
    data: dict[str, Any],
) -> dict[str, Any]:
    """Answer a clean step or submit, with any reaction's FORM UPDATE applied.

    A clean ``submitted`` completes the Flow (the terminal SUCCESS response, carrying the answer
    as the completion ``params``); reaction errors keep the form on its screen with an
    ``error_message``. A clean ``page_advanced`` advances to the next screen, built from the
    carriers the request carried with the update applied; on the last screen it stays. Staying,
    the answer is the screen's whole data with its field sentences cleared (its values passed the
    shape check) and the update applied.
    """
    errors = update.get("errors") or {}
    if isinstance(event, SubmittedEvent) and not errors:
        properties = form.schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        resolved, fields_by_screen = _resolve_pages(properties, form.pages)
        merges = _form_range_merges(properties, resolved, fields_by_screen)
        return _completion(
            flow_token, properties, payload_labels(properties), merges, _filled(partial), update.get("values") or {}
        )
    if isinstance(event, PageAdvancedEvent) and not errors:
        advanced = _advance_screen(screen, form, data, update)
        if advanced is not None:
            return advanced
    return {
        "screen": screen,
        "data": _screen_answer(form, screen, data, _update_data(update, form, data), clear_errors=True),
    }


def _update_data(update: dict[str, Any], form: EndpointForm, data: dict[str, Any]) -> dict[str, Any]:
    """The screen-``data`` keys a FORM UPDATE sets: field values, option lists, display slots, error message.

    The error message of an update carrying field errors is the caller's ``error_line`` (an ask's
    sidecar or the carrier of ``data``, the request's), else ``Label: message`` per field.
    """
    properties = form.schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    names = component_names(properties)
    labels = payload_labels(properties)
    slots = slot_datanames(form.pages)
    out: dict[str, Any] = {}
    for name, value in (update.get("values") or {}).items():
        prop = properties.get(name)
        prop = prop if isinstance(prop, dict) else {}
        shaped = _init_value(prop.get("type"), value)
        # A single-pick chips string's data carrier is array-typed (``max-selected-items: 1``), so its
        # ``__init`` is the one-element list the ChipsSelector expects — matching the initial send.
        out[f"{names[name]}__init"] = [shaped] if _chips_single_pick(prop) else shaped
    for name, option_list in (update.get("options") or {}).items():
        prop = properties.get(name)
        prop = prop if isinstance(prop, dict) else {}
        out[f"{names[name]}__ds"] = data_source_rows(name, prop, option_list)
    for slot, value in (update.get("display") or {}).items():
        if slot in slots:
            out[slots[slot]] = value
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
    filled: dict[str, Any],
    values_update: dict[str, Any],
) -> dict[str, Any]:
    """The terminal SUCCESS response that completes an endpoint-driven Flow and sends the answer back.

    The completed answer rides ``extension_message_response.params``, keyed by the same
    human-readable labels the static Flow's completion uses, so the inbound ``nfm_reply`` decode
    maps them back to the schema keys uniformly; ``params.flow_token`` is the request's own token
    (an ask's interaction id, or an ask-less form's ``tai42-nf:`` token). The merged values are the
    ones filled overlaid with any the submit reaction set. The label keys come in the Flow's drawing
    order (:func:`form_drawing_order` over ``merges``, the form's ``END -> START`` range merges):
    each ``timeOf`` time right after its date, and a merged range END with its time right after
    the START's time.
    """
    merged = {**filled, **values_update}
    params: dict[str, Any] = {"flow_token": flow_token}
    for name in form_drawing_order(list(properties), properties, merges):
        if name in merged:
            params[labels[name]] = merged[name]
    return {"screen": SUCCESS_SCREEN, "data": {"extension_message_response": {"params": params}}}


def _forwarded_carriers(data: dict[str, Any], names: dict[str, str], later_fields: list[str]) -> dict[str, Any]:
    # The carriers the next screen declares that the request carried — each later field's
    # ``__init``/``__ds``/``__err``, every ``__invalid`` and ``__required``, and the form-line
    # carriers — passed on verbatim. The rest of the screen's data (its own fields' carriers, the
    # ``__val`` carriers rebuilt here, its display slots) is not the next screen's.
    keys = {f"{names[name]}{suffix}" for name in later_fields for suffix in ("__init", "__ds", "__err")}
    keys |= {f"{cname}{suffix}" for cname in names.values() for suffix in ("__invalid", "__required")}
    keys |= {form_line_carrier(member) for member in FORM_LINE_MEMBERS}
    return {key: value for key, value in data.items() if key in keys}


def _advance_screen(
    screen: str, form: EndpointForm, data: dict[str, Any], update: dict[str, Any]
) -> dict[str, Any] | None:
    """The next-screen response for a clean page advance, or ``None`` when there is no next screen.

    Stateless: the next screen's whole data is the carriers the step footer carried for it (each
    later field's ``__init``/``__ds``/``__err``, every ``__invalid`` and ``__required``, and the
    form-line carriers), a ``__val`` carrier for
    each field collected so far — the control-shaped value the request carried (a merged range
    START its pair's object), or the reaction's value shaped for the control — and the next page's
    display slots, empty, with the update applied on top.
    """
    properties = form.schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    names = component_names(properties)
    resolved, fields_by_screen = _resolve_pages(properties, form.pages)
    current = next((index for index in range(len(fields_by_screen)) if _screen_id(index) == screen), None)
    if current is None or current + 1 >= len(fields_by_screen):
        return None
    next_screen = _screen_id(current + 1)
    range_merges = _form_range_merges(properties, resolved, fields_by_screen)
    merged_ends = set(range_merges)
    merged_starts = {start: end for end, start in range_merges.items()}
    update_values = update.get("values") or {}
    later = [field for group in fields_by_screen[current + 1 :] for field in group]
    data_out = _forwarded_carriers(data, names, later)
    data_out.update(dict.fromkeys(slot_datanames(resolved[current + 1 : current + 2]).values(), ""))
    collected = [field for group in fields_by_screen[: current + 1] for field in group]
    for name in collected:
        if name in merged_ends:
            # A merged END carries no value of its own — its START holds the pair's object ``__val``.
            continue
        cname = names[name]
        if name in merged_starts:
            end = merged_starts[name]
            if name in update_values or end in update_values:
                data_out[f"{cname}__val"] = _range_val_carrier(name, end, update_values)
            else:
                data_out[f"{cname}__val"] = data.get(name, {"start-date": "", "end-date": ""})
            continue
        prop = properties.get(name)
        prop = prop if isinstance(prop, dict) else {}
        if name in update_values or name not in data:
            # The ``__val`` carrier must carry the field's control shape exactly as its ``__init``
            # does; ``_field_init_value`` is the one shaper both carriers share, so they never drift.
            data_out[f"{cname}__val"] = _field_init_value(name, prop, update_values)
        else:
            data_out[f"{cname}__val"] = data[name]
    data_out.update(_update_data(update, form, data))
    return {"screen": next_screen, "data": data_out}


def _range_val_carrier(start: str, end: str, values: dict[str, Any]) -> dict[str, str]:
    """The forwarded ``__val`` object for a merged range START — both ends' date strings."""

    def _member(name: str) -> str:
        raw = values.get(name)
        return raw if isinstance(raw, str) else ""

    return {"start-date": _member(start), "end-date": _member(end)}
