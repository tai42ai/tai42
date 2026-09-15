"""The caller's session and the transcript-store gate.

Resolve the cookie to its registration, decide whether it serves a route, mint a fresh
one, and refuse when no store is configured.
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from tai42_kit.utils.client_address import XFF_HEADER, client_bucket

from tai42_channel_web.routes.envelope import _error
from tai42_channel_web.session import (
    mint_session_token,
    mint_visitor_id,
    session_token,
    set_session_cookie,
)
from tai42_channel_web.settings import WebSettings, web_redis_settings
from tai42_channel_web.store.registrations import (
    SessionRecordError,
    SessionRegistration,
    register_session,
    resolve_session,
)

logger = logging.getLogger(__name__)

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


def _store_configured() -> bool:
    """Whether a transcript store is configured.

    Without one a session cannot be registered, so every door but the asset door refuses
    with 501.
    """
    return bool(web_redis_settings().redis_url)


def _store_off() -> JSONResponse | None:
    """That 501 as the API doors answer it, or ``None`` when a store IS configured."""
    if _store_configured():
        return None
    return _error(_STORE_OFF, 501, _STORE_OFF_CODE)


async def _session(request: Request, settings: WebSettings) -> SessionRegistration | None:
    """The caller's session: the registration their cookie token stands for.

    Carries the conversation address to use, and the web route it was minted on. ``None``
    when there is no cookie, its value is not a minted token, or nothing is registered for
    it — an invented or planted token is never adopted as a session.
    """
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
    """Whether this session may act on ``identity``.

    A session minted on another web route is refused exactly as a missing one is: a caller
    must not be able to tell a foreign session from no session, or the refusal itself would
    say which routes a stolen cookie is good for.
    """
    return registration is not None and registration.identity == identity


def _mount_base(request: Request, route_path: str) -> str:
    """This deployment's absolute mount prefix for the web channel.

    Read from the request path by dropping this route's own relative tail. A remapped base
    is followed rather than the default assumed — the served page's asset URLs and the
    session cookie's ``Path`` both derive from it.
    """
    depth = len(route_path.strip("/").split("/"))
    return "/".join(request.url.path.split("/")[:-depth])


async def _mint_session(
    response: Response, identity: str, settings: WebSettings, params: dict[str, str], mount_base: str
) -> None:
    """Register a fresh token/visitor-id pair for one web route and set the cookie.

    Carries the entry's link params. The registration lands first: a cookie whose token
    resolves to nothing is not a session.
    """
    token = mint_session_token()
    await register_session(token, mint_visitor_id(), identity, params)
    set_session_cookie(response, token, settings, mount_base)


def _client_bucket(request: Request) -> str:
    """The accountable network client bucket for a request.

    The same value the public-door rate limiter derives, keyed on the network peer, never
    a resettable visitor id. It is what the turn cap and the entry-gate throttle hold
    accountable.
    """
    return client_bucket(request.client.host if request.client else None, request.headers.get(XFF_HEADER, ""))
