"""The visitor session: the ``tai_web_session`` cookie, and the two ids behind it.

A visitor is anonymous — this plugin has no login and no authenticated caller. The
chat page mints a SECRET token into an httpOnly cookie and registers it server-side
(``store.register_session``) against a separately minted, NON-secret ``visitor_id``;
every door resolves the cookie through that registration and uses the ``visitor_id``
as the conversation ``client_address``.

The split is what the two ids are for:

* the token is the bearer capability and nothing else — holding the cookie is
  holding the conversation, so it is minted from a CSPRNG at >=128 bits and accepted
  back only in the minted alphabet and length;
* the ``visitor_id`` is the durable address the bridge, the transcript keys, and the
  operator plane all publish — reading one grants nothing.

A token with no registration is NOT a session (the page mints a fresh one rather
than adopting it): an invented cookie can neither open a conversation nor start a
fresh address that dodges the bridge's per-address turn cap, and a planted one is
never adopted. A registration also records the web route it was minted on, and a
door refuses a session presented on any other route exactly as it refuses an
unknown token — a session is a capability on ONE route.

The CSRF posture is two independent legs, each of which refuses a cross-site POST on
its own: ``SameSite=Lax`` withholds the session cookie from it (so the door resolves
no session and answers 401), and the ``Origin`` guard refuses the mismatched origin a
browser attaches to every cross-site POST (403). The JSON body is NOT a third leg —
a cross-site form can post a JSON-shaped body with ``enctype="text/plain"``, and no
door checks ``Content-Type``.
"""

from __future__ import annotations

import re
import secrets

from starlette.requests import Request
from starlette.responses import Response

from tai42_channel_web.settings import WebSettings

SESSION_COOKIE_BASE = "tai_web_session"
# A browser accepts a ``__Host-`` cookie only when it is ``Secure``, ``Path=/`` and
# carries no ``Domain`` — and in exchange no sibling host of this origin can plant
# or overwrite it. All three conditions travel together, so ONE flag decides the
# whole naming: a Secure deployment gets the prefixed name at the root path, and a
# plain-http one (dev / e2e), which can satisfy none of the three, gets the bare
# name scoped to this plugin's own mount prefix, where the page, its assets and the
# chat doors all live.
_HOST_PREFIX = "__Host-"


def session_cookie_name(secure: bool) -> str:
    """The cookie name this deployment mints and reads back. The two names are never
    accepted interchangeably: the mode decides one name, and a cookie under the other
    is not this deployment's."""
    return f"{_HOST_PREFIX}{SESSION_COOKIE_BASE}" if secure else SESSION_COOKIE_BASE


def session_cookie_path(secure: bool, mount_base: str) -> str:
    """The cookie's ``Path``. ``/`` is what the ``__Host-`` prefix requires; without
    the prefix the capability is scoped to this deployment's mount prefix, so a
    remapped base is followed rather than the default hardcoded."""
    return "/" if secure else mount_base


# 32 bytes -> a 43-character urlsafe token. The accepted range starts at 22
# characters (128 bits) so a truncated token is refused exactly like a missing
# cookie.
_SESSION_TOKEN_BYTES = 32
_SESSION_TOKEN = re.compile(r"^[A-Za-z0-9_-]{22,64}$")

# The visitor id is not a credential, only an address: 12 bytes is far past any
# collision risk over a transcript TTL. Urlsafe-encoded, so it is ``:``-free and can
# never split a transcript key or a composite recipient.
_VISITOR_ID_BYTES = 12


def mint_session_token() -> str:
    """A fresh session cookie token: CSPRNG bytes, urlsafe-encoded. The bearer
    secret — it is never used as an address."""
    return secrets.token_urlsafe(_SESSION_TOKEN_BYTES)


def mint_visitor_id() -> str:
    """A fresh conversation address for one visitor. Opaque and non-secret: it is
    published by the bridge and the operator plane, so it must not be the token."""
    return secrets.token_urlsafe(_VISITOR_ID_BYTES)


def session_token(request: Request, settings: WebSettings) -> str | None:
    """The caller's cookie token, or ``None`` when there is no cookie under this
    deployment's name or its value is not a minted token — both mean "no session"
    before the store is even asked."""
    value = request.cookies.get(session_cookie_name(settings.session_cookie_secure))
    if value is None or _SESSION_TOKEN.match(value) is None:
        return None
    return value


def set_session_cookie(response: Response, token: str, settings: WebSettings, mount_base: str) -> None:
    """Set (or refresh the ``Max-Age`` of) the visitor's session cookie.

    ``mount_base`` is this deployment's absolute mount prefix for the web channel; a
    plain-http deployment scopes the cookie ``Path`` to it. ``httponly`` keeps the
    capability out of page script entirely; ``samesite=lax`` withholds it from
    cross-site POSTs while still arriving on a link-followed page load."""
    secure = settings.session_cookie_secure
    response.set_cookie(
        session_cookie_name(secure),
        token,
        max_age=settings.session_ttl_seconds,
        path=session_cookie_path(secure, mount_base),
        httponly=True,
        secure=secure,
        samesite="lax",
    )


def _request_origin(request: Request) -> str:
    """This request's own public origin as the browser sees it: scheme and host from
    ``X-Forwarded-Proto`` / ``X-Forwarded-Host`` when present (first value of a
    list), else the request's own scheme and ``Host``.

    The forwarded headers are read with NO trusted-proxy check. That is sound for the
    one thing this feeds: a browser can attach ``X-Forwarded-Host`` only on a
    preflighted cross-origin fetch, and these doors return no CORS headers, so the
    preflight is what fails. A non-browser client can forge it, but it can equally
    omit ``Origin`` — the CSRF guard was never the credential."""
    proto_header = request.headers.get("x-forwarded-proto")
    proto = proto_header.split(",")[0].strip() if proto_header else request.url.scheme
    host_header = request.headers.get("x-forwarded-host") or request.headers.get("host")
    host = host_header.split(",")[0].strip() if host_header else request.url.netloc
    return f"{proto}://{host}"


def is_document_navigation(request: Request) -> bool:
    """``True`` when the browser says this request is a top-level document load, or
    says nothing at all.

    ``Sec-Fetch-Dest`` is what separates opening the chat page from a cross-site
    ``<img>``/``<script>``/``fetch`` pointed at the same URL. Only a navigation may
    MINT a session: a subresource load would otherwise overwrite a live visitor's
    cookie and strand their conversation. The header is absent on older browsers and
    on every non-browser client, and those are admitted — the guard removes a
    cross-site denial, it is not a credential."""
    dest = request.headers.get("sec-fetch-dest")
    return dest is None or dest.strip().lower() == "document"


def is_cross_origin(request: Request) -> bool:
    """``True`` when the request carries an ``Origin`` naming a DIFFERENT origin than
    the one it reached. A browser sends ``Origin`` on every cross-site POST, so this
    is the CSRF refusal; a request with no ``Origin`` at all (same-origin GET, a
    non-browser client) is not cross-site by this test."""
    origin = request.headers.get("origin")
    if origin is None:
        return False
    return origin.strip().rstrip("/") != _request_origin(request)
