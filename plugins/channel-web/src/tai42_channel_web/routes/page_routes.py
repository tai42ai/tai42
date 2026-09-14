"""The chat-page navigation doors: serve the page (minting/refreshing the session) and
serve one built bundle asset."""

from __future__ import annotations

import logging
from stat import S_ISREG

from anyio.to_thread import run_sync
from starlette.requests import Request
from starlette.responses import FileResponse, Response
from tai42_contract.app import tai42_app
from tai42_contract.conversations import validate_entry_params

from tai42_channel_web.page import (
    HTML_CONTENT_TYPE,
    PAGE_CSP,
    PublicBuildError,
    asset_content_type,
    asset_path,
    load_build,
    render_page,
    render_refusal,
)
from tai42_channel_web.routes.dtos import _clean_identity
from tai42_channel_web.routes.entry_admission import _ENTRY_REFUSED_CODE, _ENTRY_REFUSED_MESSAGE, _entry_gate_outcome
from tai42_channel_web.routes.envelope import _NO_STORE, _NOSNIFF, _REFERRER_POLICY, _error, _refusal_page
from tai42_channel_web.routes.session_access import (
    _STORE_OFF_CODE,
    _mint_session,
    _mount_base,
    _serves,
    _store_configured,
)
from tai42_channel_web.session import (
    is_document_navigation,
    session_token,
    set_session_cookie,
)
from tai42_channel_web.settings import web_settings
from tai42_channel_web.store.registrations import SessionRecordError, resolve_session, update_session_params

logger = logging.getLogger(__name__)

# Minting a session is a state change, so only a top-level navigation may do it: a
# cross-site subresource pointed at the chat URL would otherwise overwrite a live
# visitor's cookie and strand their conversation.
_NOT_A_NAVIGATION_CODE = "not_a_navigation"

# A bundle file is content-hashed, so its bytes at a given name never change.
_HASHED_ASSET_CACHE = "public, max-age=31536000, immutable"

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

# Query names the web door consumes itself and NEVER stores or delivers as params:
# ``tai_pair`` (client-consumed) and the entry-gate code ``tai_entry``. Link
# param VALUES (and the entry code) never appear in any log line, error body, or
# transcript frame — keys may be logged, values never.
_RESERVED_QUERY_PARAMS = frozenset({"tai_pair", "tai_entry"})

# The link params carried a value that violates a bound (count, key shape, value
# length, or total size). One byte-constant page; the log names the bound.
_LINK_PARAMS_INVALID_CODE = "link_params_invalid"
_LINK_PARAMS_INVALID_PAGE = render_refusal(
    "This chat link is not valid",
    "This chat link is not valid. Please check the link and try again.",
    _LINK_PARAMS_INVALID_CODE,
)

# One page and one wording for a gated route with no live code — no oracle.
_ENTRY_REFUSED_PAGE = render_refusal("This chat is private", _ENTRY_REFUSED_MESSAGE, _ENTRY_REFUSED_CODE)


def _read_link_params(request: Request, identity: str) -> tuple[dict[str, str], Response | None]:
    """Parse the navigation's query into the validated link params, or a byte-constant
    400 refusal page. A DUPLICATE key — checked on the RAW query, reserved names
    included, so ``?tai_pair=a&tai_pair=b`` is a 400 too — is a bound violation; the reserved
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
    """The page door's entry-gate refusal for a mint-needing caller on ``identity``, or
    ``None`` when the route is ungated or the code is live. Runs AFTER the navigation
    guard (README door order), so a non-navigation never reaches it and no response
    differs by code validity; a refused entry is the one byte-constant page."""
    if await _entry_gate_outcome(request, identity, entry_code) is not None:
        return _refusal_page(_ENTRY_REFUSED_PAGE, 403)
    return None


@tai42_app.http.custom_route(
    "/chat/{identity}",
    methods=["GET"],
    summary="Serve the public chat page for a web route",
    tags=["channels"],
    response_model=None,
    no_body_reason="Chat HTML page: text/html document, not a JSON body",
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
    mount_base = _mount_base(request, "/chat/{identity}")
    response = Response(
        render_page(raw_identity, settings.page_title, build, mount_base),
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
        set_session_cookie(response, existing, settings, mount_base)
    else:
        await _mint_session(response, identity, settings, params, mount_base)
    return response


@tai42_app.http.custom_route(
    "/assets/{file}",
    methods=["GET"],
    summary="Serve a built chat page asset",
    tags=["channels"],
    response_model=None,
    no_body_reason="Static asset FileResponse: raw bytes",
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
