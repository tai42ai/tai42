"""The MCP transport's server-to-client stream, exempt from its generation's retire drain.

Every transport app the platform assembles (the main streamable-HTTP and SSE apps, and each
sub-MCP mount's) carries :class:`TransportStreamDrainExemptMiddleware` beside its body cap,
built with the path its transport serves the stream on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tai42_skeleton.access_control.path_canon import strip_root_path
from tai42_skeleton.app.admission import mark_current_request_drain_exempt

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send


class TransportStreamDrainExemptMiddleware:
    """Exempts a ``GET`` on the transport's stream path from the retire drain; every other request passes untouched.

    The transport's server-to-client stream never finishes on its own: the streamable-HTTP
    standalone stream ends when its session is terminated, which a retire does after its
    drain, and an SSE session stream ends when its client disconnects (closing the transports
    does not sever it). Waiting on it is pointless, so it is never counted against the drain
    budget, exactly as the interactions inbox stream exempts itself. A ``POST`` or ``DELETE``
    on the same path is a real request and stays counted. A ``GET`` on the path that is not
    the stream (refused, or replayed with nothing to replay) answers at once, so releasing its
    slot early changes nothing.

    ``stream_path`` is the route path the transport serves the stream on, compared with the
    request's path once a mount's ``root_path`` is stripped, as the router matches it.
    """

    def __init__(self, app: ASGIApp, *, stream_path: str) -> None:
        """Wrap the transport ``app`` whose stream is served at ``stream_path``."""
        self.app = app
        self.stream_path = stream_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Exempt the transport's stream, then pass the request through."""
        if (
            scope["type"] == "http"
            and scope["method"] == "GET"
            and strip_root_path(scope["path"], scope.get("root_path", "")) == self.stream_path
        ):
            mark_current_request_drain_exempt()
        await self.app(scope, receive, send)
