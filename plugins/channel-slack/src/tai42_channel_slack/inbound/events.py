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
from tai42_contract.interactions import (
    IngestedMedia,
    MediaOrigin,
    MediaSourceReadError,
    MediaStoreUnavailableError,
    MediaTooLargeError,
    MediaTypeNotAllowedError,
)
from tai42_kit.net import MediaFetchError, UrlGuardError, open_media_stream
from tai42_kit.settings import require_secret

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


def _file_download_url(file: dict) -> str | None:
    """The URL the file's bytes are fetched from, or ``None`` when the file exposes none.

    ``url_private_download`` (Slack's download-disposition variant) is preferred over
    ``url_private``; both need the bot ``Authorization`` header.
    """
    for key in ("url_private_download", "url_private"):
        value = file.get(key)
        if isinstance(value, str) and value:
            return value
    return None


async def _reject_file(
    channel: str,
    our_identity: str | None,
    event_id: str,
    index: int,
    kind: str,
    reason: InboundRejectionReason,
) -> None:
    """Send one file's permanent-rejection notice, claimed under its own dedupe key.

    The notice is claimed under the file's ``f"{event_id}-{index}"`` key — the key its
    bridged siblings dedupe under — before the reply is sent, and the claim is KEPT once
    the reply lands so a retry driven by a later file's transient fault never repeats a
    rejection already delivered. A send fault frees the claim and re-raises into the door's
    release-and-500 guard so the retry re-sends. An already-claimed key means a prior pass
    delivered this rejection; it is skipped, not sent twice.
    """
    item_key = f"{event_id}-{index}"
    if not await claim_dedupe(item_key):
        return
    try:
        await tai42_app.conversations.notify_inbound_rejected(
            channel_id="slack",
            recipient=channel,
            sender_identity=our_identity,
            kind=kind,
            reason=reason,
        )
    except BaseException:
        await release_dedupe(item_key)
        raise


def _nonempty_str(value: object) -> str | None:
    """``value`` when it is a non-empty string, else ``None``."""
    return value if isinstance(value, str) and value else None


async def _fetch_and_ingest_file(
    url: str,
    bot_token: str,
    *,
    kind: InboundMediaKind,
    declared_mime: str | None,
    filename: str | None,
    declared_size: int | None,
    origin: MediaOrigin,
) -> IngestedMedia | InboundRejectionReason:
    """Fetch one file's bytes and ingest them into served media, or map a permanent failure.

    Returns the served :class:`IngestedMedia` on success, or the
    :class:`InboundRejectionReason` for a permanent failure — an unfetchable body (a 4xx other
    than 408/429, a disabled 3xx, an SSRF-blocked target), over-cap, a disallowed type, or no blob
    store. A transient fault — a 5xx, a 408 or a 429, a timeout at open, a torn body mid-stream —
    RE-RAISES so the door frees its claim and Slack redelivers. The bot token rides the request only.
    """
    try:
        async with open_media_stream(
            url, headers={"Authorization": f"Bearer {bot_token}"}, follow_redirects=False
        ) as stream:
            return await tai42_app.media.ingest_media(
                source=stream.chunks,
                kind_hint=kind,
                declared_mime=declared_mime or stream.content_type,
                filename=filename,
                declared_size=declared_size or stream.content_length,
                integrity_sha256=None,
                origin=origin,
            )
    except MediaFetchError as exc:
        if exc.transient:
            raise
        return InboundRejectionReason.COULD_NOT_RECEIVE
    except UrlGuardError:
        return InboundRejectionReason.COULD_NOT_RECEIVE
    except MediaSourceReadError:
        raise
    except MediaTooLargeError:
        return InboundRejectionReason.TOO_LARGE
    except MediaTypeNotAllowedError:
        return InboundRejectionReason.UNSUPPORTED_TYPE
    except MediaStoreUnavailableError:
        return InboundRejectionReason.COULD_NOT_RECEIVE


async def _bridge_one_file(
    file: object,
    index: int,
    *,
    channel: str,
    our_identity: str | None,
    event_id: str,
    user: str,
    bot_token: str,
    caption: str | None,
) -> bool:
    """Fetch, ingest and bridge ONE file, or send its permanent-rejection notice.

    ``caption`` rides this file's turn when set (the caller offers it only to the first
    bridged file); otherwise the turn carries the generic placeholder so it is never empty.
    The bridged turn carries the typed served :class:`MediaItem` as ``attachments`` AND the
    parity ``media_*`` params off that ONE ingest, with the SERVED id, the seam's computed
    sha256, and the seam's SANITISED filename (the raw vendor ``name`` never enters a param
    value or the placeholder label). Returns ``True`` iff a media turn was bridged.
    """
    if not isinstance(file, dict):
        raise ValueError(f"slack file_share event {event_id} file {index} is not an object")  # noqa: TRY004 raised type is intentional (invariant/state/validation taxonomy); TypeError would change behaviour
    filename = _nonempty_str(file.get("name"))
    mime = _nonempty_str(file.get("mimetype"))
    kind = _media_kind(mime)
    url = _file_download_url(file)
    if url is None:
        # Nothing to fetch: a file with neither a fetchable body nor a name cannot be
        # represented at all (UNSUPPORTED_TYPE); a named file whose body is missing is a
        # receive failure (COULD_NOT_RECEIVE). Either way a per-file notice, never a drop.
        reason = (
            InboundRejectionReason.UNSUPPORTED_TYPE if filename is None else InboundRejectionReason.COULD_NOT_RECEIVE
        )
        await _reject_file(channel, our_identity, event_id, index, kind.value, reason)
        return False

    size = file.get("size")
    origin = MediaOrigin(channel_id="slack", participant_identity=user, message_id=f"{event_id}-{index}")
    outcome = await _fetch_and_ingest_file(
        url,
        bot_token,
        kind=kind,
        declared_mime=mime,
        filename=filename,
        declared_size=size if isinstance(size, int) else None,
        origin=origin,
    )
    if isinstance(outcome, InboundRejectionReason):
        await _reject_file(channel, our_identity, event_id, index, kind.value, outcome)
        return False

    turn_text = caption if caption is not None else inbound_media_placeholder(kind, filename=outcome.item.filename)
    await _bridge(
        our_identity,
        channel,
        turn_text,
        event_id,
        attachments=[outcome.item],
        params=build_inbound_media_params(
            kind=kind,
            media_id=outcome.media_id,
            mime_type=outcome.mime,
            sha256=outcome.sha256,
            filename=outcome.item.filename,
            size=outcome.size,
        ),
        provider_message_id=f"{event_id}-{index}",
    )
    return True


async def _bridge_files(event: dict, our_identity: str | None, event_id: str) -> Response:
    """Bridge a ``file_share`` message — one served attachment per file, none dropped.

    Each file's bytes are fetched from Slack and ingested into the platform's served media;
    each bridges its own turn under ``f"{event_id}-{index}"`` (distinct files dedupe
    distinctly at intake) — Slack allows several files in one message and the ``media_*``
    vocabulary is single-media. The message ``text`` caption rides the first bridged turn
    when non-blank; every other turn carries the generic placeholder so it is never empty. A
    permanent per-file failure sends the mapped rejection notice + event and acks, never a
    silent drop, while its siblings still bridge; a transient fetch/read fault re-raises so
    the door frees the ``event_id`` claim and Slack redelivers. When no file bridges but the
    message carried a caption, the caption bridges as a plain text turn under the message's
    own ``event_id``.
    """
    channel = event.get("channel")
    if not isinstance(channel, str) or not channel:
        raise ValueError(f"slack file_share event {event_id} carries no channel")
    user = event.get("user")
    if not isinstance(user, str) or not user:
        raise ValueError(f"slack file_share event {event_id} carries no user")
    files = event.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError(f"slack file_share event {event_id} carries no files")

    bot_token = require_secret(slack_settings().bot_token, "the slack channel", "CHANNEL_SLACK_BOT_TOKEN")

    text = event.get("text")
    caption = text if isinstance(text, str) and text.strip() else None
    bridged = False

    for index, file in enumerate(files):
        offered_caption = caption if not bridged else None
        if await _bridge_one_file(
            file,
            index,
            channel=channel,
            our_identity=our_identity,
            event_id=event_id,
            user=user,
            bot_token=bot_token,
            caption=offered_caption,
        ):
            bridged = True

    if caption is not None and not bridged:
        # Every file was rejected (each got its own notice), but the message carried a
        # caption: bridge it as a text turn under the message's own event_id so participant
        # text is never silently dropped. A multi-file share has no single kind; the first
        # file (the first rejected one, since none bridged) supplies the kind + declared mime
        # on the caption turn — the kind/mime parity params the other channels give a
        # rejected-media caption (no served reference, as the media does not exist).
        first_mime = _nonempty_str(files[0].get("mimetype"))
        await _bridge(
            our_identity,
            channel,
            caption,
            event_id,
            params=build_inbound_media_params(kind=_media_kind(first_mime), mime_type=first_mime),
        )
        bridged = True

    return JSONResponse({"status": "accepted" if bridged else "ignored"})
