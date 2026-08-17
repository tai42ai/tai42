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
<value>}`` — the text verbatim (outer whitespace stripped) for a text reply, the
resolved option for an interactive tap, or the schema-coerced dict for a
completed Flow form (``nfm_reply``); on a correlation miss the message enters the
conversation bridge instead.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from tai42_contract.app import tai42_app
from tai42_contract.conversations import BlankInboundTextError, DeliveryReceipt
from tai42_kit.clients.impl.http import HttpxClient
from tai42_kit.settings import require_secret

from tai42_channel_whatsapp.client import send_flow, send_message
from tai42_channel_whatsapp.correlation import (
    PendingQuestion,
    already_seen,
    get_cached_flow_id,
    mark_known_contact,
    mark_seen,
    peek_pending,
    pop_pending,
    restore_pending,
)
from tai42_channel_whatsapp.flows import build_flow
from tai42_channel_whatsapp.settings import require_delivery_setting, whatsapp_settings

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

# How many times a door-rejected form answer is recovered by re-sending a fresh
# Flow before the guest is told it could not be processed and the ask is left to
# time out. Each re-send spends a slot of the callback door's own rate limit
# (keyed on this server's egress IP, shared across every channel), so the loop is
# bounded — matching the web channel's answer-restore cap in spirit.
_MAX_FORM_REJECTIONS = 5

# The lead-in prefixed to the door's own rejection message in a re-sent Flow body.
_FORM_REJECTION_LEAD = "Your last answer could not be accepted:"
# Shown once when the re-send cap is spent — the ask then times out on its side.
_FORM_UNPROCESSABLE = "Sorry, your form could not be processed."
# Shown in place of a 400 body that is not this platform's error envelope (a proxy
# or WAF page); the guest is never shown an intermediary's content.
_CALLBACK_REJECTION_OPAQUE = "the answer could not be accepted"
# The door's own rejection line is bounded before it rides the guest-facing re-sent
# Flow body — it names the failing field, never an intermediary's whole page.
_DOOR_REJECTION_MAX_CHARS = 500
# Meta caps interactive.body.text at 1024 chars (WhatsApp Cloud API interactive
# message limit); a longer body is a non-retryable param error that fails the
# re-send forever, stranding the guest with neither the Flow nor a final message.
_FLOW_BODY_MAX_CHARS = 1024
# The lead + bounded door line alone always fit the cap, so the overflow fallback
# (drop the whole question) is guaranteed deliverable — verified, not assumed.
assert len(_FORM_REJECTION_LEAD) + 1 + _DOOR_REJECTION_MAX_CHARS <= _FLOW_BODY_MAX_CHARS


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


async def _forward_answer(callback_url: str, answer: str | dict[str, Any]) -> httpx.Response:
    """POST ``{"answer": <value>}`` to the interaction's callback door and return
    its response; the caller applies the status policy. ``answer`` is a string for a
    text/select reply or a dict for a completed form (Flow) reply."""
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
    "/inbound",
    methods=["GET", "POST"],
    summary="WhatsApp webhook (verification + messages + delivery statuses)",
    tags=["channels"],
    response_model=None,
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
    interactive = message.get("interactive")
    if isinstance(interactive, dict) and interactive.get("type") == "nfm_reply":
        # A completed WhatsApp Flow form arrives as an nfm_reply, not a button/list tap.
        await _handle_form_reply(interactive, phone_number_id, wa_id, wamid)
        return
    reply_id, title = _extract_interactive_reply(interactive)

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
    """One form value coerced to its schema type. Flow number inputs arrive as
    strings, so ``integer``/``number``/``boolean`` are coerced ONLY when the value
    is a string (an OptIn may already deliver a bool). A value that fails coercion
    is returned raw — the door's 400 path then restores the pending ask."""
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
    """The form answer forwarded to the door: ``response`` minus ``flow_token``,
    each value coerced to its schema property's type."""
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    props = properties if isinstance(properties, dict) else {}
    return {key: _coerce_value(value, props.get(key)) for key, value in response.items() if key != "flow_token"}


async def _handle_form_reply(interactive: dict[str, Any], phone_number_id: str, wa_id: str, wamid: str) -> None:
    """A completed Flow form (``nfm_reply``): forward the coerced answer dict to the
    pending form question, else bridge.

    The reply matches ONLY when its ``flow_token`` equals the pending ask's
    ``interaction_id``; a malformed ``response_json``, a missing/mismatched token,
    or no pending ask bridges the message (its shape carries no human-readable
    title, so a blank bridge — the same shape an uncorrelated interactive takes).
    The pending ask is peeked before the destructive pop so a non-answer never
    claims a live ask a concurrent genuine reply could still answer.
    """
    response = _extract_form_response(interactive)
    if response is None:
        logger.warning("whatsapp nfm_reply %s carried no JSON-object response_json; bridging", wamid)
        await _bridge_inbound(phone_number_id, wa_id, "", wamid)
        return

    flow_token = response.get("flow_token")
    pending = await peek_pending(phone_number_id, wa_id)
    if pending is None or not isinstance(flow_token, str) or flow_token != pending.interaction_id:
        await _bridge_inbound(phone_number_id, wa_id, "", wamid)
        return

    popped = await pop_pending(phone_number_id, wa_id)
    if popped is None or popped.interaction_id != flow_token:
        # A concurrent reply claimed the ask, or a different ask reserved the pair in
        # the peek→pop gap — this form is not an answer to it; restore and bridge.
        if popped is not None:
            await restore_pending(phone_number_id, wa_id, popped)
        await _bridge_inbound(phone_number_id, wa_id, "", wamid)
        return

    answer = _coerce_form_answer(response, popped.schema)
    await _forward_to_callback(phone_number_id, wa_id, answer, wamid, popped)


async def _forward_to_callback(
    phone_number_id: str, wa_id: str, answer: str | dict[str, Any], wamid: str, pending: PendingQuestion
) -> None:
    """Forward a correlated reply to the callback door and apply the status policy:
    2xx answered; 404 terminal drop; anything else restores the correlation and raises
    so Meta's retry re-resolves — the answer is never lost. A 400 branches on the ask
    kind: a text/select ask is kept for a re-reply in place, while a form ask (a
    completed Flow, which has no re-reply surface) is recovered by re-sending a fresh
    Flow carrying the door's error (bounded).

    ``answer`` is already final: a typed reply's stripped body, the option text an
    interactive tap resolved to, or the coerced dict of a completed form (Flow).
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
        if pending.schema is not None:
            # A form ask: the completed Flow has no re-reply surface, so a kept
            # correlation would strand the guest. Recover by re-sending a fresh Flow.
            await _recover_form_rejection(phone_number_id, wa_id, wamid, pending, forwarded)
            return
        # A text/select ask CAN be re-replied to in place; keep the correlation and
        # ack (the same body would be rejected again).
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


async def _recover_form_rejection(
    phone_number_id: str, wa_id: str, wamid: str, pending: PendingQuestion, forwarded: httpx.Response
) -> None:
    """Recover a form answer the callback door rejected (400) by re-sending a fresh
    Flow for the SAME interaction, bounded by ``_MAX_FORM_REJECTIONS``.

    The pending record is already popped by the caller. Ordering is load-bearing for
    Meta's redelivery:

    * Under the cap — re-send a fresh Flow (same ``flow_token`` = ``interaction_id``,
      same cached flow id, body = the question plus the door's error line), then keep
      the pending alive with the rejection counted and mark the wamid seen. A re-send
      that itself fails restores the pending UNCHANGED and does NOT mark the wamid
      seen, then raises — so Meta's redelivery re-pops the same ask, re-hits the 400,
      and re-enters this path (the counter is spent only by a re-send that reached the
      guest).
    * At the cap — leave the pending popped (a later text then bridges normally), tell
      the guest once the form could not be processed, and let the ask time out on its
      side.
    """
    if pending.interaction_id is None or pending.schema is None or pending.question is None:
        raise AnswerForwardError(
            f"cannot recover the form rejection for {wamid}: the pending record is missing its "
            "interaction id, schema, or question text"
        )
    if pending.rejections >= _MAX_FORM_REJECTIONS:
        logger.error(
            "form answer for %s rejected %d times (cap %d); not re-sending — telling the guest and "
            "letting the ask time out",
            wamid,
            pending.rejections,
            _MAX_FORM_REJECTIONS,
        )
        await send_message(phone_number_id=phone_number_id, to=wa_id, body=_FORM_UNPROCESSABLE)
        await mark_seen(wamid)
        return

    body_text = _rejection_body(pending.question, _door_rejection_line(forwarded))
    try:
        flow_id = await _cached_form_flow_id(pending.schema)
        await send_flow(
            phone_number_id=phone_number_id,
            to=wa_id,
            body_text=body_text,
            flow_id=flow_id,
            flow_token=pending.interaction_id,
        )
    except Exception:
        # A re-send that fails must NOT mark the wamid seen: restore the pending
        # unchanged so Meta's redelivery re-pops it and re-enters this 400 path.
        await restore_pending(phone_number_id, wa_id, pending)
        raise
    await restore_pending(phone_number_id, wa_id, replace(pending, rejections=pending.rejections + 1))
    await mark_seen(wamid)


async def _cached_form_flow_id(schema: dict[str, Any]) -> str:
    """The published flow id for a form ask's schema, from the cache the original send
    populated. The cache has no TTL, so a miss means the store was lost — a loud
    failure that re-sends nothing, never a silent skip of the recovery."""
    _, schema_hash = build_flow(schema)
    waba_id = require_delivery_setting(whatsapp_settings().waba_id, "CHANNEL_WHATSAPP_WABA_ID")
    flow_id = await get_cached_flow_id(waba_id, schema_hash)
    if flow_id is None:
        raise AnswerForwardError(
            f"cannot re-send a form for the rejected answer: no published flow cached for its schema under {waba_id}"
        )
    return flow_id


def _rejection_body(question: str, error_line: str) -> str:
    """The re-sent Flow's body: the question, then the door's rejection line.

    When the composed body would exceed ``_FLOW_BODY_MAX_CHARS`` the question is
    dropped WHOLE — the fresh Flow re-presents the fields, so a mid-string ellipsis
    (forbidden by the no-silent-truncation posture) is never needed. The lead+error
    tail is bounded (``_door_rejection_line`` caps the error at
    ``_DOOR_REJECTION_MAX_CHARS``) so it always fits the cap."""
    tail = f"{_FORM_REJECTION_LEAD} {error_line}"
    full = f"{question}\n\n{tail}"
    return full if len(full) <= _FLOW_BODY_MAX_CHARS else tail


def _door_rejection_line(forwarded: httpx.Response) -> str:
    """The guest-facing error line for the re-sent Flow: the callback door's OWN 400
    message (which names the failing field), bounded to ``_DOOR_REJECTION_MAX_CHARS``.
    A body that is not this platform's error envelope (a proxy/WAF page) is replaced
    and logged — the guest is never shown an intermediary's content."""
    try:
        payload = forwarded.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("error"), str):
        return payload["error"][:_DOOR_REJECTION_MAX_CHARS]
    logger.warning(
        "callback door rejected a form answer with a 400 body that is not this platform's error envelope; "
        "the guest is told nothing of it: %s",
        forwarded.text[:500],
    )
    return _CALLBACK_REJECTION_OPAQUE


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
