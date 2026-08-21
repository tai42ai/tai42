"""The Slack inbound doors: the Events API door and the interactivity door.

Both declare ``public: true`` (Slack cannot present the deployment api key) and both
authenticate by the ``X-Slack-Signature`` v0 HMAC over the exact raw body, reading
a bounded body first. Each mounts under the item's resolved route base.

The ``POST /interactive`` door handles Block Kit interactivity for ``form``
questions: a ``block_actions`` ``tai42_form_open`` click opens the modal
(``views.open`` with the payload's ``trigger_id``), and a ``view_submission`` for
``tai42_form_submit`` coerces the state per the stored schema and forwards
``{"answer": <dict>}`` to the callback door. Its body is form-encoded with the
JSON in the ``payload`` field, and the signature covers that raw form body.

The ``POST /inbound`` door is the Events API door. Flow:

1. Read a BOUNDED body (actual bytes, never a client ``Content-Length``) before
   any HMAC work; past the cap a loud 413, never a truncation.
2. Verify the signature over the EXACT raw body bytes — fail CLOSED. An unset or
   empty signing secret is a logged, constant 500. Even ``url_verification`` is
   verified first (an unverified echo would confirm the endpoint to anyone).
3. ``url_verification`` -> echo the challenge.
4. ``event_callback`` -> dedupe on ``event_id``; a reply whose ``thread_ts``
   matches a pending question forwards the typed answer; any other human message
   bridges to its conversation. Ack 200.

Signature and dedupe run before correlation or the bridge — no message reaches
either without passing transport auth first.

The handler works inline (two Redis round-trips + one loopback POST) so the 2xx
lands inside Slack's 3-second window. Any failure after the dedupe claim releases
it before re-raising, so Slack's retry reprocesses the event; the raise surfaces
as a loud 500.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from tai42_contract.app import tai42_app
from tai42_contract.conversations import BlankInboundTextError

from tai42_channel_slack.channel import open_modal_view
from tai42_channel_slack.client import slack_http
from tai42_channel_slack.correlation import (
    claim_dedupe,
    delete_correlation,
    delete_form_record,
    get_callback_url,
    get_form_record,
    release_dedupe,
)
from tai42_channel_slack.forms import (
    FORM_OPEN_ACTION_ID,
    FORM_SUBMIT_CALLBACK_ID,
    build_modal_view,
    extract_answer,
    first_field_name,
    is_declared_field,
)
from tai42_channel_slack.settings import slack_settings

logger = logging.getLogger(__name__)

_SIGNATURE_HEADER = "X-Slack-Signature"
_TIMESTAMP_HEADER = "X-Slack-Request-Timestamp"
_RETRY_NUM_HEADER = "X-Slack-Retry-Num"
_SIGNATURE_PREFIX = "v0="
_HEX_DIGEST_LEN = hashlib.sha256().digest_size * 2  # 64
# Slack's replay window: reject a timestamp more than five minutes from now.
_MAX_TIMESTAMP_SKEW_SECONDS = 300
# Bound what an unauthenticated door reads into memory — loud 413, never truncation.
_MAX_BODY_BYTES = 1 * 1024 * 1024


class _PayloadTooLarge(Exception):
    """The inbound body exceeded ``_MAX_BODY_BYTES`` -> 413."""


class _SignatureRejected(Exception):
    """An ordinary request-side verification failure -> uniform 401."""


def _misconfigured(env_name: str) -> JSONResponse:
    """Fail CLOSED on operator misconfiguration: one logged, constant JSON 500."""
    logger.error("slack inbound: %s is unset or empty; failing closed", env_name)
    return JSONResponse({"error": "channel misconfigured"}, status_code=500)


async def _read_bounded_body(request: Request, cap: int) -> bytes:
    """Read the body on ACTUAL bytes, never a client ``Content-Length``. Raise
    ``_PayloadTooLarge`` past ``cap`` before any HMAC or parse work."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap:
            raise _PayloadTooLarge("request body exceeds the configured cap")
        chunks.append(chunk)
    return b"".join(chunks)


def _verify_signature(raw: bytes, headers: Mapping[str, str], secret: str) -> None:
    """Authenticate ``raw`` against Slack's v0 signing scheme, or raise.

    Every request-side defect raises :class:`_SignatureRejected`, mapped to one
    constant 401 — no oracle distinguishing the failure reason.
    """
    timestamp = headers.get(_TIMESTAMP_HEADER)
    if timestamp is None:
        raise _SignatureRejected(f"missing {_TIMESTAMP_HEADER} header")
    # Gate to ASCII digits before int(): bare int() also accepts unicode whitespace
    # and non-ASCII digits, which must be the same uniform reject.
    if not (timestamp.isascii() and timestamp.isdigit()):
        raise _SignatureRejected(f"{_TIMESTAMP_HEADER} is not an ASCII-digit integer")
    ts_value = int(timestamp)
    if abs(time.time() - ts_value) > _MAX_TIMESTAMP_SKEW_SECONDS:
        raise _SignatureRejected("request timestamp outside the replay window")

    signature = headers.get(_SIGNATURE_HEADER)
    if signature is None:
        raise _SignatureRejected(f"missing {_SIGNATURE_HEADER} header")
    if not signature.startswith(_SIGNATURE_PREFIX):
        raise _SignatureRejected(f"{_SIGNATURE_HEADER} is not prefixed {_SIGNATURE_PREFIX!r}")
    provided_hex = signature[len(_SIGNATURE_PREFIX) :]
    if len(provided_hex) != _HEX_DIGEST_LEN:
        raise _SignatureRejected(f"{_SIGNATURE_HEADER} digest is not {_HEX_DIGEST_LEN} hex characters")
    try:
        provided_digest = bytes.fromhex(provided_hex)
    except ValueError as exc:
        raise _SignatureRejected(f"{_SIGNATURE_HEADER} digest is not valid hex") from exc

    base = b"v0:" + timestamp.encode("ascii") + b":" + raw
    expected_digest = hmac.new(secret.encode("utf-8"), base, hashlib.sha256).digest()
    # Constant-time compare so a mismatch position cannot be timed.
    if not hmac.compare_digest(provided_digest, expected_digest):
        raise _SignatureRejected(f"{_SIGNATURE_HEADER} digest mismatch")


@tai42_app.http.custom_route(
    "/inbound",
    methods=["POST"],
    summary="Slack Events API inbound door (signature-authenticated)",
    tags=["channels"],
    response_model=None,
)
async def slack_inbound(request: Request) -> Response:
    """Receive a Slack Events API delivery, verify it, and route it: a correlated
    threaded reply to its callback URL, any other human message to the bridge.

    Unverifiable requests get a constant 401; a missing signing secret a logged
    500. Verified traffic with nothing to do (the bot's own echoes, empty
    messages) is acked 200 ``ignored`` (Slack needs a 2xx or it retries and
    disables the subscription).
    """
    try:
        raw = await _read_bounded_body(request, _MAX_BODY_BYTES)
    except _PayloadTooLarge:
        # Bounded BEFORE any HMAC work — a loud 413, never a truncated read.
        return JSONResponse({"error": "payload too large"}, status_code=413)
    # Resolve the signing secret first. Unset or empty fails CLOSED — an empty key
    # would make the HMAC forgeable.
    signing_secret = slack_settings().signing_secret
    secret = signing_secret.get_secret_value() if signing_secret is not None else ""
    if not secret:
        return _misconfigured("CHANNEL_SLACK_SIGNING_SECRET")
    try:
        _verify_signature(raw, request.headers, secret)
    except _SignatureRejected as exc:
        # Constant body for every request-side defect; the reason stays in the log.
        logger.warning("slack inbound rejected: %s", exc)
        return JSONResponse({"error": "signature verification failed"}, status_code=401)

    try:
        payload = json.loads(raw)
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

    if payload.get("type") == "url_verification":
        challenge = payload.get("challenge")
        if not isinstance(challenge, str) or not challenge:
            return JSONResponse({"error": "url_verification without challenge"}, status_code=400)
        return JSONResponse({"challenge": challenge})

    if payload.get("type") != "event_callback":
        # A signed envelope we did not subscribe to (e.g. app_rate_limited) — ack
        # so Slack does not retry.
        return JSONResponse({"status": "ignored"})

    event_id = payload.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        return JSONResponse({"error": "event_callback without event_id"}, status_code=400)

    if not await claim_dedupe(event_id):
        # A retry of an already-processed (or in-flight) event: ack it. The
        # callback door's single-use claim is the final guard if mid-forward.
        logger.info(
            "slack inbound duplicate event %s (retry-num=%s)",
            event_id,
            request.headers.get(_RETRY_NUM_HEADER),
        )
        return JSONResponse({"status": "duplicate"})

    try:
        return await _process_event(payload, event_id)
    except BaseException:
        # Processing failed after the dedupe claim: release it so Slack's retry
        # reprocesses, then re-raise (the 500 is the correct signal).
        await release_dedupe(event_id)
        raise


async def _process_event(payload: dict, event_id: str) -> Response:
    """Route one verified, deduped ``event_callback``: a pending-question reply
    forwards to its callback; any other human message bridges to a conversation.

    A pending-question correlation is attempted first and wins. On a miss — a thread
    reply whose question expired or was never ours, a top-level message, or a message
    outside the ask_user allowlist — the message bridges. The bot's own echoes stay
    ignored throughout.
    """
    settings = slack_settings()
    recipients = set(settings.allowed_recipients)
    if settings.default_recipient is not None:
        recipients.add(settings.default_recipient)
    event = payload.get("event")
    if not isinstance(event, dict):
        raise ValueError("event_callback without an event object")

    if event.get("type") != "message" or "subtype" in event or event.get("bot_id") is not None:
        # Not a plain human message: edits/joins/file shares carry a subtype, the
        # bot's own post echoes with a bot_id.
        return JSONResponse({"status": "ignored"})
    if settings.bot_user_id is not None and event.get("user") == settings.bot_user_id:
        # A message the bot itself authored (posted under a user token, no bot_id).
        return JSONResponse({"status": "ignored"})

    text = event.get("text")
    channel = event.get("channel")
    thread_ts = event.get("thread_ts")

    if isinstance(thread_ts, str) and thread_ts and channel in recipients:
        callback_url = await get_callback_url(thread_ts)
        if callback_url is not None:
            return await _forward_answer(callback_url, thread_ts, text, settings.bot_user_id, channel, event_id)

    return await _bridge(settings.bot_user_id, channel, text, event_id)


async def _bridge(our_identity: str | None, channel: object, text: object, event_id: str) -> Response:
    """Hand one uncorrelated human message to the conversation bridge.

    ``our_identity`` (the bot user id) is required here — a bridge route with it
    unset is operator misconfig, raised loudly. A message with no text/channel, or
    with whitespace-only text, is nothing to bridge and is acked. No route bound acks
    with a debug log; an infrastructure failure propagates so Slack redelivers.
    """
    if not isinstance(text, str) or not text or not isinstance(channel, str) or not channel:
        return JSONResponse({"status": "ignored"})
    if our_identity is None:
        raise ValueError("the slack channel bridge is not configured: set CHANNEL_SLACK_BOT_USER_ID")
    try:
        await tai42_app.conversations.accept(
            channel="slack",
            our_identity=our_identity,
            client_address=channel,
            # The provider attests the channel id, so it is both the conversation identity
            # and the party the turn cap holds accountable.
            cap_key=channel,
            text=text,
            provider_message_id=event_id,
        )
    except BlankInboundTextError:
        logger.debug("slack inbound: blank text for channel=%s; acking", channel)
        return JSONResponse({"status": "ignored"})
    except LookupError:
        logger.debug("slack inbound: no conversation route for channel=%s; acking", channel)
        return JSONResponse({"status": "ignored"})
    return JSONResponse({"status": "accepted"})


async def _forward_answer(
    callback_url: str, thread_ts: str, text: object, our_identity: str | None, channel: object, event_id: str
) -> Response:
    """Forward a correlated pending-question reply to its callback door.

    ``our_identity``/``channel``/``event_id`` are the bridge context so a terminal
    404 (the interaction is gone) bridges the reply into the conversation rather than
    dropping it — ``_bridge`` sets ``provider_message_id`` = the event id, so a Slack
    redelivery of the same inbound dedupes at the conversation seam."""
    if not isinstance(text, str) or not text:
        # A CORRELATED reply with no text is an error, not chatter: raise (-> 500,
        # Slack retries then logs) rather than resolve the ask with None.
        raise ValueError(f"correlated slack reply in thread {thread_ts} carries no text")

    async with slack_http() as client:
        response = await client.post(callback_url, json={"answer": text})
    if response.status_code == 200:
        await delete_correlation(thread_ts)
        return JSONResponse({"status": "forwarded"})
    if response.status_code == 404:
        # Terminal: the interaction is gone (pruned/timed-out/cancelled). The dead
        # ticket can't accept the answer, but the human's reply must NEVER be lost —
        # drop the correlation and bridge the reply into the conversation instead.
        logger.warning(
            "slack inbound: callback door returned terminal HTTP 404 for thread_ts=%s; the interaction "
            "is gone — bridging the reply into the conversation instead of dropping it",
            thread_ts,
        )
        await delete_correlation(thread_ts)
        return await _bridge(our_identity, channel, text, event_id)
    if response.status_code == 400:
        # The door rejected THIS answer; keep the correlation so the human can
        # reply again, and ack 200 (redelivering the same text is pointless).
        logger.warning(
            "slack inbound: callback door rejected the answer for thread_ts=%s (400); correlation kept",
            thread_ts,
        )
        return JSONResponse({"status": "rejected"})
    # 401/5xx transient/misconfig — raise (keep the correlation; the guard frees
    # the dedupe claim so Slack's retry re-attempts the forward).
    raise RuntimeError(f"callback forward failed: HTTP {response.status_code} from the interactions door")


# Shown in the modal when the question's form record is gone (TTL lapsed between
# the message and the submission, or the door reports the ticket terminally gone).
_FORM_EXPIRED_TEXT = "This question is no longer available; it has expired."


def _parse_interactive_payload(raw: bytes) -> dict[str, Any] | None:
    """The JSON object in the ``payload`` field of a form-encoded interactivity
    body, or ``None`` for a body that carries no decodable JSON object."""
    try:
        fields = parse_qs(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return None
    values = fields.get("payload")
    if not values:
        return None
    try:
        payload = json.loads(values[0])
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _errors_response(block_id: str | None, text: str) -> JSONResponse:
    """A ``view_submission`` errors response pinning ``text`` under ``block_id``.

    Slack requires the id of a block present in the view; a submission that
    carried none is malformed and raised rather than silently closed.
    """
    if block_id is None:
        raise ValueError("view_submission carried no input block to report the error on")
    return JSONResponse({"response_action": "errors", "errors": {block_id: text}})


def _state_values(view: dict[str, Any]) -> dict[str, Any]:
    """The ``view.state.values`` map (block_id -> action -> value), or ``{}``."""
    state = view.get("state")
    values = state.get("values") if isinstance(state, dict) else None
    return values if isinstance(values, dict) else {}


def _door_error_text(response: Any) -> str:
    """The callback door's 400 message (its JSON ``error`` field), or a generic
    fallback when the body is not the expected shape."""
    try:
        body = response.json()
    except ValueError:
        return "The form could not be submitted."
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, str) and error:
            return error
    return "The form could not be submitted."


def _door_error_block(response: Any, schema: dict[str, Any], fallback: str) -> str:
    """The block_id to pin the door's 400 error under: the door-named ``field`` when
    it is a declared schema property (block_id == field name), else ``fallback`` (the
    first field) for an absent/unknown/nested field."""
    try:
        body = response.json()
    except ValueError:
        return fallback
    if isinstance(body, dict):
        field = body.get("field")
        if isinstance(field, str) and is_declared_field(schema, field):
            return field
    return fallback


@tai42_app.http.custom_route(
    "/interactive",
    methods=["POST"],
    summary="Slack interactivity door (Block Kit form modals, signature-authenticated)",
    tags=["channels"],
    response_model=None,
)
async def slack_interactive(request: Request) -> Response:
    """Receive a Slack interactivity POST, verify it, and act on it.

    Same transport auth as the events door: a bounded raw-body read, then the v0
    HMAC over those exact bytes (fail CLOSED; a missing signing secret a logged
    500). The body is ``application/x-www-form-urlencoded`` with the JSON in the
    ``payload`` field, and the signature covers that raw form body. A
    ``block_actions`` ``tai42_form_open`` opens the form modal; a
    ``view_submission`` for ``tai42_form_submit`` forwards the answer; every other
    payload is acked 200 and ignored (Slack needs a 2xx).
    """
    try:
        raw = await _read_bounded_body(request, _MAX_BODY_BYTES)
    except _PayloadTooLarge:
        return JSONResponse({"error": "payload too large"}, status_code=413)
    signing_secret = slack_settings().signing_secret
    secret = signing_secret.get_secret_value() if signing_secret is not None else ""
    if not secret:
        return _misconfigured("CHANNEL_SLACK_SIGNING_SECRET")
    try:
        _verify_signature(raw, request.headers, secret)
    except _SignatureRejected as exc:
        logger.warning("slack interactive rejected: %s", exc)
        return JSONResponse({"error": "signature verification failed"}, status_code=401)

    payload = _parse_interactive_payload(raw)
    if payload is None:
        return JSONResponse({"error": "body must carry a JSON payload field"}, status_code=400)

    payload_type = payload.get("type")
    if payload_type == "block_actions":
        return await _handle_block_actions(payload)
    if payload_type == "view_submission":
        return await _handle_view_submission(payload)
    # A signed interactivity type we do not handle (shortcuts, other actions): ack
    # so Slack does not retry — matching the events door's unknown-type posture.
    return JSONResponse({"status": "ignored"})


async def _handle_block_actions(payload: dict[str, Any]) -> Response:
    """Open the form modal for a ``tai42_form_open`` click.

    Any other action is acked-ignored. A missing/expired form record (the button
    outlived its question) is acked-ignored too — there is nothing to open. A live
    record opens the modal inline with the payload's short-lived ``trigger_id``; a
    failed ``views.open`` surfaces as a loud 500.
    """
    actions = payload.get("actions")
    action = actions[0] if isinstance(actions, list) and actions and isinstance(actions[0], dict) else None
    if action is None or action.get("action_id") != FORM_OPEN_ACTION_ID:
        return JSONResponse({"status": "ignored"})
    interaction_id = action.get("value")
    if not isinstance(interaction_id, str) or not interaction_id:
        raise ValueError("tai42_form_open action carried no interaction id")
    record = await get_form_record(interaction_id)
    if record is None:
        logger.info("slack interactive: form record %s missing or expired; nothing to open", interaction_id)
        return JSONResponse({"status": "ignored"})
    trigger_id = payload.get("trigger_id")
    if not isinstance(trigger_id, str) or not trigger_id:
        raise ValueError("block_actions payload carried no trigger_id")
    view = build_modal_view(interaction_id, record["question"], record["schema"])
    await open_modal_view(trigger_id, view)
    return JSONResponse({"status": "opened"})


async def _handle_view_submission(payload: dict[str, Any]) -> Response:
    """Forward one ``tai42_form_submit`` submission to its callback door.

    The interaction id rides in ``private_metadata``. A gone form record shows the
    expired notice. Otherwise the state is coerced per the stored schema and posted
    as ``{"answer": <dict>}``: door 2xx closes the modal and drops the record; 400
    keeps the record and shows the door's message; 404 shows the expired notice and
    drops the record; anything else raises (loud 500, record kept).
    """
    view = payload.get("view")
    if not isinstance(view, dict) or view.get("callback_id") != FORM_SUBMIT_CALLBACK_ID:
        return JSONResponse({"status": "ignored"})
    interaction_id = view.get("private_metadata")
    if not isinstance(interaction_id, str) or not interaction_id:
        raise ValueError("tai42_form_submit view carried no private_metadata")
    state_values = _state_values(view)
    record = await get_form_record(interaction_id)
    if record is None:
        return _errors_response(next(iter(state_values), None), _FORM_EXPIRED_TEXT)

    schema = record["schema"]
    answer = extract_answer(schema, state_values)
    async with slack_http() as client:
        response = await client.post(record["callback_url"], json={"answer": answer})
    status = response.status_code
    first_block = first_field_name(schema)
    if status == 200:
        await delete_form_record(interaction_id)
        # An empty body closes the modal.
        return JSONResponse({})
    if status == 400:
        # The door rejected THIS answer; keep the record so the human can correct it.
        # Pin the error under the failing field's block when the door named one, else
        # the first field.
        logger.warning("slack interactive: door rejected form %s (400); record kept", interaction_id)
        return _errors_response(_door_error_block(response, schema, first_block), _door_error_text(response))
    if status == 404:
        logger.warning("slack interactive: door reports form %s ticket terminally gone (404)", interaction_id)
        await delete_form_record(interaction_id)
        return _errors_response(first_block, _FORM_EXPIRED_TEXT)
    raise RuntimeError(f"callback forward failed: HTTP {status} from the interactions door")
