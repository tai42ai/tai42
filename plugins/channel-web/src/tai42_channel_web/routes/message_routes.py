"""The inbound-message doors: send one visitor message, and submit one ask-less form.
Both bridge a participant message into the conversation through the shared bridge."""

from __future__ import annotations

import hashlib
import logging
from typing import Any
from uuid import uuid4

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import Response
from tai42_contract.app import tai42_app
from tai42_contract.conversations import BlankInboundTextError, validate_entry_params
from tai42_contract.locale import InvalidLocaleError, normalize_optional_locale
from tai42_kit.utils.data.form_text import render_form_text

from tai42_channel_web.routes.dtos import (
    FormSubmissionBody,
    MessageAcceptedResponse,
    MessageBody,
    _object_refusal,
)
from tai42_channel_web.routes.envelope import _body_refusal, _error, _json_body, _ok
from tai42_channel_web.routes.session_access import (
    _CROSS_ORIGIN,
    _CROSS_ORIGIN_CODE,
    _SESSION_MISSING,
    _SESSION_MISSING_CODE,
    _client_bucket,
    _serves,
    _session,
    _store_off,
)
from tai42_channel_web.session import is_cross_origin
from tai42_channel_web.settings import web_settings
from tai42_channel_web.store.forms import read_form_record
from tai42_channel_web.store.registrations import SessionRegistration
from tai42_channel_web.store.transcript import append_message, transcript_order

logger = logging.getLogger(__name__)

# The store's per-thread FIFO overflow is a skeleton-internal type a channel plugin
# cannot import (it sits below the skeleton); it is matched by class name to answer a
# retriable 503 rather than an opaque 500. The channel accept path silently sheds
# rate limits (no exception), so there is no 429 to map here.
_THREAD_QUEUE_OVERFLOW = "ThreadQueueOverflowError"

# ONE wording for a form token that is unknown, expired, or another conversation's:
# no response may differ by which, or the refusal becomes a token/ownership oracle.
_FORM_NOT_FOUND = "form not found (unknown, expired, or already gone)"


def _inbound_locale(request: Request) -> str | None:
    """The visitor's BCP 47 locale from the request's ``Accept-Language`` header — the
    browser's top-ranked language range, canonicalized. A wildcard (``*``), an absent
    header or a malformed range is dropped to ``None`` (no locale hint), so the turn still
    runs; the platform never guesses a language from nothing."""
    header = request.headers.get("accept-language", "")
    top = header.split(",", 1)[0].split(";", 1)[0].strip()
    if not top or top == "*":
        return None
    try:
        return normalize_optional_locale(top)
    except InvalidLocaleError:
        logger.warning("web inbound: dropping malformed Accept-Language %r", top)
        return None


def _provider_message_id(identity: str, address: str, client_message_id: str | None) -> str:
    """The bridge's dedup id for one message POST.

    Without a retry key every POST mints a fresh id, so every POST is its own turn.
    With one, the id is derived from the key AND the whole conversation the message
    goes into — ``(identity, address)``, which is what a conversation is keyed by
    everywhere else. A re-POST of a reply the browser never saw resolves to the turn
    the first attempt already started; a message the same visitor sends to a
    DIFFERENT web route is a different conversation and must never be deduped against
    it (the bridge dedups identity-blind, so it would answer the first route's turn id
    and the text would land on the first route's transcript). No visitor can reach
    into another's dedup space either: neither half is the caller's to choose — the
    address comes from their registration, and both halves are ``:``-free, so the
    join is unambiguous."""
    if client_message_id is None:
        return uuid4().hex
    return hashlib.sha256(f"{identity}:{address}:{client_message_id}".encode()).hexdigest()


def _turn_params(registration: SessionRegistration, reply_id: str | None) -> dict[str, str] | None:
    """The turn's opaque enrichment params: the session's captured link params, plus a
    tapped reply option's ``id`` under ``reply_id`` when one rode this send. ``None`` when
    neither is present, so a plain typed message's payload stays byte-identical to before.

    The merged dict is re-bounded by the shared entry-param validator here (rather than only
    inside ``accept``, which would surface an over-count/over-size merge as an opaque 500):
    the caller maps a ``ValueError`` to a 422 the visitor's chip tap can be refused with."""
    params = dict(registration.params)
    if reply_id is not None:
        params["reply_id"] = reply_id
    if not params:
        return None
    return validate_entry_params(params)


async def _bridge_inbound_message(
    request: Request,
    identity: str,
    address: str,
    cap_key: str,
    text: str,
    client_message_id: str | None,
    params: dict[str, str] | None,
    blank_message: str,
    form: dict[str, Any] | None = None,
) -> Response:
    """Bridge one participant message into its web conversation and append the
    visitor's own frame under the accept-returned id — both held under the
    conversation's write-order gate.

    Shared by the messages door and the form-submission door. The inbound transcript
    entry is appended only once ``accept`` has RETURNED a turn id (a refused message
    never pollutes the transcript; a shed one has an id and is appended exactly as the
    bridge recorded it), and it reuses that ``message_id`` so a retry re-appends under
    the SAME id and the page's id-keyed replay still shows one message. The gate spans
    ``accept`` and the append so a shed message's slow-down reply cannot land ahead of
    the message that caused it."""
    async with transcript_order(identity, address):
        try:
            message_id = await tai42_app.conversations.accept(
                channel="web",
                our_identity=identity,
                client_address=address,
                cap_key=cap_key,
                text=text,
                provider_message_id=_provider_message_id(identity, address, client_message_id),
                params=params,
                locale=_inbound_locale(request),
                form=form,
            )
        except BlankInboundTextError:
            return _error(blank_message, 400)
        except LookupError as exc:
            return _error(str(exc), 404)
        except Exception as exc:
            if type(exc).__name__ == _THREAD_QUEUE_OVERFLOW:
                return _error(str(exc), 503)
            raise
        await append_message(identity, address, "in", text, entry_id=message_id, client_message_id=client_message_id)
    return _ok({"message_id": message_id})


@tai42_app.http.custom_route(
    "/messages",
    methods=["POST"],
    summary="Send a web chat message into the visitor's conversation",
    tags=["channels"],
    response_model=MessageAcceptedResponse,
)
async def web_messages(request: Request) -> Response:
    """Bridge one visitor message into their own web conversation.

    The conversation ``client_address`` is the registered visitor id — never taken
    from the body — and the body's ``identity`` must be the one the session was minted
    on: a session bought on one web route buys nothing on another, and the refusal is
    the one a caller with no session at all gets. The ``provider_message_id`` is
    minted fresh per POST unless the body carries a ``client_message_id``, in which
    case it is derived from that key and the caller's address so a retried POST
    resolves to the first attempt's turn.
    """
    if is_cross_origin(request):
        return _error(_CROSS_ORIGIN, 403, _CROSS_ORIGIN_CODE)
    off = _store_off()
    if off is not None:
        return off
    settings = web_settings()
    registration = await _session(request, settings)
    if registration is None:
        return _error(_SESSION_MISSING, 401, _SESSION_MISSING_CODE)

    raw, refusal = await _json_body(request, settings)
    if refusal is not None:
        return refusal
    try:
        body = MessageBody.model_validate(raw)
    except ValidationError as exc:
        return _error(_body_refusal(exc), 422)
    if not _serves(registration, body.identity):
        return _error(_SESSION_MISSING, 401, _SESSION_MISSING_CODE)
    address = registration.visitor_id
    # The captured link params plus a tapped reply option's id (params.reply_id) ride the
    # turn; an over-count/over-size merge is a clean 422 rather than an opaque accept 500.
    try:
        params = _turn_params(registration, body.reply_id)
    except ValueError as exc:
        return _error(str(exc), 422)
    # The conversation identity is the minted visitor id, which a visitor can rotate at
    # will — so it cannot bound spend. The turn cap keys instead on the accountable
    # NETWORK bucket, the same value the public-door rate limiter derives.
    cap_key = _client_bucket(request)
    return await _bridge_inbound_message(
        request, body.identity, address, cap_key, body.text, body.client_message_id, params, "message text is blank"
    )


@tai42_app.http.custom_route(
    "/forms/{token}",
    methods=["POST"],
    summary="Submit an ask-less web form into the visitor's conversation",
    tags=["channels"],
    response_model=MessageAcceptedResponse,
)
async def web_form_submit(request: Request) -> Response:
    """Bridge one ask-less form submission as a participant message.

    The token names a ``chat.form`` card's stored record. The record must exist AND
    its conversation — both the web route identity and the address — must be the
    caller's own session's: a foreign, expired, and never-minted token all answer
    the ONE uniform 404, so the refusal is never a token or ownership oracle. The
    record is READ, never claimed — a form is submittable again and again, each
    submission its own participant message (the option-chips precedent).

    The values pass the same transport bound the answer door's form branch applies
    (a JSON object of finite numbers, size-capped) and are NEVER validated against
    the stored schema: participant-shaped data the platform re-bounds (size/depth) at
    ``accept`` and no consumer may trust as schema-conformant. The text every
    consumer sees is rendered HERE from the STORED schema's labels — the client
    contributes values only.
    """
    if is_cross_origin(request):
        return _error(_CROSS_ORIGIN, 403, _CROSS_ORIGIN_CODE)
    off = _store_off()
    if off is not None:
        return off
    settings = web_settings()
    registration = await _session(request, settings)
    if registration is None:
        return _error(_SESSION_MISSING, 401, _SESSION_MISSING_CODE)
    token = request.path_params["token"]

    raw, refusal = await _json_body(request, settings)
    if refusal is not None:
        return refusal
    try:
        body = FormSubmissionBody.model_validate(raw)
    except ValidationError as exc:
        return _error(_body_refusal(exc), 422)
    bad_values = _object_refusal(body.values, "values object")
    if bad_values is not None:
        return _error(bad_values, 422)

    record = await read_form_record(token)
    if record is None or not _serves(registration, record.identity) or record.address != registration.visitor_id:
        return _error(_FORM_NOT_FOUND, 404)
    address = registration.visitor_id
    # The same accountable cap key as the messages door: the network bucket, never
    # the resettable visitor id.
    cap_key = _client_bucket(request)
    text = render_form_text(body.values, record.schema)
    return await _bridge_inbound_message(
        request,
        record.identity,
        address,
        cap_key,
        text,
        body.client_message_id,
        registration.params or None,
        "form values render to a blank message",
        form=body.values,
    )
