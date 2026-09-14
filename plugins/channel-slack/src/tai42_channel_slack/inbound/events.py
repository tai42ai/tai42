"""The Slack Events API door — ``POST /inbound``.

Public (Slack cannot present the deployment api key) and signature-authenticated
over the exact raw body. Flow:

1. Read a BOUNDED, verified body (413 past the cap, 500 on a missing secret, 401
   on a signature defect) before any event work.
2. ``url_verification`` -> echo the challenge (verified first: an unverified echo
   would confirm the endpoint to anyone).
3. ``event_callback`` -> dedupe on ``event_id``; a reply whose ``thread_ts``
   matches a pending question forwards the typed answer; any other human message
   bridges to its conversation. Ack 200.

The handler works inline (two Redis round-trips + one loopback POST) so the 2xx
lands inside Slack's 3-second window. Any failure after the dedupe claim releases
it before re-raising, so Slack's retry reprocesses the event; the raise surfaces
as a loud 500.
"""

from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from tai42_contract.app import tai42_app

from tai42_channel_slack.correlation import claim_dedupe, release_dedupe
from tai42_channel_slack.inbound.routing import _bridge, _recipients, _resolve_answer
from tai42_channel_slack.inbound.verification import _InboundRejected, _read_verified_body
from tai42_channel_slack.settings import slack_settings

logger = logging.getLogger(__name__)

_RETRY_NUM_HEADER = "X-Slack-Retry-Num"


@tai42_app.http.custom_route(
    "/inbound",
    methods=["POST"],
    summary="Slack Events API inbound door (signature-authenticated)",
    tags=["channels"],
    response_model=None,
    no_body_reason="Slack Events API webhook: url_verification challenge / vendor ack",
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
        raw = await _read_verified_body(request)
    except _InboundRejected as rejected:
        return rejected.response

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
    recipients = _recipients(settings)
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

    if isinstance(thread_ts, str) and thread_ts and isinstance(channel, str) and channel in recipients:
        return await _resolve_answer(thread_ts, text, settings.bot_user_id, channel, event_id)

    return await _bridge(settings.bot_user_id, channel, text, event_id)
