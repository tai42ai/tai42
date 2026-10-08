"""The WhatsApp Flow data endpoint — the reactive transport for a reacting form.

A reacting Flow is published endpoint-driven: its reacting fields/pages/submission fire
``data_exchange`` actions that Meta sends here, encrypted. This route is a pure TRANSLATOR
between Meta's wire and the platform's one reaction chokepoint
(``tai42_app.interactions.react``) — it holds no business logic:

* authenticate (``X-Hub-Signature-256`` over the raw body; a mismatch is HTTP 432);
* decrypt the request (RSA-unwrap the AES key, AES-128-GCM the payload; undecryptable is 421);
* ``ping`` → the health response; ``INIT``/``BACK`` → a benign no-op (the send pre-populates
  the entry screen through its navigate payload);
* ``data_exchange`` → recover the form's schema from the ``flow_token`` sidecar (an unknown or
  expired token is HTTP 427), map the vendor ``action``/markers to a react EVENT, coerce the
  partial values, call ``react``, and translate the returned FORM UPDATE into the vendor
  ``{screen, data}`` response (per-field errors → a screen ``error_message``; values/options/
  display → the screen data; a clean ``submitted`` → the terminal completion);
* anything ``react`` raises (closed/malformed/handler-fail/deadline) surfaces as the vendor's
  error surface (a screen ``error_message``) plus a LOUD log — never a stale or silent value;
* encrypt the response under the same AES key with the bit-flipped IV.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from tai42_contract.app import tai42_app
from tai42_kit.net.request_body import RequestBodyTooLargeError, read_bounded_body
from tai42_kit.settings import require_secret

from tai42_channel_whatsapp.correlation import get_reaction_form
from tai42_channel_whatsapp.flow_crypto import (
    FlowPayloadDecryptError,
    decrypt_request,
    encrypt_response,
    load_private_key,
)
from tai42_channel_whatsapp.flows import (
    _EVENT_FIELD_CHANGED,
    _EVENT_PAGE_ADVANCED,
    _EVENT_SUBMITTED,
    _REACTION_EVENT_KEY,
    _REACTION_FIELD_KEY,
    _REACTION_PAGE_KEY,
    _RESERVED_REACTION_KEYS,
    FORM_ENTRY_SCREEN,
    SUCCESS_SCREEN,
    _init_value,
    _resolve_pages,
    _screen_id,
    build_flow_data,
    component_names,
    data_source_item,
    payload_labels,
    slot_datanames,
)
from tai42_channel_whatsapp.inbound.auth import _SIGNATURE_HEADER, SignatureRejectedError, _validate_signature
from tai42_channel_whatsapp.inbound.forms import _coerce_value
from tai42_channel_whatsapp.settings import whatsapp_settings

logger = logging.getLogger(__name__)

# Bound what the endpoint reads into memory — loud 413, never truncation.
_MAX_BODY_BYTES = 1 * 1024 * 1024

# The generic notice shown to the participant (via a screen ``error_message``) when a reaction
# cannot be computed — the vendor's error surface, paired with a loud operator log.
_REACTION_FAILURE_NOTICE = "Sorry, this form could not be updated right now. Please try again."


class _FlowTokenInvalidError(Exception):
    """The request's ``flow_token`` names no open reacting form (mapped to HTTP 427)."""


@tai42_app.http.custom_route(
    "/flow-data",
    methods=["POST"],
    summary="WhatsApp Flow data endpoint (reacting-form data_exchange)",
    tags=["channels"],
    response_model=None,
    no_body_reason="Meta/WhatsApp Flow data endpoint: encrypted vendor request, encrypted vendor reply",
)
async def whatsapp_flow_data(request: Request) -> Response:
    """Meta's Flow data endpoint: authenticate, decrypt, react, encrypt.

    The order is load-bearing: bounded body read (413) → ``X-Hub-Signature-256`` (432 on
    mismatch; nothing trusted before it) → decrypt (421) → dispatch → encrypt. An unset app
    secret or private key is a loud misconfiguration (500), never a skipped check. A
    no-longer-valid flow token is 427; anything the react facet raises becomes the vendor's
    encrypted error surface, never a 5xx that would have Meta retry a poison reaction forever.
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

    ``ping`` is the health check; ``data_exchange`` runs a reaction; ``INIT``/``BACK`` and any
    other action are a benign no-op keeping the current screen (the send pre-populates the entry
    screen, so the endpoint never has to seed it). Raises :class:`_FlowTokenInvalidError` (→ 427)
    when a ``data_exchange`` names no open reacting form.
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


async def _handle_data_exchange(payload: dict[str, Any]) -> dict[str, Any]:
    """Run one reaction for a ``data_exchange`` request and translate its update into a vendor response."""
    flow_token = payload.get("flow_token")
    screen = payload.get("screen")
    screen = screen if isinstance(screen, str) and screen else FORM_ENTRY_SCREEN
    data = payload.get("data")
    data = data if isinstance(data, dict) else {}
    if not isinstance(flow_token, str) or not flow_token:
        raise _FlowTokenInvalidError
    cached = await get_reaction_form(flow_token)
    if cached is None:
        logger.warning("whatsapp flow endpoint: no open reacting form for flow token %s", flow_token)
        raise _FlowTokenInvalidError
    schema, pages, values, options = cached
    properties = schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}

    event = _build_event(data)
    if event is None:
        logger.error("whatsapp flow reaction for %s carried no recognised event marker", flow_token)
        return _error_response(screen)
    partial = _partial_values(data, properties)
    try:
        update = await tai42_app.interactions.react(flow_token, event, partial)
    except Exception:
        logger.exception("whatsapp flow reaction for %s failed; surfacing the vendor error", flow_token)
        return _error_response(screen)
    return _translate_update(event, update or {}, screen, flow_token, schema, pages, values, options, partial)


def _build_event(data: dict[str, Any]) -> dict[str, Any] | None:
    """The react EVENT a ``data_exchange`` payload names via its markers, or ``None`` when malformed."""
    kind = data.get(_REACTION_EVENT_KEY)
    if kind == _EVENT_FIELD_CHANGED:
        field = data.get(_REACTION_FIELD_KEY)
        return {"kind": _EVENT_FIELD_CHANGED, "field": field} if isinstance(field, str) else None
    if kind == _EVENT_PAGE_ADVANCED:
        page = data.get(_REACTION_PAGE_KEY)
        return {"kind": _EVENT_PAGE_ADVANCED, "page": page} if isinstance(page, str) else None
    if kind == _EVENT_SUBMITTED:
        return {"kind": _EVENT_SUBMITTED}
    return None


def _partial_values(data: dict[str, Any], properties: dict[str, Any]) -> dict[str, Any]:
    """The values filled so far, keyed by schema name and coerced to schema types.

    The reaction markers are stripped; each value is coerced as the inbound answer path coerces
    (Flow numbers arrive as strings). A non-string field whose value is empty (an unfilled
    number/boolean-less/array) is genuinely not yet filled, so it is omitted — partial values
    are a subset, and the react facet type-checks exactly what is present.
    """
    partial: dict[str, Any] = {}
    for key, raw in data.items():
        if key in _RESERVED_REACTION_KEYS:
            continue
        prop = properties.get(key)
        coerced = _coerce_value(raw, prop)
        ptype = prop.get("type") if isinstance(prop, dict) else None
        if ptype != "string" and coerced in ("", [], None):
            continue
        partial[key] = coerced
    return partial


def _error_response(screen: str) -> dict[str, Any]:
    """Keep the form on its current screen and show the generic reaction-failure notice."""
    return {"screen": screen, "data": {"error_message": _REACTION_FAILURE_NOTICE}}


def _translate_update(
    event: dict[str, Any],
    update: dict[str, Any],
    screen: str,
    flow_token: str,
    schema: dict[str, Any],
    pages: list[dict[str, Any]] | None,
    values: dict[str, Any],
    options: dict[str, list[dict[str, Any]]],
    partial: dict[str, Any],
) -> dict[str, Any]:
    """Translate a react FORM UPDATE into the vendor ``{screen, data}`` response.

    A clean ``submitted`` completes the Flow (the terminal SUCCESS response, carrying the answer
    as the completion ``params``); any errors keep the form on its screen with an
    ``error_message``. A clean ``page_advanced`` advances to the next screen (rebuilt with the
    collected values and the update applied); a ``field_changed`` (or any event with errors)
    stays on the current screen with the update applied.
    """
    properties = schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    names = component_names(properties)
    labels = payload_labels(properties)
    slots = slot_datanames(pages)
    errors = update.get("errors") or {}

    if event["kind"] == _EVENT_SUBMITTED and not errors:
        return _completion(flow_token, properties, labels, partial, update.get("values") or {})
    if event["kind"] == _EVENT_PAGE_ADVANCED and not errors:
        advanced = _advance_screen(screen, schema, pages, names, labels, slots, values, options, partial, update)
        if advanced is not None:
            return advanced
    return {"screen": screen, "data": _update_data(update, properties, names, labels, slots)}


def _update_data(
    update: dict[str, Any],
    properties: dict[str, Any],
    names: dict[str, str],
    labels: dict[str, str],
    slots: dict[str, str],
) -> dict[str, Any]:
    """The screen-``data`` keys a FORM UPDATE sets: field values, option lists, display slots, error message."""
    out: dict[str, Any] = {}
    for name, value in (update.get("values") or {}).items():
        prop = properties.get(name)
        prop = prop if isinstance(prop, dict) else {}
        out[f"{names[name]}__init"] = _init_value(prop.get("type"), value)
    for name, option_list in (update.get("options") or {}).items():
        out[f"{names[name]}__ds"] = [data_source_item(option) for option in option_list]
    for slot, value in (update.get("display") or {}).items():
        if slot in slots:
            out[slots[slot]] = value
    errors = update.get("errors") or {}
    if errors:
        out["error_message"] = "; ".join(f"{labels.get(field, field)}: {message}" for field, message in errors.items())
    return out


def _completion(
    flow_token: str,
    properties: dict[str, Any],
    labels: dict[str, str],
    partial: dict[str, Any],
    values_update: dict[str, Any],
) -> dict[str, Any]:
    """The terminal SUCCESS response that completes a reacting Flow and sends the answer back.

    The completed answer rides ``extension_message_response.params``, keyed by the same
    human-readable labels the static Flow's completion uses, so the inbound ``nfm_reply`` decode
    maps them back to the schema keys uniformly. The merged values are the ones filled so far
    overlaid with any the submit reaction set.
    """
    merged = {**partial, **values_update}
    params: dict[str, Any] = {"flow_token": flow_token}
    for name in properties:
        if name in merged:
            params[labels[name]] = merged[name]
    return {"screen": SUCCESS_SCREEN, "data": {"extension_message_response": {"params": params}}}


def _advance_screen(
    screen: str,
    schema: dict[str, Any],
    pages: list[dict[str, Any]] | None,
    names: dict[str, str],
    labels: dict[str, str],
    slots: dict[str, str],
    values: dict[str, Any],
    options: dict[str, list[dict[str, Any]]],
    partial: dict[str, Any],
    update: dict[str, Any],
) -> dict[str, Any] | None:
    """The next-screen response for a clean page advance, or ``None`` when there is no next screen.

    The next screen is rebuilt from the collected values (the per-send prefill overlaid with the
    values filled so far and any the reaction set): every field's ``init``/``ds`` and every
    collected field's ``__val`` carrier, with the update applied on top.
    """
    properties = schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    _resolved, fields_by_screen = _resolve_pages(properties, pages)
    current = next((index for index in range(len(fields_by_screen)) if _screen_id(index) == screen), None)
    if current is None or current + 1 >= len(fields_by_screen):
        return None
    next_screen = _screen_id(current + 1)
    merged_values = {**values, **partial, **(update.get("values") or {})}
    merged_options: dict[str, list[dict[str, Any]]] = {**options}
    for name, option_list in (update.get("options") or {}).items():
        merged_options[name] = [
            {"value": option["value"], **({"label": option["label"]} if option.get("label") else {})}
            for option in option_list
        ]
    data_out = build_flow_data(schema, merged_values, merged_options)
    collected = [field for group in fields_by_screen[: current + 1] for field in group]
    for name in collected:
        ptype = properties.get(name, {}).get("type") if isinstance(properties.get(name), dict) else None
        if name in merged_values:
            data_out[f"{names[name]}__val"] = _init_value(ptype, merged_values[name])
        else:
            data_out[f"{names[name]}__val"] = [] if ptype == "array" else False if ptype == "boolean" else ""
    data_out.update(_update_data(update, properties, names, labels, slots))
    return {"screen": next_screen, "data": data_out}
