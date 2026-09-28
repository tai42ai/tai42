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
from tai42_contract.conversations import (
    InboundMediaKind,
    InboundRejectionReason,
    build_inbound_media_params,
    inbound_media_placeholder,
)

from tai42_channel_slack.correlation import claim_dedupe, release_dedupe
from tai42_channel_slack.inbound.routing import _bridge, _recipients, _resolve_answer
from tai42_channel_slack.inbound.verification import _InboundRejectedError, _read_verified_body
from tai42_channel_slack.settings import slack_settings

logger = logging.getLogger(__name__)

_RETRY_NUM_HEADER = "X-Slack-Retry-Num"

# Message subtypes whose top-level ``text`` is the participant's own words:
# ``me_message`` (a ``/me …`` post) and ``thread_broadcast`` (a threaded reply also
# posted to the channel, carrying ``thread_ts``). They route exactly like a plain
# message. Every other subtype's payload is not a participant utterance.
_PARTICIPANT_TEXT_SUBTYPES = frozenset({"me_message", "thread_broadcast"})


@tai42_app.http.custom_route(
    "/inbound",
    methods=["POST"],
    summary="Slack Events API inbound door (signature-authenticated)",
    tags=["channels"],
    response_model=None,
    no_body_reason="Slack Events API webhook: url_verification challenge / vendor ack",
)
async def slack_inbound(request: Request) -> Response:
    """Receive a Slack Events API delivery, verify it, and route it.

    A correlated threaded reply goes to its callback URL; any other human message goes
    to the bridge. Unverifiable requests get a constant 401; a missing signing secret a logged
    500. Verified traffic with nothing to do (the bot's own echoes, empty
    messages) is acked 200 ``ignored`` (Slack needs a 2xx or it retries and
    disables the subscription).
    """
    try:
        raw = await _read_verified_body(request)
    except _InboundRejectedError as rejected:
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
    """Route one verified, deduped ``event_callback``.

    A pending-question reply forwards to its callback; any other human message bridges
    to a conversation. A pending-question correlation is attempted first and wins. On a miss — a thread
    reply whose question expired or was never ours, a top-level message, or a message
    outside the ask allowlist — the message bridges. The bot's own echoes stay
    ignored throughout.
    """
    settings = slack_settings()
    recipients = _recipients(settings)
    event = payload.get("event")
    if not isinstance(event, dict):
        raise ValueError("event_callback without an event object")  # noqa: TRY004 raised type is intentional (invariant/state/validation taxonomy); TypeError would change behaviour

    if event.get("type") != "message" or event.get("bot_id") is not None:
        # Not a message event, or the bot's own post echoing with a bot_id.
        return JSONResponse({"status": "ignored"})
    if settings.bot_user_id is not None and event.get("user") == settings.bot_user_id:
        # A message the bot itself authored (posted under a user token, no bot_id).
        return JSONResponse({"status": "ignored"})

    subtype = event.get("subtype")
    if subtype == "file_share":
        # A file share is participant content and bridges one turn per file.
        return await _bridge_files(event, settings.bot_user_id, event_id)
    if subtype is not None and subtype not in _PARTICIPANT_TEXT_SUBTYPES:
        # A subtype whose payload is not a participant utterance is not content this
        # door represents and stays ack-ignored: edits such as message_changed and
        # message_deleted, joins and leaves, bot_message, pins, and the message_replied
        # container. A participant-text subtype falls through to the message path below.
        return JSONResponse({"status": "ignored"})

    text = event.get("text")
    channel = event.get("channel")
    thread_ts = event.get("thread_ts")

    if isinstance(thread_ts, str) and thread_ts and isinstance(channel, str) and channel in recipients:
        return await _resolve_answer(thread_ts, text, settings.bot_user_id, channel, event_id)

    return await _bridge(settings.bot_user_id, channel, text, event_id)


def _media_kind(mimetype: str | None) -> InboundMediaKind:
    """Map a Slack file ``mimetype`` onto the generic inbound-media wire kind.

    By the mime major type: ``image``/``video``/``audio`` map to themselves,
    ``application``/``text`` to ``document``; any other present major type
    (e.g. ``model``/``font``) and a missing/blank ``mimetype`` (external or unprocessed
    files may carry none) to ``file``.
    """
    if not mimetype:
        return InboundMediaKind.FILE
    major = mimetype.split("/", 1)[0].strip().lower()
    if major == "image":
        return InboundMediaKind.IMAGE
    if major == "video":
        return InboundMediaKind.VIDEO
    if major == "audio":
        return InboundMediaKind.AUDIO
    if major in ("application", "text"):
        return InboundMediaKind.DOCUMENT
    return InboundMediaKind.FILE


async def _bridge_files(event: dict, our_identity: str | None, event_id: str) -> Response:
    """Bridge a ``file_share`` message — one turn per file, none dropped.

    Slack allows several files in one message and the ``media_*`` vocabulary is
    single-media, so each file bridges its own turn under ``f"{event_id}-{index}"`` (so
    distinct files of one message dedupe distinctly at intake). The message ``text``
    caption rides the first bridged turn when non-blank; every other turn carries the
    generic placeholder so it is never empty. A file exposing neither a fetchable
    ``url_private`` nor a ``name`` cannot be represented — it gets the shared rejection
    reply + event, never a silent drop — while its siblings still bridge. That rejection
    is claimed under the file's own ``f"{event_id}-{index}"`` key (the key its bridged
    siblings dedupe under) before the reply is sent and released if the send fails, so a
    retry driven by a later file's transient fault never repeats a rejection already
    delivered. When no file bridges (every one was unfetchable) but the message carried a
    caption, the caption bridges as a plain text turn under the message's own
    ``event_id``.
    """
    channel = event.get("channel")
    if not isinstance(channel, str) or not channel:
        raise ValueError(f"slack file_share event {event_id} carries no channel")
    files = event.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError(f"slack file_share event {event_id} carries no files")

    text = event.get("text")
    caption = text if isinstance(text, str) and text.strip() else None
    caption_pending = caption is not None
    bridged = False

    for index, file in enumerate(files):
        if not isinstance(file, dict):
            raise ValueError(f"slack file_share event {event_id} file {index} is not an object")  # noqa: TRY004 raised type is intentional (invariant/state/validation taxonomy); TypeError would change behaviour
        url_private = file.get("url_private")
        name = file.get("name")
        media_id = url_private if isinstance(url_private, str) and url_private else None
        filename = name if isinstance(name, str) and name else None
        if media_id is None and filename is None:
            item_key = f"{event_id}-{index}"
            if not await claim_dedupe(item_key):
                # A reprocess whose prior pass already delivered this file's rejection
                # reply (a later file's transient fault made the door re-raise): skip so
                # the participant is not notified a second time.
                continue
            try:
                await tai42_app.conversations.notify_inbound_rejected(
                    channel_id="slack",
                    recipient=channel,
                    sender_identity=our_identity,
                    kind="file",
                    reason=InboundRejectionReason.UNSUPPORTED_TYPE,
                )
            except BaseException:
                # The reply never reached the participant: free the claim so a retry
                # sends it, then re-raise into the door's release-and-500 guard.
                await release_dedupe(item_key)
                raise
            continue

        mimetype = file.get("mimetype")
        mime = mimetype if isinstance(mimetype, str) and mimetype else None
        kind = _media_kind(mime)
        size = file.get("size")
        turn_text = caption if caption_pending else inbound_media_placeholder(kind, filename=filename)
        caption_pending = False
        await _bridge(
            our_identity,
            channel,
            turn_text,
            event_id,
            params=build_inbound_media_params(
                kind=kind,
                media_id=media_id,
                mime_type=mime,
                filename=filename,
                size=size if isinstance(size, int) else None,
            ),
            provider_message_id=f"{event_id}-{index}",
        )
        bridged = True

    if caption is not None and not bridged:
        # Every file was unfetchable (each got its own rejection reply), but the
        # message carried a caption: bridge it as a plain text turn under the
        # message's own event_id so participant text is never silently dropped.
        await _bridge(our_identity, channel, caption, event_id)
        bridged = True

    return JSONResponse({"status": "accepted" if bridged else "ignored"})
