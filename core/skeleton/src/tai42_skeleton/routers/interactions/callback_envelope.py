"""The request/response envelope the ticketed callback POST doors share.

The answer callback door and the mid-form reaction door both read their body through
:func:`callback_post_size_guard` (the one bounded read, capped on the query string and the
body bytes alike) and answer through :func:`callback_json`.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from tai42_kit.net.request_body import RequestBodyTooLargeError, read_bounded_body

from tai42_skeleton.interactions.settings import InteractionsSettings

from .pages import _BASE_HEADERS

__all__ = ["callback_json", "callback_post_size_guard"]


def callback_json(payload: dict, status_code: int) -> JSONResponse:
    """A callback door's JSON reply, carrying the doors' base response headers."""
    return JSONResponse(payload, status_code=status_code, headers=dict(_BASE_HEADERS))


async def callback_post_size_guard(request: Request, settings: InteractionsSettings) -> Response | bytes:
    """Enforce ``callback_max_body_bytes`` on BOTH the raw query string and the actual body bytes.

    The confirm-flow answer rides the URL, so both are capped. Return the 413 response
    when either exceeds the cap, else the bounded body bytes — the one read of the body.
    Runs identically in the OFF gate and the configured POST path so an oversized
    POST answers the SAME 413 whether or not the store is configured — the public
    door leaks no configured-vs-off oracle through the size cap.
    """
    if len(request.url.query.encode()) > settings.callback_max_body_bytes:
        return callback_json({"error": "payload too large"}, 413)
    try:
        return await read_bounded_body(request, settings.callback_max_body_bytes)
    except RequestBodyTooLargeError:
        return callback_json({"error": "payload too large"}, 413)
