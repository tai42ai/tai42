"""The web-chat doors — under ``/api/channels/web``.

The chat doors are PUBLIC; the entry-gate management doors (bottom of this module)
are AUTHED — they carry the platform api key and declare an explicit action-class.

* ``GET /api/channels/web/chat/{identity}`` — the standalone chat page for one web
  route: the HTML shell around the built bundle. Mints AND registers the visitor's
  session when their cookie resolves to none for THIS route, and refreshes both the
  cookie's ``Max-Age`` and the registration on every load. The navigation's query
  string carries link params (captured with the session, delivered to the turn's tool
  payload) and, on a gated route, the ``tai_entry`` code that admits a new entry.
* ``GET /api/channels/web/assets/{file}`` — one file of that bundle, served only
  when the build manifest's integrity map lists it by exact name.
* ``POST /api/channels/web/messages`` — the visitor sends one message into their own
  conversation. The address comes from the session registration, never the body; the
  message is bridged through ``conversations.accept`` and, on success, appended to
  the transcript so the visitor's SSE stream replays it.
* ``GET /api/channels/web/stream`` — the SSE feed of the session's own conversation
  (backlog then live tail), keyed by ``(identity, visitor id)``.
* ``POST /api/channels/web/questions/{interaction_id}/answer`` — answer a pending
  question: the record must belong to the caller's own conversation, then the answer
  is forwarded to its interactions callback and a ``chat.answered`` frame appended.
* ``POST /api/channels/web/session/rotate`` — body ``{identity}``; mint a fresh
  session for that web route (the visitor's "new conversation"); the next message
  opens a conversation on the new address.

The chat doors are PUBLIC (``authed=False``): the transport credential is the
visitor's session cookie, and no chat door reads the platform api key or a Studio
session. A session is a capability on ONE web route: it is minted against the route
it was minted on, and a door presented with it on any other route refuses exactly as
it refuses an unknown token — the two are indistinguishable to a caller, so no session
can be probed for which route it belongs to. Success bodies are ``{"data": {...}}``;
failures are ``{"error": "<message>"}``, plus a ``code`` on the refusals a caller
must tell apart from their status alone — ``session_missing`` (401),
``origin_mismatch`` (403), ``not_a_navigation`` (403), ``entry_refused`` (403),
``web_transcript_store_off`` (501).

The chat page door is the exception: its caller is a browser NAVIGATION, which would
render a JSON body as text, so every refusal it answers is a minimal HTML page under
``REFUSAL_CSP`` instead — same status and, for the refusals that have one, the same
machine-readable code carried in a meta tag (``link_params_invalid`` for a params
bound violation, ``entry_refused`` for a gated route with no live code, uniform for
all five of missing/unknown/expired/revoked/throttled — no oracle). The unusable-
bundle 500 has no code: it is a server fault with no API counterpart, and nothing the
page could do differently for. Every page-door HTML response carries
``Referrer-Policy: no-referrer`` — a capability URL must never leak via referrer.

The entry-gate management doors are AUTHED (``authed=True``): they mint, list, revoke
codes and toggle the gate for a web route, keyed by the platform api key, and each
declares an explicit ``read``/``write`` action-class (an authed route with none
fails to register). Entry codes are hashed at rest, multi-use, optionally expiring;
the raw code is returned once at mint and never read back. A gate refuses uniformly.

Flood control on these doors is NOT this plugin's: the platform's public-door limiter
bounds requests per caller ahead of them, and the operator's ingress bounds what
reaches the platform. What the plugin bounds is the one resource it owns — the
dedicated store connection an open SSE stream pins (``stream.py``). The one client
address it reads is on the messages door: the visitor id keys the conversation, but a
visitor mints and rotates that id freely, so the accountable turn cap is keyed on the
request's network client bucket instead — the same value the public-door limiter
derives — never on the resettable visitor id.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from datetime import UTC, datetime
from stat import S_ISREG
from typing import Any
from uuid import uuid4

import httpx
from anyio.to_thread import run_sync
from pydantic import BaseModel, Field, ValidationError, field_validator
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from tai42_contract.app import tai42_app
from tai42_contract.conversations import BlankInboundTextError, validate_entry_params
from tai42_kit.clients.impl.http import HttpxClient
from tai42_kit.utils.client_address import XFF_HEADER, client_bucket

from tai42_channel_web.page import (
    HTML_CONTENT_TYPE,
    PAGE_CSP,
    REFUSAL_CSP,
    PublicBuildError,
    asset_content_type,
    asset_path,
    load_build,
    render_page,
    render_refusal,
)
from tai42_channel_web.session import (
    is_cross_origin,
    is_document_navigation,
    mint_session_token,
    mint_visitor_id,
    session_token,
    set_session_cookie,
)
from tai42_channel_web.settings import WebSettings, web_redis_settings, web_settings
from tai42_channel_web.store import (
    EntryCode,
    SessionRecordError,
    SessionRegistration,
    append_answered,
    append_message,
    check_entry_code,
    claim_question,
    drop_session,
    entry_attempt_allowed,
    is_gate_enabled,
    list_entry_codes,
    mint_entry_code,
    peek_question,
    register_session,
    resolve_session,
    restore_question,
    revoke_entry_code,
    set_gate,
    transcript_order,
    update_session_params,
)
from tai42_channel_web.stream import StreamLimitError, check_stream_admission, stream_transcript

logger = logging.getLogger(__name__)

# The store's per-thread FIFO overflow is a skeleton-internal type a channel plugin
# cannot import (it sits below the skeleton); it is matched by class name to answer a
# retriable 503 rather than an opaque 500. The channel accept path silently sheds
# rate limits (no exception), so there is no 429 to map here.
_THREAD_QUEUE_OVERFLOW = "ThreadQueueOverflowError"

# The page recognises this code and stops reconnecting: no store means no session
# registration, no transcript, and no stream — the whole channel is switched off.
_STORE_OFF = "web channel transcript store is not configured"
_STORE_OFF_CODE = "web_transcript_store_off"

# The page recognises this code and re-opens the chat URL to be minted a session.
# It answers BOTH "your cookie resolves to nothing" and "your session was minted on
# another web route": the wording covers each, and reloading the chat page is the
# cure for both.
_SESSION_MISSING = "no visitor session for this conversation; reload the chat page"
_SESSION_MISSING_CODE = "session_missing"

_CROSS_ORIGIN = "cross-origin request refused"
_CROSS_ORIGIN_CODE = "origin_mismatch"

# Minting a session is a state change, so only a top-level navigation may do it: a
# cross-site subresource pointed at the chat URL would otherwise overwrite a live
# visitor's cookie and strand their conversation.
_NOT_A_NAVIGATION_CODE = "not_a_navigation"

# A bundle file is content-hashed, so its bytes at a given name never change.
_HASHED_ASSET_CACHE = "public, max-age=31536000, immutable"
# Everything else these doors answer is about one caller at one moment: the page
# carries a fresh session cookie and links the current build's hashes, and a refusal
# describes this caller's session or origin. A cached copy would hand a later visitor
# any of it — and 404 and 501 are cacheable by default without this.
_NO_STORE = "no-store"

# Every response's declared type must be honored, never MIME-sniffed into something
# executable — the page's CSP script gate assumes it, and the asset door serves
# visitor-reachable bytes.
_NOSNIFF = {"x-content-type-options": "nosniff"}

_QUESTION_NOT_FOUND = "question not found (unknown, expired, or already answered)"
_ALREADY_ANSWERED = "that question was already answered"
# What a visitor is told when the callback door refused their answer without an error
# message of its own — never the raw body an intermediary may have written.
_CALLBACK_REFUSED = "the answer was refused"
# Appended once the refused question has been restored as often as it may be: the
# record is gone, so the visitor is told rather than left retrying into a 404.
_ANSWER_RETRIES_SPENT = "that question can no longer be answered"

# What an anonymous visitor is told when the bundle on disk is unusable. The path
# and the build step stay in the log — a public door names no server path.
_PAGE_UNAVAILABLE = "the chat page is unavailable"

# The page door's refusals, as the pages a browser renders instead of a raw body.
# Built once at import from these module constants, so each is byte-constant. The
# prose is the visitor's; the code meta and the log line carry the operator's detail,
# and neither page names anything about the server beyond its own refusal code.
_STORE_OFF_PAGE = render_refusal(
    "Chat is unavailable",
    "This chat is not switched on. Please contact the site owner.",
    _STORE_OFF_CODE,
)
_NOT_A_NAVIGATION_PAGE = render_refusal(
    "Open the chat in its own page",
    "This chat starts a session only when its page is opened directly. "
    "Open the chat link in your browser to start chatting.",
    _NOT_A_NAVIGATION_CODE,
)
_PAGE_UNAVAILABLE_PAGE = render_refusal(
    "Chat is unavailable",
    "The chat page could not be loaded. Please try again later.",
)

# A capability URL must never leak via the ``Referer`` header — set on EVERY
# page-door HTML response (the chat page and every refusal page).
_REFERRER_POLICY = {"referrer-policy": "no-referrer"}

# Query names the web door consumes itself and NEVER stores or delivers as params:
# ``pair`` (existing, client-consumed) and the entry-gate code ``tai_entry``. Link
# param VALUES (and the entry code) never appear in any log line, error body, or
# transcript frame — keys may be logged, values never.
_RESERVED_QUERY_PARAMS = frozenset({"pair", "tai_entry"})

# The link params carried a value that violates a bound (count, key shape, value
# length, or total size). One byte-constant page; the log names the bound.
_LINK_PARAMS_INVALID_CODE = "link_params_invalid"
_LINK_PARAMS_INVALID_PAGE = render_refusal(
    "This chat link is not valid",
    "This chat link is not valid. Please check the link and try again.",
    _LINK_PARAMS_INVALID_CODE,
)

# The route is gated and the navigation's code is missing, unknown, expired, revoked,
# or the client is throttled — ONE page and ONE wording for all five, so no response
# ever differs by code validity (no oracle). The wording constant is shared by the
# rotate door's JSON refusal.
_ENTRY_REFUSED_CODE = "entry_refused"
_ENTRY_REFUSED_MESSAGE = "This chat is private. Open it from the link you were given."
_ENTRY_REFUSED_PAGE = render_refusal("This chat is private", _ENTRY_REFUSED_MESSAGE, _ENTRY_REFUSED_CODE)

# A web route identity is a URL segment naming a configured route; longer than this
# is not one.
_MAX_IDENTITY_CHARS = 256
# The longest single visitor message the door accepts. Well under the body cap, so
# an over-long text is a precise 422 rather than an opaque 413.
_MAX_TEXT_CHARS = 8000
# The longest string answer the door forwards — it is persisted verbatim into the
# transcript frame the page replays.
_MAX_ANSWER_CHARS = 8000
# A web question is text / confirm / select, so its answer is one scalar. ``bool`` is
# listed for the reader; it is already an ``int`` subclass.
_ANSWER_TYPES = (str, bool, int, float)

# The retry key a page may put on a message POST: opaque to the server, and the only
# thing that makes a re-POST resolve to the turn the lost first attempt started.
_CLIENT_MESSAGE_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


_IDENTITY_REQUIREMENT = (
    f"identity must be a non-blank, ':'-free web route identity of at most {_MAX_IDENTITY_CHARS} characters"
)


def _clean_identity(value: str) -> str | None:
    """The bridge's canonical identity — trimmed, because the bridge trims and an
    untrimmed one here would key a transcript nothing ever writes to. ``None`` when
    it is blank, over-long, or carries the ``:`` that separates a composite recipient
    and qualifies the transcript key — an absent one is the caller's to tell apart."""
    identity = value.strip()
    if not identity or len(identity) > _MAX_IDENTITY_CHARS or ":" in identity:
        return None
    return identity


class PayloadTooLargeError(Exception):
    """The visitor's request body exceeded the door's cap (mapped to 413)."""


class IdentityBody(BaseModel):
    """The web route a request names. Every door that takes one canonicalises it the
    same way, so the value compared against the caller's session registration is the
    bridge's own form and not the caller's spacing."""

    identity: str

    @field_validator("identity")
    @classmethod
    def _canonical_identity(cls, value: str) -> str:
        identity = _clean_identity(value)
        if identity is None:
            raise ValueError(_IDENTITY_REQUIREMENT)
        return identity


class RotateBody(IdentityBody):
    """The web route the fresh session is minted for — a session is bound to one.

    ``entry_code`` carries the URL's ``tai_entry`` value: a gated identity refuses a
    rotation with a missing/dead code exactly as the page door refuses an entry; an
    ungated identity ignores the field."""

    entry_code: str | None = None


# The longest operator label a minted code may carry — bounded so an authed door
# cannot store an unbounded string.
_MAX_LABEL_CHARS = 256


class GateToggleBody(BaseModel):
    """The management PUT body: whether the route is gated."""

    enabled: bool


class MintCodeBody(BaseModel):
    """The management mint body: an optional operator label and an optional expiry.
    ``expires_at`` must be timezone-aware and in the future (the code's Redis TTL is
    derived from it)."""

    label: str | None = Field(default=None, max_length=_MAX_LABEL_CHARS)
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def _future_and_tz_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expires_at must be timezone-aware")
        if value <= datetime.now(UTC):
            raise ValueError("expires_at must be in the future")
        return value


class MessageBody(IdentityBody):
    text: str = Field(max_length=_MAX_TEXT_CHARS)
    # Optional retry key. A POST whose reply never reached the browser is re-sent
    # with the SAME key, and the door then derives the bridge's dedup id from it, so
    # the retry resolves to the first attempt's turn instead of delivering twice.
    client_message_id: str | None = None

    @field_validator("client_message_id")
    @classmethod
    def _opaque_retry_key(cls, value: str | None) -> str | None:
        if value is not None and _CLIENT_MESSAGE_ID.match(value) is None:
            raise ValueError("client_message_id must be 8 to 64 characters of [A-Za-z0-9_-]")
        return value


class AnswerForwardError(Exception):
    """The interactions callback door did not accept the forwarded answer (mapped to 500)."""


def _error(message: str, status_code: int, code: str | None = None) -> JSONResponse:
    """A refusal as the API doors answer it. Never cached: a refusal is about this
    caller at this moment, and several of these statuses (404, 501) are heuristically
    cacheable — a cached one would answer a later visitor on a GET door."""
    payload: dict[str, str] = {"error": message}
    if code is not None:
        payload["code"] = code
    headers = {**_NOSNIFF, "cache-control": _NO_STORE}
    return JSONResponse(payload, status_code=status_code, headers=headers)


def _refusal_page(html: str, status_code: int) -> Response:
    """A refusal the PAGE door answers: the caller is a browser navigating to the chat
    URL, so it is served one of the byte-constant pages above rather than the API
    doors' JSON. Cached as little as the page itself — a refusal is about this caller
    at this moment."""
    headers = {"content-security-policy": REFUSAL_CSP, **_NOSNIFF, "cache-control": _NO_STORE, **_REFERRER_POLICY}
    return Response(html, status_code=status_code, media_type=HTML_CONTENT_TYPE, headers=headers)


def _ok(data: dict[str, Any]) -> JSONResponse:
    return JSONResponse({"data": data}, headers=_NOSNIFF)


def _store_configured() -> bool:
    """Whether a transcript store is configured. Without one a session cannot be
    registered, so every door but the asset door refuses with 501."""
    return bool(web_redis_settings().redis_url)


def _store_off() -> JSONResponse | None:
    """That 501 as the API doors answer it, or ``None`` when a store IS configured."""
    if _store_configured():
        return None
    return _error(_STORE_OFF, 501, _STORE_OFF_CODE)


async def _read_bounded_body(request: Request, cap: int) -> bytes:
    """Read the body counting ACTUAL bytes, never a client ``Content-Length``.
    Raises ``PayloadTooLargeError`` past ``cap`` — the read stops there, and nothing
    is ever truncated into a shorter valid body."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap:
            raise PayloadTooLargeError(f"request body exceeds the {cap}-byte cap")
        chunks.append(chunk)
    return b"".join(chunks)


async def _json_body(request: Request, settings: WebSettings) -> tuple[Any, JSONResponse | None]:
    """The parsed JSON body, or ``(None, <refusal>)`` for an over-cap or unparseable
    one."""
    try:
        raw = await _read_bounded_body(request, settings.max_body_bytes)
    except PayloadTooLargeError as exc:
        logger.warning("web chat door refused an oversized body: %s", exc)
        return None, _error("request body is too large", 413)
    try:
        return json.loads(raw), None
    except ValueError:
        return None, _error("invalid JSON body", 400)


async def _session(request: Request, settings: WebSettings) -> SessionRegistration | None:
    """The caller's session: the registration their cookie token stands for — the
    conversation address to use, and the web route it was minted on. ``None`` when
    there is no cookie, its value is not a minted token, or nothing is registered for
    it — an invented or planted token is never adopted as a session."""
    token = session_token(request, settings)
    if token is None:
        return None
    try:
        return await resolve_session(token)
    except SessionRecordError:
        # A stored record in any other shape fails loud in the decoder; the DOOR
        # re-mints. The session read already refreshed the record's TTL, so without
        # this catch the raise escapes as a 500 and the dead record never ages out.
        # Value-free: nothing of the record (or the token) is logged.
        logger.warning("web chat door ignored a session: its stored record was refused")
        return None


def _serves(registration: SessionRegistration | None, identity: str) -> bool:
    """Whether this session may act on ``identity``. A session minted on another web
    route is refused exactly as a missing one is: a caller must not be able to tell a
    foreign session from no session, or the refusal itself would say which routes a
    stolen cookie is good for."""
    return registration is not None and registration.identity == identity


async def _mint_session(response: Response, identity: str, settings: WebSettings, params: dict[str, str]) -> None:
    """Register a fresh token/visitor-id pair for one web route, with the entry's link
    params, and set the cookie. The registration lands first: a cookie whose token
    resolves to nothing is not a session."""
    token = mint_session_token()
    await register_session(token, mint_visitor_id(), identity, params)
    set_session_cookie(response, token, settings)


def _client_bucket(request: Request) -> str:
    """The accountable network client bucket for a request — the same value the
    public-door rate limiter derives, keyed on the network peer, never a resettable
    visitor id. It is what the turn cap and the entry-gate throttle hold accountable."""
    return client_bucket(request.client.host if request.client else None, request.headers.get(XFF_HEADER, ""))


def _read_link_params(request: Request, identity: str) -> tuple[dict[str, str], Response | None]:
    """Parse the navigation's query into the validated link params, or a byte-constant
    400 refusal page. A DUPLICATE key — checked on the RAW query, reserved names
    included, so ``?pair=a&pair=b`` is a 400 too — is a bound violation; the reserved
    names are then stripped before validation. Param VALUES never reach the log: the
    duplicate warning names no value, and ``validate_entry_params`` names only the
    violated bound (and at most a key)."""
    pairs = request.query_params.multi_items()
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        logger.warning("web chat page refused link params for identity %s: a query parameter is repeated", identity)
        return {}, _refusal_page(_LINK_PARAMS_INVALID_PAGE, 400)
    candidate = {key: value for key, value in pairs if key not in _RESERVED_QUERY_PARAMS}
    try:
        return validate_entry_params(candidate), None
    except ValueError as exc:
        logger.warning("web chat page refused link params for identity %s: %s", identity, exc)
        return {}, _refusal_page(_LINK_PARAMS_INVALID_PAGE, 400)


async def _entry_gate_refusal(request: Request, identity: str, entry_code: str | None) -> Response | None:
    """The entry-gate check for a mint-needing caller on ``identity``, or ``None`` when
    the route is ungated or the code is live. Runs AFTER the navigation guard (README
    door order), so a non-navigation never reaches it and no response differs by code
    validity. Throttle is checked BEFORE the code lookup; missing/throttled/unknown all
    answer the ONE byte-constant refusal page. The presented value is never logged."""
    if not await is_gate_enabled(identity):
        return None
    bucket = _client_bucket(request)
    if entry_code is None:
        outcome = "missing"
    elif not await entry_attempt_allowed(bucket):
        outcome = "throttled"
    elif not await check_entry_code(identity, entry_code):
        outcome = "unknown"
    else:
        return None
    logger.warning("web chat page refused entry for identity %s: %s (bucket %s)", identity, outcome, bucket)
    return _refusal_page(_ENTRY_REFUSED_PAGE, 403)


def _door_error_detail(response: httpx.Response) -> str:
    """The callback door's OWN error message, or a fixed refusal when the reply does
    not carry one.

    Only a body in this platform's envelope is relayed. Anything else — a proxy or WAF
    page that answered instead of the door, a framework traceback, an upstream banner
    — is replaced and logged: it is written by an intermediary, names hosts and
    software the visitor is not entitled to, and this is an anonymous public door."""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("error"), str):
        return payload["error"][:500]
    logger.warning(
        "the interactions callback door answered HTTP %d with a body that is not this platform's error "
        "envelope; the visitor is told nothing of it: %s",
        response.status_code,
        response.text[:500],
    )
    return _CALLBACK_REFUSED


def _is_already_answered(response: httpx.Response) -> bool:
    """``True`` for the callback door's idempotent lost-claim reply,
    ``200 {"data": {"status": "already_answered"}}`` — the door's ONLY answer to a
    lost claim; it has no 409."""
    try:
        payload = response.json()
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    data = payload.get("data")
    return isinstance(data, dict) and data.get("status") == "already_answered"


def _answer_refusal(answer: Any) -> str | None:
    """The refusal for an answer value this door will not forward, or ``None``."""
    if not isinstance(answer, _ANSWER_TYPES):
        return "answer must be a string, number, or boolean"
    if isinstance(answer, float) and not math.isfinite(answer):
        # ``json.loads`` accepts ``Infinity``/``NaN`` and overflows ``1e999`` to inf.
        # Forwarding one emits invalid JSON to the callback door AND persists a
        # ``Infinity`` token into the transcript, which then fails the page's own
        # JSON parse on every reconnect for the whole transcript TTL.
        return "answer must be a finite number"
    if isinstance(answer, str) and len(answer) > _MAX_ANSWER_CHARS:
        return f"answer must be at most {_MAX_ANSWER_CHARS} characters"
    return None


def _body_refusal(exc: ValidationError) -> str:
    """The first field-level reason a message body was refused, so a caller can tell a
    malformed retry key from an unusable identity or an over-long text. Every part
    quoted is this door's own field name or message."""
    first = exc.errors()[0]
    field = ".".join(str(part) for part in first["loc"]) or "body"
    return f"invalid request body: {field}: {first['msg']}"


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


async def _forward_answer(callback_url: str, answer: Any) -> httpx.Response:
    """POST ``{"answer": <value>}`` to the interaction's callback door and return its
    response; the caller applies the status policy."""
    async with tai42_app.clients.client_ctx(HttpxClient, timeout=web_settings().http_timeout_seconds) as client:
        return await client.post(callback_url, json={"answer": answer})


@tai42_app.http.custom_route(
    "/api/channels/web/chat/{identity}",
    methods=["GET"],
    summary="Serve the public chat page for a web route",
    tags=["channels"],
    response_model=None,
    authed=False,
)
async def web_chat_page(request: Request) -> Response:
    """Serve the visitor-facing chat page and (re)establish the session.

    The identity is taken as-is for RENDERING: the page is served for ANY identity,
    and a name with no web route behind it surfaces on the visitor's first send —
    a friendly 404 for a canonical name, a 422 for one the sending doors refuse
    outright (blank, over-long, or ``:``-bearing) — rather than as a dead URL.
    A cookie that resolves to no registration —
    or to one minted on ANOTHER web route — gets a fresh token AND a fresh visitor id
    here, one of the two places a session is minted, so a planted, invented or foreign
    cookie value is replaced rather than adopted. A resolving one for this route is
    re-set, refreshing cookie and registration together so an active visitor never
    ages out mid-conversation.

    The session is bound to the bridge's canonical form of the identity, which is what
    the doors taking an identity compare against. A path segment with no canonical
    form (blank, over-long, or ``:``-bearing) is bound verbatim: those doors refuse
    such a body outright, so no session can ever present one.

    Minting is guarded because a mint is a state change on a GET: it happens only for
    a top-level navigation. A returning visitor mints nothing.

    Every refusal here is an HTML page, not JSON: this door is reached by navigating
    to it, so a JSON body would be rendered as text in the visitor's window.
    """
    if not _store_configured():
        return _refusal_page(_STORE_OFF_PAGE, 501)
    settings = web_settings()
    raw_identity = request.path_params["identity"]
    identity = _clean_identity(raw_identity) or raw_identity

    # Door order (README): parse + strip reserved -> params bounds -> navigation guard
    # -> gate check for mint-needing callers -> mint/refresh with params.
    params, params_refusal = _read_link_params(request, identity)
    if params_refusal is not None:
        return params_refusal
    entry_code = request.query_params.get("tai_entry")

    token = session_token(request, settings)
    try:
        registration = await resolve_session(token) if token is not None else None
    except SessionRecordError:
        # The dead record fails loud in the decoder; the door re-mints. Value-free.
        logger.warning("web chat page ignored a session for identity %s: its stored record was refused", identity)
        registration = None
    existing = token if _serves(registration, identity) else None
    navigation = is_document_navigation(request)

    if existing is None:
        # The navigation guard runs BEFORE the gate check: a non-navigation answers
        # ``not_a_navigation`` regardless of any presented code, so no response ever
        # differs by code validity (no oracle).
        if not navigation:
            return _refusal_page(_NOT_A_NAVIGATION_PAGE, 403)
        gate_refusal = await _entry_gate_refusal(request, identity, entry_code)
        if gate_refusal is not None:
            return gate_refusal

    try:
        build = load_build()
    except PublicBuildError as exc:
        logger.error("web chat page cannot be served: %s", exc)
        return _refusal_page(_PAGE_UNAVAILABLE_PAGE, 500)
    response = Response(
        render_page(raw_identity, settings.page_title, build),
        media_type=HTML_CONTENT_TYPE,
        headers={"content-security-policy": PAGE_CSP, **_NOSNIFF, "cache-control": _NO_STORE, **_REFERRER_POLICY},
    )
    if existing is not None:
        # A NAVIGATION carrying params rewrites the live visitor's params (same token,
        # same visitor id); a cross-site subresource must not, and empty params leave
        # the stored ones untouched.
        if params and navigation:
            assert registration is not None  # _serves guarantees it when existing is set
            await update_session_params(existing, registration, params)
        set_session_cookie(response, existing, settings)
    else:
        await _mint_session(response, identity, settings, params)
    return response


@tai42_app.http.custom_route(
    "/api/channels/web/assets/{file}",
    methods=["GET"],
    summary="Serve a built chat page asset",
    tags=["channels"],
    response_model=None,
    authed=False,
)
async def web_asset(request: Request) -> Response:
    """Serve one file of the built chat bundle.

    The requested name is looked up by EXACT match in the build's integrity map, so
    only files the build emitted are reachable and no name can address anything
    outside the bundle. A listed file missing from disk is a broken build, not a
    404 — it answers a loud 500.

    The bytes are streamed by ``FileResponse``, never read into the handler: the
    bundle is hundreds of kilobytes and every read would otherwise block the event
    loop for the whole file, on every request. The file is stat'ed ONCE, here, and
    the result handed to the response — which would otherwise stat it again for its
    own length/etag headers, two thread-pool round trips per asset request.
    """
    name = request.path_params["file"]
    try:
        build = load_build()
    except PublicBuildError as exc:
        logger.error("web chat asset cannot be served: %s", exc)
        return _error(_PAGE_UNAVAILABLE, 500)
    if name not in build.integrity:
        return _error("not found", 404)
    target = asset_path(name)
    try:
        stat_result = await run_sync(target.stat)
    except OSError:
        stat_result = None
    if stat_result is None or not S_ISREG(stat_result.st_mode):
        logger.error(
            "the chat page bundle is incomplete: %s is listed in the build manifest but is not a readable file on disk",
            target,
        )
        return _error(_PAGE_UNAVAILABLE, 500)
    return FileResponse(
        target,
        media_type=asset_content_type(name),
        headers={"cache-control": _HASHED_ASSET_CACHE, **_NOSNIFF},
        stat_result=stat_result,
    )


@tai42_app.http.custom_route(
    "/api/channels/web/messages",
    methods=["POST"],
    summary="Send a web chat message into the visitor's conversation",
    tags=["channels"],
    response_model=None,
    authed=False,
)
async def web_messages(request: Request) -> Response:
    """Bridge one visitor message into their own web conversation.

    The conversation ``client_address`` is the registered visitor id — never taken
    from the body — and the body's ``identity`` must be the one the session was minted
    on: a session bought on one web route buys nothing on another, and the refusal is
    the one a caller with no session at all gets. The ``provider_message_id`` is
    minted fresh per POST unless the
    body carries a ``client_message_id``, in which case it is derived from that key
    and the caller's address so a retried POST resolves to the first attempt's turn.
    The inbound transcript entry is appended only once ``accept`` has RETURNED a turn
    id (a refused message must not pollute the transcript; a shed one has an id and is
    appended, exactly as the bridge recorded it), and it reuses that ``message_id`` so
    the frame joins the bridge record. A retry therefore appends its frame a second
    time under the SAME id; the page keys the transcript by id and replaces in place,
    so the replay still shows one message.

    ``accept`` and that append are held under the conversation's write-order gate:
    a shed message's slow-down reply is spawned inside ``accept`` and would otherwise
    be able to land in the transcript ahead of the message that caused it.
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
    # The conversation identity is the minted visitor id, which the platform mints on
    # unauthenticated doors and a visitor can rotate at will — so it cannot bound spend.
    # The turn cap keys instead on the accountable NETWORK bucket, the same value the
    # public-door rate limiter derives for this request.
    cap_key = _client_bucket(request)

    async with transcript_order(body.identity, address):
        try:
            message_id = await tai42_app.conversations.accept(
                channel="web",
                our_identity=body.identity,
                client_address=address,
                cap_key=cap_key,
                text=body.text,
                provider_message_id=_provider_message_id(body.identity, address, body.client_message_id),
                # The captured link params ride the turn's tool payload under their own
                # key; empty -> None -> payload byte-identical to today.
                params=registration.params or None,
            )
        except BlankInboundTextError:
            return _error("message text is blank", 400)
        except LookupError as exc:
            return _error(str(exc), 404)
        except Exception as exc:
            if type(exc).__name__ == _THREAD_QUEUE_OVERFLOW:
                return _error(str(exc), 503)
            raise

        await append_message(
            body.identity,
            address,
            "in",
            body.text,
            entry_id=message_id,
            client_message_id=body.client_message_id,
        )
    return _ok({"message_id": message_id})


@tai42_app.http.custom_route(
    "/api/channels/web/stream",
    methods=["GET"],
    summary="Stream the visitor's web conversation (backlog then live)",
    tags=["channels"],
    response_model=None,
    authed=False,
)
async def web_stream(request: Request) -> Response:
    """Open the SSE feed of the session's own conversation.

    The transcript is keyed by ``(identity, visitor id)``, so a visitor only ever
    sees the conversation their own session addresses — and only on the web route
    their session was minted on, refused as a missing session otherwise. An
    unconfigured transcript store answers a plain 501+code up front rather than
    sending 200 + SSE headers and then dying mid-body, and an over-cap caller a plain
    503 that names which ceiling it hit — each open stream pins a dedicated Redis
    connection for its whole life.

    The caps are checked here but TAKEN by the generator: see
    ``check_stream_admission``.
    """
    off = _store_off()
    if off is not None:
        return off
    settings = web_settings()
    registration = await _session(request, settings)
    if registration is None:
        return _error(_SESSION_MISSING, 401, _SESSION_MISSING_CODE)
    requested = request.query_params.get("identity")
    if requested is None:
        return _error("the 'identity' query parameter is required", 400)
    identity = _clean_identity(requested)
    if identity is None:
        return _error(
            "the 'identity' query parameter must be a non-blank, ':'-free web route identity "
            f"of at most {_MAX_IDENTITY_CHARS} characters",
            400,
        )
    if not _serves(registration, identity):
        return _error(_SESSION_MISSING, 401, _SESSION_MISSING_CODE)
    address = registration.visitor_id
    try:
        check_stream_admission(address, settings)
    except StreamLimitError as exc:
        logger.warning("web chat stream refused: %s", exc)
        return _error(exc.visitor_message, 503)
    return StreamingResponse(
        stream_transcript(request, identity, address, settings),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no", **_NOSNIFF},
    )


@tai42_app.http.custom_route(
    "/api/channels/web/questions/{interaction_id}/answer",
    methods=["POST"],
    summary="Answer a pending web chat question",
    tags=["channels"],
    response_model=None,
    authed=False,
)
async def web_answer(request: Request) -> Response:
    """Forward an answer to a pending web question's interactions callback.

    The record is READ first and answered only when its conversation — BOTH the web
    route identity and the address — is the caller's own; a question is answerable
    solely from the conversation it was asked in, and a foreign one is reported as not
    found (never as "exists, but not yours"). Only
    then is it claimed with ``GETDEL`` (a missing record is a 404 — unknown, expired,
    already answered, or claimed by a racing duplicate). The status policy on the
    callback door's reply: a 2xx carrying ``already_answered`` means the door recorded
    SOMEONE ELSE's answer, so the record stays dropped and no ``chat.answered`` frame
    is written (it would show the visitor a value that was never recorded); any other
    2xx appends the frame and returns ok; 404 stays dropped (the ticket is gone); 400
    restores the record so the visitor can re-answer and surfaces the door's OWN error
    message (never a body an intermediary wrote — see ``_door_error_detail``);
    anything else (or a transport failure) restores the record and raises so the
    visitor can retry.

    One record may be restored only ``max_answer_restores`` times: a forward spends a
    slot of the interactions callback door's own rate limit, which is keyed on this
    server's egress IP and shared with every other channel's answer forwards, so an
    unbounded re-answer loop here drains a bucket the whole deployment rides.
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
    interaction_id = request.path_params["interaction_id"]

    raw, refusal = await _json_body(request, settings)
    if refusal is not None:
        return refusal
    if not isinstance(raw, dict) or "answer" not in raw:
        return _error("body must contain 'answer'", 400)
    answer = raw["answer"]
    bad_answer = _answer_refusal(answer)
    if bad_answer is not None:
        return _error(bad_answer, 422)

    pending = await peek_question(interaction_id)
    if pending is None or not _serves(registration, pending.identity) or pending.address != registration.visitor_id:
        return _error(_QUESTION_NOT_FOUND, 404)
    record = await claim_question(interaction_id)
    if record is None:
        return _error(_QUESTION_NOT_FOUND, 404)

    restores = settings.max_answer_restores
    try:
        forwarded = await _forward_answer(record.callback_url, answer)
    except httpx.HTTPError:
        # Transport failure — restore so the visitor's retry still resolves it.
        await restore_question(interaction_id, record, restores)
        raise

    if forwarded.status_code // 100 == 2:
        if _is_already_answered(forwarded):
            logger.info("interaction %s was already answered elsewhere; this forward recorded nothing", interaction_id)
            return _error(_ALREADY_ANSWERED, 409)
        # Under the conversation's write-order gate like every other agent-side
        # append: without it this frame can XADD ahead of the reply the answer sets
        # off, and the page replays the answer after what it answered.
        async with transcript_order(record.identity, record.address):
            await append_answered(record.identity, record.address, interaction_id, answer)
        return _ok({"status": "answered"})
    if forwarded.status_code == 404:
        # Terminal: the ticket is gone; retrying can't succeed, so stay dropped.
        logger.warning("callback door returned terminal HTTP 404 for %s; dropping the question", interaction_id)
        return _error("question expired or withdrawn", 404)
    if forwarded.status_code == 400:
        # The door rejected THIS answer; keep the record so the visitor can re-answer
        # — until the restore cap is spent, when the question is gone and they are
        # told so rather than left re-answering into a 404.
        detail = _door_error_detail(forwarded)
        if not await restore_question(interaction_id, record, restores):
            return _error(f"{detail}; {_ANSWER_RETRIES_SPENT}", 400)
        return _error(detail, 400)
    await restore_question(interaction_id, record, restores)
    raise AnswerForwardError(
        f"interactions callback rejected the answer: HTTP {forwarded.status_code}: {forwarded.text[:500]}"
    )


@tai42_app.http.custom_route(
    "/api/channels/web/session/rotate",
    methods=["POST"],
    summary="Start a fresh web chat visitor session",
    tags=["channels"],
    response_model=None,
    authed=False,
)
async def web_session_rotate(request: Request) -> Response:
    """Mint a fresh session for one web route and set it as the visitor's cookie.

    The body names the route, because a session is bound to one and this door mints
    without reading a URL: the fresh session serves that route and no other. It is not
    a credential check — anyone may open the chat page of any route and be minted a
    session there — so no session is required to rotate, exactly as before.

    The old registration is DELETED, so the token that reached this door can never
    address the old conversation again. The new visitor id holds no transcript, so
    the conversation starts empty on the next message; the old transcript is
    untouched and ages out on its own TTL.
    """
    if is_cross_origin(request):
        return _error(_CROSS_ORIGIN, 403, _CROSS_ORIGIN_CODE)
    off = _store_off()
    if off is not None:
        return off
    settings = web_settings()
    raw, refusal = await _json_body(request, settings)
    if refusal is not None:
        return refusal
    try:
        body = RotateBody.model_validate(raw)
    except ValidationError as exc:
        return _error(_body_refusal(exc), 422)
    # A rotation mints a session, so a gated identity gates it too — same throttle then
    # code check, same wording as the page refusal. An ungated identity ignores the code.
    if await is_gate_enabled(body.identity):
        bucket = _client_bucket(request)
        if (
            body.entry_code is None
            or not await entry_attempt_allowed(bucket)
            or not await check_entry_code(body.identity, body.entry_code)
        ):
            logger.warning("web chat rotate refused entry for identity %s (bucket %s)", body.identity, bucket)
            return _error(_ENTRY_REFUSED_MESSAGE, 403, _ENTRY_REFUSED_CODE)
    token = session_token(request, settings)
    if token is not None:
        await drop_session(token)
    response = _ok({"status": "rotated"})
    # A rotation mints a CLEAN registration: it carries no link params (README pin).
    await _mint_session(response, body.identity, settings, {})
    return response


# -- management doors (authed=True, platform api key) ---------------------------
#
# The entry gate's operator plane. Unlike the public chat doors these require the
# platform api key (``authed=True``) and declare an explicit action-class — an authed
# route with none REFUSES TO REGISTER (fail-closed fence). They refuse with the
# standard JSON envelope; they are not navigations and get no refusal pages.


def _managed_identity(request: Request) -> str | None:
    """The canonical identity a management door acts on, or ``None`` when the path
    segment is unusable (the door answers a 422)."""
    return _clean_identity(request.path_params["identity"])


def _code_view(code: EntryCode) -> dict[str, Any]:
    return {
        "code_id": code.code_id,
        "label": code.label,
        "created_at": code.created_at,
        "expires_at": code.expires_at,
    }


@tai42_app.http.custom_route(
    "/api/channels/web/gates/{identity}",
    methods=["GET"],
    summary="Read a web route's entry-gate state and its codes",
    tags=["channels"],
    response_model=None,
    authed=True,
    action="read",
)
async def web_gate_read(request: Request) -> Response:
    """The gate flag for a web route and the live codes minted for it (never the raw
    codes — only their ids and metadata)."""
    identity = _managed_identity(request)
    if identity is None:
        return _error(_IDENTITY_REQUIREMENT, 422)
    off = _store_off()
    if off is not None:
        return off
    enabled = await is_gate_enabled(identity)
    codes = await list_entry_codes(identity)
    return _ok({"enabled": enabled, "codes": [_code_view(code) for code in codes]})


@tai42_app.http.custom_route(
    "/api/channels/web/gates/{identity}",
    methods=["PUT"],
    summary="Turn a web route's entry gate on or off",
    tags=["channels"],
    response_model=None,
    request_model=GateToggleBody,
    authed=True,
    action="write",
)
async def web_gate_toggle(request: Request) -> Response:
    """Set the explicit gate flag. Turning it off does not touch the codes; turning it
    on with no live code makes the route unreachable until one is minted."""
    identity = _managed_identity(request)
    if identity is None:
        return _error(_IDENTITY_REQUIREMENT, 422)
    off = _store_off()
    if off is not None:
        return off
    settings = web_settings()
    raw, refusal = await _json_body(request, settings)
    if refusal is not None:
        return refusal
    try:
        body = GateToggleBody.model_validate(raw)
    except ValidationError as exc:
        return _error(_body_refusal(exc), 422)
    await set_gate(identity, body.enabled)
    return _ok({"enabled": body.enabled})


@tai42_app.http.custom_route(
    "/api/channels/web/gates/{identity}/codes",
    methods=["POST"],
    summary="Mint an entry code for a web route",
    tags=["channels"],
    response_model=None,
    request_model=MintCodeBody,
    authed=True,
    action="write",
)
async def web_gate_mint_code(request: Request) -> Response:
    """Mint a multi-use entry code. The raw code is returned ONCE, here — only its hash
    is stored, so it can never be read back."""
    identity = _managed_identity(request)
    if identity is None:
        return _error(_IDENTITY_REQUIREMENT, 422)
    off = _store_off()
    if off is not None:
        return off
    settings = web_settings()
    raw, refusal = await _json_body(request, settings)
    if refusal is not None:
        return refusal
    try:
        body = MintCodeBody.model_validate(raw)
    except ValidationError as exc:
        return _error(_body_refusal(exc), 422)
    raw_code, code_id = await mint_entry_code(identity, body.label, body.expires_at)
    expires_at = body.expires_at.astimezone(UTC).isoformat() if body.expires_at is not None else None
    return _ok({"code": raw_code, "code_id": code_id, "expires_at": expires_at})


@tai42_app.http.custom_route(
    "/api/channels/web/gates/{identity}/codes/{code_id}",
    methods=["DELETE"],
    summary="Revoke a web route's entry code",
    tags=["channels"],
    response_model=None,
    authed=True,
    action="write",
)
async def web_gate_revoke_code(request: Request) -> Response:
    """Revoke one code by its id; an unknown id is a 404 envelope error."""
    identity = _managed_identity(request)
    if identity is None:
        return _error(_IDENTITY_REQUIREMENT, 422)
    off = _store_off()
    if off is not None:
        return off
    code_id = request.path_params["code_id"]
    if not await revoke_entry_code(identity, code_id):
        return _error("entry code not found", 404)
    return _ok({"status": "revoked"})
