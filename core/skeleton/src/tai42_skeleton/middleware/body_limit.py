"""App-level request body-size cap — the backstop on every route.

Public doors read their bodies bounded already (a 64 KiB-class cap); the AUTHED
routes read the whole body unbounded (``request.json()`` and friends). This
middleware caps EVERY route in one place: the running total of the ACTUAL body
bytes read is bounded (never a client-declared ``Content-Length``, which is
advisory), and an over-cap request is answered with 413, loudly — never a
silently truncated stream.

The escape signal is a module-private ``_BodyTooLargeError`` that subclasses
``Exception`` DIRECTLY, never ``ValueError``: several routes wrap
``request.json()`` in ``except ValueError`` (``routers/backup.py``,
``routers/_tool_call.py``), so a ``ValueError``-based escape would be swallowed
into a 400 and the 413 would never fire. Raised from the wrapped ``receive`` and
caught around the inner app: before response-start it is converted to a 413,
after start it is re-raised (the stream cannot be un-sent).

This runs INSIDE the base app's own Starlette stack (via the base-app middleware
list, so it sits inside that app's ``ServerErrorMiddleware``): the over-cap escape
must reach this handler and become a 413 before any error middleware turns the
raised ``_BodyTooLargeError`` into a 500. Always on; tune via
``TAI_BODY_LIMIT_MAX_BODY_BYTES``.
"""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tai42_skeleton.settings.body_limit import body_limit_settings


class _BodyTooLargeError(Exception):
    """Internal escape raised once the accumulated body exceeds the cap.

    Subclasses ``Exception`` DIRECTLY (never ``ValueError``) so a route's
    ``except ValueError`` around ``request.json()`` cannot swallow it into a 400.
    """


class BodyLimitMiddleware:
    """Caps every request body at ``TAI_BODY_LIMIT_MAX_BODY_BYTES`` actual bytes.

    Over-cap answers 413. Non-http scopes pass straight through.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the inner ASGI ``app``."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Cap this request's body, converting an over-cap read into a 413 before response start."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        cap = body_limit_settings().max_body_bytes

        # Up-front reject: a declared Content-Length already over the cap is
        # refused before a single body byte is read.
        if self._declared_length_over_cap(scope, cap):
            await self._reject(cap, scope, receive, send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > cap:
                    raise _BodyTooLargeError
            return message

        response_started = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLargeError:
            if response_started:
                # The response head is already on the wire; the stream cannot be
                # un-sent, so surface the over-cap loudly rather than truncate.
                raise
            await self._reject(cap, scope, receive, send)

    @staticmethod
    def _declared_length_over_cap(scope: Scope, cap: int) -> bool:
        """Whether the advisory ``Content-Length`` header already declares a body over the cap.

        The header is advisory, so this only powers the up-front reject; the real bound
        is the running total of body bytes actually read.
        """
        declared = Headers(scope=scope).get("content-length")
        if declared is None:
            return False
        try:
            declared_len = int(declared)
        except ValueError:
            return False
        return declared_len > cap

    @staticmethod
    async def _reject(cap: int, scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            {"error": f"request body exceeds TAI_BODY_LIMIT_MAX_BODY_BYTES ({cap} bytes)"},
            status_code=413,
            headers={"Cache-Control": "no-store"},
        )
        await response(scope, receive, send)
