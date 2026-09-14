"""The HTTP route and per-batch dispatch for Meta's single webhook endpoint."""

from __future__ import annotations

import json
import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from tai42_contract.app import tai42_app
from tai42_kit.net.request_body import PayloadTooLarge

from tai42_channel_whatsapp.inbound.auth import (
    SignatureRejectedError,
    _auth_error_response,
    _authenticated_body,
    _verify_handshake,
)
from tai42_channel_whatsapp.inbound.envelope import _as_list, _iter_values
from tai42_channel_whatsapp.inbound.messages import _handle_message
from tai42_channel_whatsapp.inbound.status import _handle_status

logger = logging.getLogger(__name__)


@tai42_app.http.custom_route(
    "/inbound",
    methods=["GET", "POST"],
    summary="WhatsApp webhook (verification + messages + delivery statuses)",
    tags=["channels"],
    response_model=None,
    no_body_reason="Meta/WhatsApp webhook: GET hub-challenge echo, POST vendor ack",
)
async def whatsapp_inbound(request: Request) -> Response:
    """Meta's single webhook endpoint: GET verification, POST message/status events.

    POST order is load-bearing: bounded body read (413) → X-Hub-Signature-256
    (nothing trusted before it) → parse → dispatch. A single POST batches many
    entries/changes/messages/statuses; every one is processed. Each status records
    a delivery receipt; each message runs wamid dedupe → pending-question
    correlation → bridge on a MISS. A correlated reply forwards to the callback
    door; a bridge message with no route is logged and skipped (the provider must
    not retry a permanently-unrouted address). A propagating (non-LookupError)
    failure surfaces as a 5xx so Meta redelivers the whole batch — safe because
    each message dedupes on its own wamid, so already-handled ones are skipped.
    """
    if request.method == "GET":
        return _verify_handshake(request)

    try:
        raw = await _authenticated_body(request)
    except (ValueError, PayloadTooLarge, SignatureRejectedError) as exc:
        return _auth_error_response(exc)

    try:
        payload = json.loads(raw)
    except ValueError:
        return PlainTextResponse("invalid JSON", status_code=400)

    first_error: Exception | None = None
    for value in _iter_values(payload):
        error = await _process_value(value)
        if error is not None and first_error is None:
            first_error = error
    if first_error is not None:
        # An earlier item's failure never abandons later independent items: they
        # commit their own dedupe/correlation above, then the batch 5xx's for the
        # failed item only, which retries under its own wamid on redelivery.
        raise first_error
    return Response(status_code=200)


async def _process_value(value: dict[str, Any]) -> Exception | None:
    """Dispatch every status then every message in one webhook value; process all
    of them even if some fail, and return the FIRST propagating failure (or None).

    A non-object ``statuses``/``messages`` item is odd and skipped (logged), never
    a 500. A per-item propagating (non-LookupError) failure does not abort the
    batch — it is remembered so the caller can re-raise a single aggregated error.
    """
    first_error: Exception | None = None
    for status in _as_list(value.get("statuses")):
        if not isinstance(status, dict):
            logger.warning("whatsapp status item is not an object; skipping: %r", status)
            continue
        try:
            await _handle_status(status)
        except Exception as exc:
            if first_error is None:
                first_error = exc
    for message in _as_list(value.get("messages")):
        if not isinstance(message, dict):
            logger.warning("whatsapp message item is not an object; skipping: %r", message)
            continue
        try:
            await _handle_message(message, value)
        except Exception as exc:
            if first_error is None:
                first_error = exc
    return first_error
