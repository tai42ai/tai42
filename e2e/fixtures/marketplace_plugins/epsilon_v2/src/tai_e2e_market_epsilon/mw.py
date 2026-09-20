"""The epsilon fixture's response-header middleware.

Registers a no-op ASGI middleware at import via ``@tai42_app.http.middleware``, so
the skeleton installer's manifest patch persists this module into
``middlewares_modules`` and a process restart wraps it into the served ASGI app
(the middleware stack is applied when the app is built, so it activates on restart
like the router). It stamps a distinctive header onto every HTTP response, giving a
test an observable signal that the merged middleware is live."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from tai42_contract.app import tai42_app

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]


@tai42_app.http.middleware
class EpsilonHeaderMiddleware:
    """Stamp ``x-e2e-epsilon: 1`` onto every HTTP response, passing everything else
    through unchanged."""

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((b"x-e2e-epsilon", b"1"))
                message["headers"] = headers
            await send(message)

        await self._app(scope, receive, send_with_header)
