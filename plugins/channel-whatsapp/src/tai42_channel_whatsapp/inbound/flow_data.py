"""The WhatsApp Flow data endpoint — the encrypted HTTP door of an endpoint-driven form.

A form that declares a consumer reaction, carries a shape no Meta control checks natively, or draws
a field as several per-case controls is published endpoint-driven: every footer (and each reacting
field) fires a ``data_exchange`` that Meta sends here, encrypted. This route owns Meta's wire:

* authenticate (``X-Hub-Signature-256`` over the raw body; a mismatch is HTTP 432);
* decrypt the request (RSA-unwrap the AES key, AES-128-GCM the payload; undecryptable is 421);
* ``ping`` → the health response; ``INIT``/``BACK`` → a benign no-op (the send pre-populates
  the entry screen through its navigate payload);
* ``data_exchange`` → the form's step
  (:func:`~tai42_channel_whatsapp.inbound.data_exchange.handle_data_exchange`); a ``flow_token``
  naming no open form is HTTP 427;
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

from tai42_channel_whatsapp.flow_crypto import (
    FlowPayloadDecryptError,
    decrypt_request,
    encrypt_response,
    load_private_key,
)
from tai42_channel_whatsapp.flows import FORM_ENTRY_SCREEN
from tai42_channel_whatsapp.flows_endpoint import ENDPOINT_BODY_MAX_BYTES
from tai42_channel_whatsapp.inbound.auth import SIGNATURE_HEADER, SignatureRejectedError, validate_signature
from tai42_channel_whatsapp.inbound.data_exchange import FlowTokenInvalidError, handle_data_exchange
from tai42_channel_whatsapp.settings import whatsapp_settings

logger = logging.getLogger(__name__)


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
        raw = await read_bounded_body(request, ENDPOINT_BODY_MAX_BYTES)
    except RequestBodyTooLargeError:
        return PlainTextResponse("payload too large", status_code=413)

    settings = whatsapp_settings()
    try:
        app_secret = require_secret(settings.app_secret, "WhatsApp channel", "CHANNEL_WHATSAPP_APP_SECRET")
    except ValueError:
        logger.exception("whatsapp flow endpoint: CHANNEL_WHATSAPP_APP_SECRET is unset; failing closed")
        return PlainTextResponse("channel misconfigured", status_code=500)
    try:
        validate_signature(app_secret, raw, request.headers.get(SIGNATURE_HEADER))
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
    except FlowTokenInvalidError:
        return PlainTextResponse("flow token no longer valid", status_code=427)
    return PlainTextResponse(encrypt_response(response_obj, aes_key, iv))


async def _dispatch(payload: dict[str, Any]) -> dict[str, Any]:
    """Route a decrypted request by its ``action`` to the response object (to be encrypted).

    ``ping`` is the health check; ``data_exchange`` runs the form's step; ``INIT``/``BACK`` and any
    other action are a benign no-op keeping the current screen (the send pre-populates the entry
    screen, so the endpoint never has to seed it). Raises :class:`FlowTokenInvalidError` (→ 427)
    when a ``data_exchange`` names no open endpoint-driven form.
    """
    action = payload.get("action")
    if action == "ping":
        return {"data": {"status": "active"}}
    if action == "data_exchange":
        return await handle_data_exchange(payload)
    if action not in ("INIT", "BACK"):
        logger.warning("whatsapp flow endpoint got unexpected action %r; treating as a no-op", action)
    screen = payload.get("screen")
    return {"screen": screen if isinstance(screen, str) and screen else FORM_ENTRY_SCREEN, "data": {}}
