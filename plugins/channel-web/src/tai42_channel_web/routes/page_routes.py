"""The chat-page navigation doors: serve the page (minting/refreshing the session) and serve a bundle asset."""

from __future__ import annotations

import logging
from functools import lru_cache
from stat import S_ISREG

from anyio.to_thread import run_sync
from starlette.requests import Request
from starlette.responses import FileResponse, Response
from tai42_contract.app import DeclaredRouteMetadata, tai42_app
from tai42_kit.conversations.entry_params import OverBoundParamsMode, bound_entry_params

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
from tai42_channel_web.routes.entry_admission import _ENTRY_REFUSED_CODE, _entry_gate_outcome
from tai42_channel_web.routes.envelope import _NO_STORE, _NOSNIFF, _REFERRER_POLICY, _error, _refusal_page
from tai42_channel_web.routes.limits import CHAT_RATE_LIMIT_BUDGET, CHAT_RATE_LIMIT_FAMILY
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
from tai42_channel_web.store.words import get_words
from tai42_channel_web.words import WebWords, page_refusal

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

# The page door's refusals, as the pages a browser renders instead of a raw body. Each
# is drawn from the route's own words where it set them, else the built-in English
# (``words.DEFAULT_PAGE_WORDS``); the store-off page from the deployment's settings,
# since with no store there are no route words to read. Every page is byte-constant per
# route: the prose is the visitor's, the code meta and the log line carry the
# operator's detail, and no page names anything about the server beyond its own
# refusal code.


@lru_cache(maxsize=8)
def _store_off_page(title: str, text: str) -> str:
    return render_refusal(title, text, _STORE_OFF_CODE)


def _words_page(words: WebWords | None, member: str, code: str | None = None) -> str:
    heading, text = page_refusal(words, member)
    return render_refusal(heading, text, code)


# Query names the web door consumes itself and NEVER stores or delivers as params:
# ``tai_pair`` (client-consumed) and the entry-gate code ``tai_entry``. Link
# param VALUES (and the entry code) never appear in any log line, error body, or
# transcript frame — keys may be logged, values never.
_RESERVED_QUERY_PARAMS = frozenset({"tai_pair", "tai_entry"})

# The link params carried a value that violates a bound (count, key shape, value
# length, or total size). One byte-constant page per route; the log names the bound.
_LINK_PARAMS_INVALID_CODE = "link_params_invalid"


def _read_link_params(
    request: Request, identity: str, words: WebWords | None
) -> tuple[dict[str, str], Response | None]:
    """Parse the navigation's query into the validated link params, or a byte-constant 400 refusal page.

    A DUPLICATE key — checked on the RAW query, reserved names included, so ``?tai_pair=a&tai_pair=b`` is a
    400 too — is a bound violation; the reserved names are then stripped before validation. Param VALUES
    never reach the log: the duplicate warning names no value, and the bound refusal names only the
    violated bound (and at most a key).
    """
    pairs = request.query_params.multi_items()
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        logger.warning("web chat page refused link params for identity %s: a query parameter is repeated", identity)
        return {}, _refusal_page(_words_page(words, "link_invalid", _LINK_PARAMS_INVALID_CODE), 400)
    candidate = {key: value for key, value in pairs if key not in _RESERVED_QUERY_PARAMS}
    try:
        return bound_entry_params(candidate, mode=OverBoundParamsMode.REFUSE, door="web") or {}, None
    except ValueError as exc:
        logger.warning("web chat page refused link params for identity %s: %s", identity, exc)
        return {}, _refusal_page(_words_page(words, "link_invalid", _LINK_PARAMS_INVALID_CODE), 400)


async def _entry_gate_refusal(
    request: Request, identity: str, entry_code: str | None, words: WebWords | None
) -> Response | None:
    """The page door's entry-gate refusal for a mint-needing caller on ``identity``, or ``None`` when open.

    ``None`` when the route is ungated or the code is live. Runs AFTER the navigation guard (README door
    order), so a non-navigation never reaches it and no response differs by code validity; a refused entry
    is the one byte-constant page for the route.
    """
    if await _entry_gate_outcome(request, identity, entry_code) is not None:
        return _refusal_page(_words_page(words, "entry_refused", _ENTRY_REFUSED_CODE), 403)
    return None


@tai42_app.http.custom_route(
    "/chat/{identity}",
    methods=["GET"],
    summary="Serve the public chat page for a web route",
    tags=["channels"],
    response_model=None,
    no_body_reason="Chat HTML page: text/html document, not a JSON body",
    success_media_types={"GET": ("text/html",)},
    declared=DeclaredRouteMetadata(
        reload_gated=False,
        reads_body=False,
        error_statuses=(400, 403, 500, 501),
        success_status=200,
        rate_limit_family=CHAT_RATE_LIMIT_FAMILY,
        rate_limit_budget=CHAT_RATE_LIMIT_BUDGET,
    ),
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
    to it, so a JSON body would be rendered as text in the visitor's window. Past the
    store check the route's own words are read once: each refusal page draws its heading
    and sentence from them where set, and the served page hands the widget the route's
    refusal sentences on ``#root``'s ``data-words``.
    """
    settings = web_settings()
    if not _store_configured():
        return _refusal_page(_store_off_page(settings.store_off_title, settings.store_off_text), 501)
    raw_identity = request.path_params["identity"]
    identity = _clean_identity(raw_identity) or raw_identity
    # The route's own words, read once: every refusal below and the page itself draw them.
    words = await get_words(identity)

    # Door order (README): parse + strip reserved -> params bounds -> navigation guard
    # -> gate check for mint-needing callers -> mint/refresh with params.
    params, params_refusal = _read_link_params(request, identity, words)
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
            return _refusal_page(_words_page(words, "not_a_navigation", _NOT_A_NAVIGATION_CODE), 403)
        gate_refusal = await _entry_gate_refusal(request, identity, entry_code, words)
        if gate_refusal is not None:
            return gate_refusal

    try:
        build = load_build()
    except PublicBuildError as exc:
        # Surface the actionable build error (path + build step) in the message line for operators.
        logger.exception("web chat page cannot be served: %s", exc)  # noqa: TRY401
        return _refusal_page(_words_page(words, "page_unavailable"), 500)
    mount_base = _mount_base(request, "/chat/{identity}")
    response = Response(
        render_page(raw_identity, settings.page_title, build, mount_base, words.transport_payload() if words else None),
        media_type=HTML_CONTENT_TYPE,
        headers={"content-security-policy": PAGE_CSP, **_NOSNIFF, "cache-control": _NO_STORE, **_REFERRER_POLICY},
    )
    if existing is not None:
        # A NAVIGATION carrying params rewrites the live visitor's params (same token,
        # same visitor id); a cross-site subresource must not, and empty params leave
        # the stored ones untouched.
        if params and navigation:
            assert registration is not None  # noqa: S101 (type-narrowing invariant guaranteed above; assert keeps the complexity floor)
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
    success_media_types={"GET": ("application/octet-stream",)},
    declared=DeclaredRouteMetadata(
        reload_gated=False,
        reads_body=False,
        error_statuses=(404, 500),
        success_status=200,
        rate_limit_family=CHAT_RATE_LIMIT_FAMILY,
        rate_limit_budget=CHAT_RATE_LIMIT_BUDGET,
    ),
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
        # Surface the actionable build error (path + build step) in the message line for operators.
        logger.exception("web chat asset cannot be served: %s", exc)  # noqa: TRY401
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
