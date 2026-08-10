"""Inbound WhatsApp webhook — where the human's reply and delivery
receipts enter the system.

``/api/channels/whatsapp/inbound`` is unauthenticated (Meta cannot send the
platform api key):

* ``GET`` is Meta's subscription handshake — echo ``hub.challenge`` in plaintext
  IFF ``hub.verify_token`` matches the configured token (constant-time), else 403.
* ``POST`` carries message and delivery-status events, signed with
  ``X-Hub-Signature-256`` = ``sha256=<hex>`` HMAC-SHA256 over the RAW body,
  validated fail-closed BEFORE the body is parsed. A missing configured app
  secret is a loud misconfiguration (logged 500), never a skipped check.

Meta's signature scheme carries no timestamp, so the ``wamid`` dedupe is the
replay guard. A reply matching a pending question is forwarded as ``{"answer":
<text verbatim, outer whitespace stripped>}``; on a correlation miss the message
enters the conversation bridge instead.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Iterator
from typing import Any

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from tai42_contract.app import tai42_app
from tai42_contract.conversations import BlankInboundTextError, DeliveryReceipt
from tai42_kit.clients.impl.http import HttpxClient
from tai42_kit.settings import require_secret

from tai42_channel_whatsapp.correlation import (
    PendingQuestion,
    already_seen,
    mark_known_contact,
    mark_seen,
    peek_pending,
    pop_pending,
    restore_pending,
)
from tai42_channel_whatsapp.settings import whatsapp_settings

logger = logging.getLogger(__name__)

_SIGNATURE_HEADER = "X-Hub-Signature-256"
_SIGNATURE_PREFIX = "sha256="
# Bound what an unauthenticated door reads into memory — loud 413, never truncation.
_MAX_BODY_BYTES = 1 * 1024 * 1024

# WhatsApp delivery status → the terminal receipt to record. "read" is
# informational (ignored); any other status (e.g. a future one) is a no-op.
_DELIVERY_RECEIPTS = {
    "failed": DeliveryReceipt.FAILED,
    "sent": DeliveryReceipt.DELIVERED,
    "delivered": DeliveryReceipt.DELIVERED,
}


class SignatureRejectedError(Exception):
    """The request failed X-Hub-Signature-256 authentication (mapped to 401)."""


class PayloadTooLargeError(Exception):
    """The inbound body exceeded the unauthenticated door's byte cap (mapped to 413)."""


class AnswerForwardError(Exception):
    """The interactions callback door did not accept the forwarded answer."""


async def _read_bounded_body(request: Request, cap: int) -> bytes:
    """Read the body counting ACTUAL bytes, never a client ``Content-Length``.
    Raises ``PayloadTooLargeError`` past ``cap`` before any signature work."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap:
            raise PayloadTooLargeError("request body exceeds the configured cap")
        chunks.append(chunk)
    return b"".join(chunks)


def _validate_signature(app_secret: str, body: bytes, provided: str | None) -> None:
    """Validate ``X-Hub-Signature-256`` over the raw body or raise
    ``SignatureRejectedError``. Header form ``sha256=<hex>``; compared
    constant-time against the HMAC-SHA256 of the body under the app secret."""
    if provided is None:
        raise SignatureRejectedError(f"missing {_SIGNATURE_HEADER} header")
    if not provided.startswith(_SIGNATURE_PREFIX):
        raise SignatureRejectedError(f"{_SIGNATURE_HEADER} is not in sha256=<hex> form")
    try:
        # Decode to bytes first: a non-hex / non-ASCII header is a 401, never a
        # compare_digest TypeError (which would surface as a 500).
        provided_digest = bytes.fromhex(provided[len(_SIGNATURE_PREFIX) :])
    except ValueError as exc:
        raise SignatureRejectedError(f"{_SIGNATURE_HEADER} is not valid hex") from exc
    expected = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).digest()
    if not hmac.compare_digest(provided_digest, expected):
        raise SignatureRejectedError(f"{_SIGNATURE_HEADER} mismatch")


async def _authenticated_body(request: Request) -> bytes:
    """Bounded-read and signature-validate the POST body; return the RAW bytes.
    Nothing in the body is trusted until the signature validates. Raises
    ``ValueError`` (app secret unset → logged 500), ``PayloadTooLargeError``
    (→ 413), or ``SignatureRejectedError`` (→ 401)."""
    app_secret = require_secret(whatsapp_settings().app_secret, "WhatsApp channel", "CHANNEL_WHATSAPP_APP_SECRET")
    raw = await _read_bounded_body(request, _MAX_BODY_BYTES)
    _validate_signature(app_secret, raw, request.headers.get(_SIGNATURE_HEADER))
    return raw


def _auth_error_response(exc: ValueError | PayloadTooLargeError | SignatureRejectedError) -> Response:
    """Map an ``_authenticated_body`` failure to its response: 413 oversize, 401
    bad signature, 500 for an unset app secret (operator misconfig, never a 401
    that reads like an ordinary bad signature)."""
    if isinstance(exc, PayloadTooLargeError):
        return PlainTextResponse("payload too large", status_code=413)
    if isinstance(exc, SignatureRejectedError):
        logger.warning("rejected whatsapp inbound: %s", exc)
        return PlainTextResponse("signature verification failed", status_code=401)
    logger.error("whatsapp inbound: CHANNEL_WHATSAPP_APP_SECRET is unset or empty; failing closed")
    return JSONResponse({"error": "channel misconfigured"}, status_code=500)


async def _forward_answer(callback_url: str, answer: str) -> httpx.Response:
    """POST ``{"answer": <value>}`` to the interaction's callback door and return
    its response; the caller applies the status policy."""
    async with tai42_app.clients.client_ctx(HttpxClient, timeout=whatsapp_settings().http_timeout_seconds) as (client):
        return await client.post(callback_url, json={"answer": answer})


def _verify_handshake(request: Request) -> Response:
    """Meta's GET subscription handshake: echo ``hub.challenge`` iff
    ``hub.verify_token`` matches the configured token (constant-time), else 403.
    An unset verify token is a loud misconfiguration (logged 500)."""
    params = request.query_params
    if params.get("hub.mode") != "subscribe":
        return PlainTextResponse("unsupported hub.mode", status_code=403)
    try:
        expected = require_secret(whatsapp_settings().verify_token, "WhatsApp channel", "CHANNEL_WHATSAPP_VERIFY_TOKEN")
    except ValueError:
        logger.error("whatsapp verify: CHANNEL_WHATSAPP_VERIFY_TOKEN is unset or empty; failing closed")
        return JSONResponse({"error": "channel misconfigured"}, status_code=500)
    provided = params.get("hub.verify_token")
    # A non-ASCII token can never match the configured token and would raise a
    # compare_digest TypeError (a 500); treat it as a mismatch (403).
    if provided is None or not provided.isascii() or not hmac.compare_digest(provided, expected):
        logger.warning("rejected whatsapp verify: hub.verify_token mismatch")
        return PlainTextResponse("verification failed", status_code=403)
    challenge = params.get("hub.challenge")
    if challenge is None:
        return PlainTextResponse("missing hub.challenge", status_code=400)
    return PlainTextResponse(challenge)


def _iter_values(payload: Any) -> Iterator[dict[str, Any]]:
    """Every ``entry[].changes[].value`` object in a webhook payload.

    A webhook batches: multiple entries, changes, messages and statuses in one
    POST. Non-object ``entry``/``change`` items are skipped so a well-signed but
    odd payload never crashes the door.
    """
    if not isinstance(payload, dict):
        return
    entries = payload.get("entry")
    if not isinstance(entries, list):
        return
    for entry in entries:
        if not isinstance(entry, dict):
            logger.warning("whatsapp entry is not an object; skipping: %r", entry)
            continue
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                logger.warning("whatsapp change is not an object; skipping: %r", change)
                continue
            value = change.get("value")
            if isinstance(value, dict):
                yield value


def _as_list(value: Any) -> list[Any]:
    """The value if it is a JSON array, else an empty list."""
    return value if isinstance(value, list) else []


@tai42_app.http.custom_route(
    "/api/channels/whatsapp/inbound",
    methods=["GET", "POST"],
    summary="WhatsApp webhook (verification + messages + delivery statuses)",
    tags=["channels"],
    response_model=None,
    authed=False,
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
    except (ValueError, PayloadTooLargeError, SignatureRejectedError) as exc:
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


async def _handle_message(message: dict[str, Any], value: dict[str, Any]) -> None:
    """Resolve one inbound message's pending question, or route it to the bridge.
    A message lacking a string id is odd and skipped (logged)."""
    wamid = message.get("id")
    if not isinstance(wamid, str) or not wamid:
        logger.warning("whatsapp message missing a string id; skipping: %r", message)
        return

    metadata = value.get("metadata")
    phone_number_id = metadata.get("phone_number_id", "") if isinstance(metadata, dict) else ""
    wa_id = message.get("from", "")

    # Record the known-contact marker for EVERY authenticated inbound BEFORE the
    # message-type drop: a guest who sent only a photo still opened Meta's window.
    if phone_number_id and wa_id:
        await mark_known_contact(phone_number_id, wa_id)

    message_type = message.get("type")
    if message_type == "text":
        await _handle_text(message, phone_number_id, wa_id, wamid)
    elif message_type == "interactive":
        await _handle_interactive(message, phone_number_id, wa_id, wamid)
    else:
        logger.debug("non-text whatsapp message %s ignored", wamid)


async def _handle_text(message: dict[str, Any], phone_number_id: str, wa_id: str, wamid: str) -> None:
    """A typed reply: forward it to a pending question, else route to the bridge."""
    if await already_seen(wamid):
        return
    text_field = message.get("text")
    body = text_field.get("body") if isinstance(text_field, dict) else None
    text = body if isinstance(body, str) else ""

    pending = await pop_pending(phone_number_id, wa_id)
    if pending is None:
        # No pending question (unrelated text or expired) — route to the bridge.
        await _bridge_inbound(phone_number_id, wa_id, text, wamid)
        return
    # A typed reply answers with its body minus outer whitespace.
    await _forward_to_callback(phone_number_id, wa_id, text.strip(), wamid, pending)


def _extract_interactive_reply(interactive: Any) -> tuple[str | None, str]:
    """The tapped ``(id, title)`` from an interactive reply, or ``(None, "")``.

    ``id`` is None when the button/list reply is missing or malformed; ``title``
    is the human-readable label bridged when the tap is not an answer.
    """
    if not isinstance(interactive, dict):
        return None, ""
    reply_type = interactive.get("type")
    reply = interactive.get(reply_type) if isinstance(reply_type, str) else None
    if reply_type not in ("button_reply", "list_reply") or not isinstance(reply, dict):
        return None, ""
    reply_id = reply.get("id")
    title = reply.get("title")
    return (reply_id if isinstance(reply_id, str) else None), (title if isinstance(title, str) else "")


def _map_tap_to_answer(reply_id: str | None, pending: PendingQuestion) -> str | None:
    """The option text a tap answers, or ``None`` when the tap is NOT an answer.

    A tap answers only when its id's interaction part EQUALS the pending ask's and
    its index is in range for that ask's options. A malformed id, an out-of-range
    index, a mismatched interaction part (a stale button from an earlier ask), or a
    pending question with no options (a text ask) all yield ``None`` — the caller
    restores the pending question and bridges the tap's title instead.
    """
    if reply_id is None or pending.options is None or pending.interaction_id is None:
        return None
    interaction_part, separator, index_part = reply_id.rpartition(":")
    if not separator or interaction_part != pending.interaction_id or not index_part.isdigit():
        return None
    try:
        index = int(index_part)
    except ValueError:
        # ``isdigit()`` is True for Unicode digit characters (e.g. "²") and for
        # absurdly-long digit strings, both of which ``int()`` rejects — a
        # non-answer, never a propagating 5xx that has Meta redeliver forever.
        return None
    if index >= len(pending.options):
        return None
    return pending.options[index]


async def _handle_interactive(message: dict[str, Any], phone_number_id: str, wa_id: str, wamid: str) -> None:
    """A button tap or list pick: map it to a pending ask's option, else bridge
    the tap's title.

    A tap whose id matches the pending ask answers it (``options[index]``). A tap
    with no pending question, or one whose id is stale/malformed/out-of-range, is
    NOT an answer: the pending question is left untouched and the tap's title goes
    to the bridge like any unrelated message — never a 5xx that would have Meta
    redeliver the poison tap forever.

    The pending question is read NON-destructively first (``peek_pending``) and only
    popped once the tap is confirmed a real answer, so a stale/malformed tap never
    claims a live ask that a concurrent genuine reply from the same pair could still
    answer.
    """
    if await already_seen(wamid):
        return
    reply_id, title = _extract_interactive_reply(message.get("interactive"))

    # Decide before destructively popping: peek the pending ask and check the tap
    # against it. A non-answer must not remove the pending — bridge and leave it.
    pending = await peek_pending(phone_number_id, wa_id)
    if pending is None or _map_tap_to_answer(reply_id, pending) is None:
        await _bridge_inbound(phone_number_id, wa_id, title, wamid)
        return

    # A real answer — claim it now (atomic GETDEL). ``None`` means a concurrent
    # reply already claimed it: bridge the title so the same ask is never answered
    # twice.
    popped = await pop_pending(phone_number_id, wa_id)
    if popped is None:
        await _bridge_inbound(phone_number_id, wa_id, title, wamid)
        return
    answer = _map_tap_to_answer(reply_id, popped)
    if answer is None:
        # A different ask reserved the pair in the tiny peek→pop gap — this tap is
        # not an answer to it; restore that ask and bridge the tap's title.
        await restore_pending(phone_number_id, wa_id, popped)
        await _bridge_inbound(phone_number_id, wa_id, title, wamid)
        return
    await _forward_to_callback(phone_number_id, wa_id, answer, wamid, popped)


async def _forward_to_callback(
    phone_number_id: str, wa_id: str, answer: str, wamid: str, pending: PendingQuestion
) -> None:
    """Forward a correlated reply to the callback door and apply the status policy:
    2xx answered; 404 terminal drop; 400 kept for a re-reply; anything else restores
    the correlation and raises so Meta's retry re-resolves — the answer is never lost.

    ``answer`` is already final: a typed reply's stripped body, or the option text
    an interactive tap resolved to.
    """
    try:
        forwarded = await _forward_answer(pending.callback_url, answer)
    except Exception:
        # Transport failure — restore so the webhook retry or the human's next
        # message still resolves it.
        await restore_pending(phone_number_id, wa_id, pending)
        raise

    if forwarded.status_code // 100 == 2:
        await mark_seen(wamid)
        return
    if forwarded.status_code == 404:
        # Terminal: the ticket is gone; retrying can't succeed, so stay dropped
        # and ack (no redelivery storm on a dead ticket).
        logger.warning("callback door returned terminal HTTP 404 for %s; dropping the correlation", wamid)
        await mark_seen(wamid)
        return
    if forwarded.status_code == 400:
        # The door rejected THIS answer; keep the correlation so the human can
        # re-reply, and ack (the same body would be rejected again).
        logger.warning(
            "callback door rejected the answer for %s (400); correlation kept so the human can re-reply", wamid
        )
        await restore_pending(phone_number_id, wa_id, pending)
        await mark_seen(wamid)
        return
    # 401/413/5xx ambient failure — keep the correlation and fail loudly; Meta's
    # retry re-pops the restored question and forwards again.
    await restore_pending(phone_number_id, wa_id, pending)
    raise AnswerForwardError(
        f"interactions callback rejected the answer: HTTP {forwarded.status_code}: {forwarded.text[:500]}"
    )


async def _bridge_inbound(phone_number_id: str, wa_id: str, text: str, wamid: str) -> None:
    """Route an uncorrelated inbound message into the conversation bridge.

    ``our_identity`` = phone_number_id, ``client_address`` = wa_id (verbatim). A
    message with no route bound, or with blank text (a media-only message or an
    empty interactive title), is logged and skipped; a retryable overflow or
    infrastructure failure propagates as a 5xx so Meta redelivers.
    """
    try:
        await tai42_app.conversations.accept(
            channel="whatsapp",
            our_identity=phone_number_id,
            client_address=wa_id,
            # The provider attests the wa_id, so it is both the conversation identity and
            # the party the turn cap holds accountable.
            cap_key=wa_id,
            text=text,
            provider_message_id=wamid,
        )
    except BlankInboundTextError as exc:
        logger.warning("blank whatsapp inbound %s dropped: %s", wamid, exc)
    except LookupError as exc:
        logger.warning("unrouted whatsapp inbound %s dropped: %s", wamid, exc)
    await mark_seen(wamid)


async def _handle_status(status: dict[str, Any]) -> None:
    """Record one delivery status against its outbound message.

    ``failed`` → FAILED (loud); ``sent``/``delivered`` → DELIVERED; ``read`` is
    informational (debug-ignored). A status for a ``wamid`` the bridge does not
    track (``record_delivery_status`` raises ``LookupError``) is acked, never a
    5xx — the provider must not retry a message we do not own.
    """
    wamid = status.get("id")
    state = status.get("status")
    if not isinstance(wamid, str) or not wamid or not isinstance(state, str) or not state:
        logger.warning("whatsapp status entry missing string id/status; skipping: %r", status)
        return
    if state == "read":
        logger.debug("whatsapp read receipt for %s ignored", wamid)
        return
    receipt = _DELIVERY_RECEIPTS.get(state)
    if receipt is None:
        logger.debug("whatsapp status %r for %s ignored", state, wamid)
        return
    if receipt is DeliveryReceipt.FAILED:
        logger.warning("whatsapp reported delivery failure for %s: %r", wamid, status.get("errors"))
    try:
        await tai42_app.conversations.record_delivery_status("whatsapp", wamid, receipt)
    except LookupError as exc:
        logger.info("whatsapp status for untracked message %s ignored: %s", wamid, exc)
