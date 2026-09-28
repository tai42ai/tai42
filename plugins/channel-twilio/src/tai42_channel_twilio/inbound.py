"""Inbound Twilio webhook — where the human's reply enters the system.

``POST /api/channels/twilio/inbound`` is unauthenticated (Twilio cannot send the
platform api key); auth is the ``X-Twilio-Signature`` HMAC, validated fail-closed
before any byte of the body is trusted. Two correctness rules:

* The signed URL must be the EXACT public URL Twilio called, reconstructed from
  ``X-Forwarded-Proto``/``X-Forwarded-Host`` behind a TLS-terminating proxy. A
  spoofed header only changes the URL the HMAC is computed over, so it can only
  make validation FAIL — forging a PASS still needs the auth token.
* The signed params must be the RAW form pairs including duplicates
  (``parse_qsl`` over the raw body); collapsing to a dict drops duplicate keys
  and breaks the signature.

Twilio's scheme carries no timestamp, so the ``MessageSid`` dedupe is the replay
guard. A reply matching a pending question is forwarded as ``{"answer": <Body
verbatim, outer whitespace stripped>}``; on a correlation miss the message enters
the conversation bridge instead. The signed status door reuses the same auth.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from urllib.parse import parse_qsl

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from tai42_contract.app import tai42_app
from tai42_contract.channels import InboundAnswerOutcome, InboundBridge
from tai42_contract.conversations import (
    BlankInboundTextError,
    DeliveryReceipt,
    InboundMediaKind,
    InboundRejectionReason,
    build_inbound_media_params,
    inbound_media_placeholder,
)
from tai42_contract.interactions import (
    MediaOrigin,
    MediaSourceReadError,
    MediaStoreUnavailableError,
    MediaTooLargeError,
    MediaTypeNotAllowedError,
)
from tai42_kit.net import MediaFetchError, UrlGuardError, open_media_stream
from tai42_kit.net.request_body import RequestBodyTooLargeError, read_bounded_body
from tai42_kit.settings import require, require_secret

from tai42_channel_twilio.correlation import (
    already_seen,
    correlation_key,
    mark_seen,
    twilio_correlation_store,
)
from tai42_channel_twilio.settings import twilio_settings

logger = logging.getLogger(__name__)

_SIGNATURE_HEADER = "X-Twilio-Signature"
# Twilio's mandated request-signature digest is HMAC-SHA1; 20 bytes, 28 base64 chars.
_SHA1_DIGEST_LEN = hashlib.sha1().digest_size  # noqa: S324
# Bound what an unauthenticated door reads into memory — loud 413, never truncation.
_MAX_BODY_BYTES = 1 * 1024 * 1024

# Twilio MessageStatus → the terminal receipt to record; anything else (queued,
# sent, sending, ...) is a non-terminal no-op.
_DELIVERY_RECEIPTS = {
    "failed": DeliveryReceipt.FAILED,
    "undelivered": DeliveryReceipt.FAILED,
    "delivered": DeliveryReceipt.DELIVERED,
}


class SignatureRejectedError(Exception):
    """The request failed Twilio signature authentication (mapped to 401)."""


def _reconstruct_public_url(request: Request) -> str:
    """The exact public URL Twilio called, as seen from outside the proxy.

    Scheme/host from ``X-Forwarded-Proto``/``X-Forwarded-Host`` when the trusted
    proxy set them (first value of a list), else the request's own; path and raw
    query from the request unchanged.
    """
    proto_header = request.headers.get("x-forwarded-proto")
    proto = proto_header.split(",")[0].strip() if proto_header else request.url.scheme
    host_header = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if not host_header:
        raise SignatureRejectedError("request carries no Host header — cannot reconstruct the signed URL")
    host = host_header.split(",")[0].strip()
    url = f"{proto}://{host}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    return url


def _validate_signature(
    auth_token: str, public_url: str, form_pairs: list[tuple[str, str]], provided: str | None
) -> None:
    """Validate ``X-Twilio-Signature`` or raise ``SignatureRejectedError``.

    Twilio's algorithm: append each POST param's name+value (params sorted by
    name, no delimiters) to the full URL, HMAC-SHA1 with the auth token, base64.
    Compared constant-time over the decoded digest bytes.
    """
    if provided is None:
        raise SignatureRejectedError(f"missing {_SIGNATURE_HEADER} header")
    payload = public_url + "".join(name + value for name, value in sorted(form_pairs))
    expected = hmac.new(auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).digest()
    try:
        provided_digest = base64.b64decode(provided, validate=True)
    except ValueError as exc:
        raise SignatureRejectedError(f"{_SIGNATURE_HEADER} is not valid base64") from exc
    if len(provided_digest) != _SHA1_DIGEST_LEN:
        raise SignatureRejectedError(f"{_SIGNATURE_HEADER} is not a SHA-1 digest")
    if not hmac.compare_digest(provided_digest, expected):
        raise SignatureRejectedError(f"{_SIGNATURE_HEADER} mismatch")


async def _authenticated_form_pairs(request: Request) -> list[tuple[str, str]]:
    """Bounded-read and Twilio-signature-validate the request; return the RAW form pairs (duplicates kept).

    Nothing in the body is trusted until the signature validates. Raises ``ValueError`` (auth token unset →
    logged 500), ``RequestBodyTooLargeError`` (→ 413), or ``SignatureRejectedError`` (→ 401).
    """
    auth_token = require_secret(twilio_settings().auth_token, "Twilio channel", "CHANNEL_TWILIO_AUTH_TOKEN")
    raw = await read_bounded_body(request, _MAX_BODY_BYTES)
    try:
        body_text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        # Twilio signs UTF-8 bodies; undecodable bytes can't carry a valid signature.
        raise SignatureRejectedError("body is not valid UTF-8") from exc
    form_pairs = parse_qsl(body_text, keep_blank_values=True)
    public_url = _reconstruct_public_url(request)
    _validate_signature(auth_token, public_url, form_pairs, request.headers.get(_SIGNATURE_HEADER))
    return form_pairs


def _auth_error_response(exc: ValueError | RequestBodyTooLargeError | SignatureRejectedError, route: str) -> Response:
    """Map an ``_authenticated_form_pairs`` failure to its response.

    413 oversize, 401 bad signature, 500 for an unset auth token (operator misconfig, never a
    401 that reads like an ordinary bad signature).
    """
    if isinstance(exc, RequestBodyTooLargeError):
        return PlainTextResponse("payload too large", status_code=413)
    if isinstance(exc, SignatureRejectedError):
        logger.warning("rejected Twilio %s: %s", route, exc)
        return PlainTextResponse("signature verification failed", status_code=401)
    logger.error("twilio %s: CHANNEL_TWILIO_AUTH_TOKEN is unset or empty; failing closed", route)
    return JSONResponse({"error": "channel misconfigured"}, status_code=500)


@tai42_app.http.custom_route(
    "/inbound",
    methods=["POST"],
    summary="Twilio inbound message webhook",
    tags=["channels"],
    response_model=None,
    no_body_reason="Twilio inbound webhook: 204/plain-text, no JSON body",
)
async def twilio_inbound(request: Request) -> Response:
    """Receive a Twilio inbound message and resolve the pair's pending question, or route it to the bridge.

    Order is load-bearing: bounded body read (413) → signature (nothing trusted
    before it) → MessageSid dedupe → the shared inbound-answer ladder. A correlated
    reply is resolved by the ladder (forward / retry-in-place / bridge over the
    plugin's :class:`CorrelationStore`); a correlation MISS (``NO_CORRELATION``)
    goes to this channel's bridge. Every resolved/kept/bridged
    outcome acks 204 and marks the ``MessageSid`` seen; an ``AnswerForwardError``
    (401/413/5xx / transport fault) propagates so Twilio's retry re-runs the ladder —
    the answer is never silently lost, and the sid is NOT marked seen on that raise.
    """
    try:
        form_pairs = await _authenticated_form_pairs(request)
    except (ValueError, RequestBodyTooLargeError, SignatureRejectedError) as exc:
        return _auth_error_response(exc, "inbound")

    # Collapse to single values only now, after the signature validated the raw pairs.
    form = dict(form_pairs)
    message_sid = form.get("MessageSid")
    if not message_sid:
        return PlainTextResponse("missing MessageSid", status_code=400)

    if await already_seen(message_sid):
        return Response(status_code=204)

    # Inbound direction: To = the deployment's Twilio number, From = the human.
    twilio_number = form.get("To", "")
    human_number = form.get("From", "")
    result = await tai42_app.channels.handle_inbound_answer(
        channel_id="twilio",
        correlation_key=correlation_key(twilio_number, human_number),
        # A typed SMS answers with its Body minus outer whitespace.
        answer=form.get("Body", "").strip(),
        store=twilio_correlation_store,
        bridge=InboundBridge(
            channel_id="twilio",
            our_identity=twilio_number,
            client_address=human_number,
            # The provider attests the From number, so it is both the conversation
            # identity and the party the turn cap holds accountable.
            cap_key=human_number,
            provider_message_id=message_sid,
            # A bridged (gone-ask / hard-mismatch) reply carries the Body verbatim.
            bridge_text=form.get("Body", ""),
        ),
    )
    if result.outcome is InboundAnswerOutcome.NO_CORRELATION:
        # No pending question (unrelated text or expired) — route to the bridge.
        return await _bridge_inbound(form, message_sid)
    # FORWARDED / RETRY_KEPT / BRIDGED: the ladder resolved (or already bridged) the
    # reply; ack 204 and mark the sid seen so a redelivery is not re-processed.
    await mark_seen(message_sid)
    return Response(status_code=204)


def _media_count(form: dict[str, str]) -> int:
    """The number of MMS media items the inbound carries (``NumMedia``; absent → 0).

    A present-but-non-integer value is a Twilio protocol violation and raises loudly
    (propagated as a 5xx so the malformed delivery is never silently treated as media-less).
    """
    raw = form.get("NumMedia")
    if raw is None:
        return 0
    return int(raw)


def _media_kind(content_type: str) -> InboundMediaKind:
    """Map a Twilio ``MediaContentType`` onto the generic inbound-media wire kind.

    By the mime major type: ``image``/``audio``/``video`` map to themselves,
    ``application``/``text`` to ``document``; any other or missing major type to ``file``.
    """
    major = content_type.split("/", 1)[0].strip().lower()
    if major == "image":
        return InboundMediaKind.IMAGE
    if major == "audio":
        return InboundMediaKind.AUDIO
    if major == "video":
        return InboundMediaKind.VIDEO
    if major in ("application", "text"):
        return InboundMediaKind.DOCUMENT
    return InboundMediaKind.FILE


async def _bridge_inbound(form: dict[str, str], message_sid: str) -> Response:
    """Route an uncorrelated inbound message into the conversation bridge.

    ``our_identity`` = To, ``client_address`` = From (verbatim). A text-only message
    bridges one turn under the bare ``MessageSid``; an MMS fetches and ingests each
    media item, bridging ONE served turn per item (``provider_message_id=f"{sid}-{N}"``)
    with the typed attachment and the parity ``media_*`` params — the ``Body`` caption
    rides the first item, a caption-less item a sanitised ``[kind]`` placeholder so the
    turn is never blank. A permanent media failure notifies the participant once and acks;
    a transient fetch/read fault propagates so Twilio redelivers. A message with no route
    bound, or with no body and no media, is logged and success-acked (the provider must
    not retry-storm a permanently-unrouted or empty message); a retryable overflow or
    infrastructure failure propagates as a 5xx so Twilio redelivers rather than silently
    dropping it.
    """
    try:
        await _accept_inbound(form, message_sid)
    except BlankInboundTextError as exc:
        logger.warning("blank Twilio inbound %s dropped: %s", message_sid, exc)
    except LookupError as exc:
        logger.warning("unrouted Twilio inbound %s dropped: %s", message_sid, exc)
    await mark_seen(message_sid)
    return Response(status_code=204)


async def _accept_inbound(form: dict[str, str], message_sid: str) -> None:
    """Accept the inbound as one text turn, or one served turn per MMS media item.

    The provider attests the From number, so it is both the conversation identity and
    the party the turn cap holds accountable. Each MMS media item is fetched from Twilio
    and ingested into the platform's served store; a transient fetch/read fault RAISES so
    Twilio redelivers (the ``MessageSid`` stays unmarked), a permanent one notifies the
    participant once and acks.
    """
    our_identity = form.get("To", "")
    client_address = form.get("From", "")
    media_count = _media_count(form)
    if media_count <= 0:
        await tai42_app.conversations.accept(
            channel="twilio",
            our_identity=our_identity,
            client_address=client_address,
            cap_key=client_address,
            text=form.get("Body", ""),
            provider_message_id=message_sid,
        )
        return

    settings = twilio_settings()
    account_sid = require(settings.account_sid, "Twilio channel", "CHANNEL_TWILIO_ACCOUNT_SID")
    auth_token = require_secret(settings.auth_token, "Twilio channel", "CHANNEL_TWILIO_AUTH_TOKEN")
    body = form.get("Body", "")
    has_caption = bool(body.strip())
    for index in range(media_count):
        caption = body if index == 0 and has_caption else None
        await _accept_media_item(
            form=form,
            message_sid=message_sid,
            index=index,
            our_identity=our_identity,
            client_address=client_address,
            account_sid=account_sid,
            auth_token=auth_token,
            caption=caption,
        )


# The kit fetch faults and the contract ingest faults one inbound media fetch can raise; every
# other exception propagates (a bare or unexpected ingest failure must surface loudly).
_MEDIA_FAILURES = (
    MediaFetchError,
    UrlGuardError,
    MediaTooLargeError,
    MediaTypeNotAllowedError,
    MediaStoreUnavailableError,
    MediaSourceReadError,
)


def _media_rejection_reason(exc: Exception) -> InboundRejectionReason | None:
    """The rejection reason for a PERMANENT media failure, or ``None`` when the failure is TRANSIENT.

    ``None`` means the caller must re-raise (redeliver): a transport fault, a 5xx, a 408 or a 429 at
    fetch (``MediaFetchError.transient``) and a torn body read (``MediaSourceReadError``). Every other
    caught failure is permanent: over-cap → ``TOO_LARGE``, disallowed type → ``UNSUPPORTED_TYPE``,
    a gone media / SSRF-blocked url / absent store → ``COULD_NOT_RECEIVE``.
    """
    if isinstance(exc, MediaFetchError):
        return None if exc.transient else InboundRejectionReason.COULD_NOT_RECEIVE
    if isinstance(exc, MediaSourceReadError):
        return None
    if isinstance(exc, MediaTooLargeError):
        return InboundRejectionReason.TOO_LARGE
    if isinstance(exc, MediaTypeNotAllowedError):
        return InboundRejectionReason.UNSUPPORTED_TYPE
    return InboundRejectionReason.COULD_NOT_RECEIVE


async def _accept_media_item(
    *,
    form: dict[str, str],
    message_sid: str,
    index: int,
    our_identity: str,
    client_address: str,
    account_sid: str,
    auth_token: str,
    caption: str | None,
) -> None:
    """Fetch one MMS media item from Twilio, ingest it, and bridge one served turn.

    ``MediaUrl{index}`` 307s to a foreign CDN; the fetch carries HTTP Basic auth on the
    first hop only — the kit drops it on the cross-origin redirect, and the vendor URL is
    pre-signed. A transient fetch/read fault propagates (Twilio redelivers, the
    ``MessageSid`` stays unmarked); a permanent one (a gone media, an over-cap or
    disallowed body, no store) notifies the participant once and, when a caption rides this
    item, still bridges the caption as a text turn so it is never lost.
    """
    media_url = form[f"MediaUrl{index}"]
    content_type = form[f"MediaContentType{index}"]
    kind = _media_kind(content_type)
    provider_message_id = f"{message_sid}-{index}"
    try:
        async with open_media_stream(media_url, auth=(account_sid, auth_token), follow_redirects=True) as stream:
            ingested = await tai42_app.media.ingest_media(
                source=stream.chunks,
                kind_hint=kind,
                declared_mime=content_type,
                filename=None,
                declared_size=stream.content_length,
                integrity_sha256=None,
                origin=MediaOrigin(
                    channel_id="twilio",
                    participant_identity=client_address,
                    message_id=provider_message_id,
                ),
            )
    except _MEDIA_FAILURES as exc:
        reason = _media_rejection_reason(exc)
        if reason is None:
            # Transient (a 5xx/timeout at fetch, a torn body read): propagate so Twilio
            # redelivers — the ``MessageSid`` stays unmarked, so the redelivery is re-processed
            # rather than deduped away.
            raise
        await _reject_media_item(
            our_identity=our_identity,
            client_address=client_address,
            kind=kind,
            reason=reason,
            caption=caption,
            declared_mime=content_type,
            message_sid=message_sid,
            index=index,
        )
        return

    text = caption if caption is not None else inbound_media_placeholder(kind, filename=ingested.item.filename)
    await tai42_app.conversations.accept(
        channel="twilio",
        our_identity=our_identity,
        client_address=client_address,
        cap_key=client_address,
        text=text,
        provider_message_id=provider_message_id,
        attachments=[ingested.item],
        params=build_inbound_media_params(
            kind=kind,
            media_id=ingested.media_id,
            mime_type=ingested.mime,
            sha256=ingested.sha256,
            filename=ingested.item.filename,
            size=ingested.size,
        ),
    )


async def _reject_media_item(
    *,
    our_identity: str,
    client_address: str,
    kind: InboundMediaKind,
    reason: InboundRejectionReason,
    caption: str | None,
    declared_mime: str,
    message_sid: str,
    index: int,
) -> None:
    """Notify the participant once that one media could not be received, then keep any caption.

    The notice is guarded by a per-item seen marker (``f"{message_sid}:media:{index}:rejected"`` —
    which cannot collide with a bare ``MessageSid``, as Twilio sids carry no colon): the notice
    fires and the marker is set ONLY after the send succeeds, so a redelivery driven by a SIBLING
    item's transient fault never repeats a rejection already delivered. A failed send leaves the
    marker unset and raises into the door's guard, so the retry re-sends. When the item carried the
    message ``Body`` as a caption, that text is still bridged as a turn (with the kind/mime parity
    params but no served reference — the media does not exist); that turn is idempotent under its
    ``provider_message_id``.
    """
    item_key = f"{message_sid}:media:{index}:rejected"
    if not await already_seen(item_key):
        await tai42_app.conversations.notify_inbound_rejected(
            channel_id="twilio",
            recipient=client_address,
            sender_identity=our_identity,
            kind=kind.value,
            reason=reason,
        )
        await mark_seen(item_key)
    if caption is not None:
        await tai42_app.conversations.accept(
            channel="twilio",
            our_identity=our_identity,
            client_address=client_address,
            cap_key=client_address,
            text=caption,
            provider_message_id=f"{message_sid}-{index}",
            params=build_inbound_media_params(kind=kind, mime_type=declared_mime),
        )


@tai42_app.http.custom_route(
    "/status",
    methods=["POST"],
    summary="Twilio delivery status webhook",
    tags=["channels"],
    response_model=None,
    no_body_reason="Twilio delivery-status webhook: 204/plain-text",
)
async def twilio_status(request: Request) -> Response:
    """Ingest a Twilio-signed delivery-status callback for a bridge outbound message.

    ``failed``/``undelivered`` → record FAILED; ``delivered`` → DELIVERED; every
    intermediate status (queued/sent/sending/...) is a benign no-op. A status for a
    ``MessageSid`` the bridge does not track is acked, never a 5xx — the provider
    must not retry a message we do not own.
    """
    try:
        form_pairs = await _authenticated_form_pairs(request)
    except (ValueError, RequestBodyTooLargeError, SignatureRejectedError) as exc:
        return _auth_error_response(exc, "status")

    form = dict(form_pairs)
    message_sid = form.get("MessageSid")
    if not message_sid:
        return PlainTextResponse("missing MessageSid", status_code=400)
    status = form.get("MessageStatus")
    if not status:
        return PlainTextResponse("missing MessageStatus", status_code=400)

    receipt = _DELIVERY_RECEIPTS.get(status)
    if receipt is None:
        return Response(status_code=204)

    try:
        await tai42_app.conversations.record_delivery_status("twilio", message_sid, receipt)
    except LookupError as exc:
        # Not a bridge outbound: it may be a ``notify_user`` send with no conversation
        # record. Post the receipt onto the originating trace via the send-outcome index;
        # only a genuine miss (neither the bridge nor such a send owns the id) keeps the
        # untracked-message log.
        if not await tai42_app.channels.record_send_receipt(
            "twilio", message_sid, receipt, errors=form.get("ErrorCode")
        ):
            logger.info("twilio status for untracked message %s ignored: %s", message_sid, exc)
    return Response(status_code=204)
