"""The per-identity words management doors (authed, platform api key): read, replace and clear a web route's words.

Like the entry-gate doors they require the platform api key (declared ``public: false``) and
declare an explicit ``read``/``write`` action-class. A ``PUT`` carries the whole words object and
replaces what is stored; a body outside :class:`~tai42_channel_web.words.WebWords` answers 422 with
the model's message. Importing this module also arms the words backup section.
"""

from __future__ import annotations

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import Response
from tai42_contract.app import tai42_app

# Arms the words backup-section on_startup hook. Homed in a manifest-loaded routes module the
# host imports only post-bind, never in the package ``__init__``: a pre-bind package import must
# never touch the bound handle.
from tai42_channel_web import backup as _backup  # noqa: F401
from tai42_channel_web.routes.dtos import _IDENTITY_REQUIREMENT, WordsClearedResponse
from tai42_channel_web.routes.envelope import _body_refusal, _error, _json_body, _ok
from tai42_channel_web.routes.gate_routes import _managed_identity
from tai42_channel_web.routes.session_access import _store_off
from tai42_channel_web.settings import web_settings
from tai42_channel_web.store.words import clear_words, get_words, set_words
from tai42_channel_web.words import WebWords, stored_payload

_NO_WORDS = "no words are set for this web route"


@tai42_app.http.custom_route(
    "/words/{identity}",
    methods=["GET"],
    summary="Read a web route's own words",
    tags=["channels"],
    response_model=WebWords,
    action="read",
)
async def web_words_read(request: Request) -> Response:
    """The words stored for a web route; 404 when none are set."""
    identity = _managed_identity(request)
    if identity is None:
        return _error(_IDENTITY_REQUIREMENT, 422)
    off = _store_off()
    if off is not None:
        return off
    words = await get_words(identity)
    if words is None:
        return _error(_NO_WORDS, 404)
    return _ok(stored_payload(words))


@tai42_app.http.custom_route(
    "/words/{identity}",
    methods=["PUT"],
    summary="Replace a web route's own words",
    tags=["channels"],
    response_model=WebWords,
    request_model=WebWords,
    action="write",
)
async def web_words_replace(request: Request) -> Response:
    """Store the body as the route's whole words object and answer what is now stored."""
    identity = _managed_identity(request)
    if identity is None:
        return _error(_IDENTITY_REQUIREMENT, 422)
    off = _store_off()
    if off is not None:
        return off
    raw, refusal = await _json_body(request, web_settings())
    if refusal is not None:
        return refusal
    try:
        words = WebWords.model_validate(raw)
    except ValidationError as exc:
        return _error(_body_refusal(exc), 422)
    await set_words(identity, words)
    return _ok(stored_payload(words))


@tai42_app.http.custom_route(
    "/words/{identity}",
    methods=["DELETE"],
    summary="Clear a web route's own words",
    tags=["channels"],
    response_model=WordsClearedResponse,
    action="write",
)
async def web_words_clear(request: Request) -> Response:
    """Delete the route's words, so every page and sentence draws the built-in English; 404 when none are set."""
    identity = _managed_identity(request)
    if identity is None:
        return _error(_IDENTITY_REQUIREMENT, 422)
    off = _store_off()
    if off is not None:
        return off
    if not await clear_words(identity):
        return _error(_NO_WORDS, 404)
    return _ok({"status": "cleared"})
