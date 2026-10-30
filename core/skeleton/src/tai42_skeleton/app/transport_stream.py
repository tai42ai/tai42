"""The MCP transport's server-to-client stream and the sessions it carries, as a serving generation retires them.

Every transport app the platform assembles (the main streamable-HTTP and SSE apps, and each
sub-MCP mount's) carries :class:`TransportStreamDrainExemptMiddleware` beside its body cap,
built with the path its transport serves the stream on and whether the stream carries its
session's replies (the SSE transport's does). Every FastMCP server the platform builds
carries :class:`SessionCallAdmissionMiddleware`, so a call of an SSE session is counted
against the drain as a request of its own until its stream has written its reply. An SSE
app records, when it is assembled, how its live sessions are ended
(:func:`bind_sse_session_ender`); the lifespan that owns the app ends them when its
generation closes its transports.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from fastmcp.server.middleware import Middleware as McpMiddleware
from mcp.server.auth.middleware.bearer_auth import RequireAuthMiddleware
from mcp.server.sse import SseServerTransport
from starlette.routing import Mount

from tai42_skeleton.access_control.path_canon import strip_root_path
from tai42_skeleton.app.admission import (
    admit_session_call,
    mark_current_request_drain_exempt,
    track_current_stream_replies,
)

if TYPE_CHECKING:
    from fastmcp.server.middleware import CallNext, MiddlewareContext
    from starlette.applications import Starlette
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

# The attribute of an SSE app's ``state`` its session ender is recorded under.
_SSE_SESSION_ENDER = "tai42_end_sse_sessions"


class TransportStreamDrainExemptMiddleware:
    """Exempts a ``GET`` on the transport's stream path from the retire drain; every other request passes untouched.

    The transport's server-to-client stream never finishes on its own: the streamable-HTTP
    standalone stream ends when its session is terminated, and an SSE session stream ends
    when its client disconnects or when its generation's close ends the session, both of
    which a retire does after its drain. Waiting on it is pointless, so it is never counted
    against the drain budget, exactly as the interactions inbox stream exempts itself. A
    ``POST`` or ``DELETE`` on the same path is a real request and stays counted. A ``GET`` on
    the path that is not the stream (refused, or replayed with nothing to replay) answers at
    once, so releasing its slot early changes nothing.

    When the server ends an SSE session, the stream's response completes first and the
    transport's handler then returns the placeholder response it returns for every ended
    session (fastmcp's SSE handler); the ASGI server would refuse it as a message after the
    response completed. For the stream request, a message sent after the response completed
    is therefore dropped; every message before it passes unchanged.

    ``stream_path`` is the route path the transport serves the stream on, compared with the
    request's path once a mount's ``root_path`` is stripped, as the router matches it.
    ``carries_replies`` says whether the stream carries the replies to its session's calls
    (the SSE transport's session stream). Such a stream reports every body chunk to its
    reply tracker once the chunk's ``send`` has returned, and its end once its app returns
    (:func:`~tai42_skeleton.app.admission.track_current_stream_replies`), so each call of its
    session counts as answered only once its reply is written.
    """

    def __init__(self, app: ASGIApp, *, stream_path: str, carries_replies: bool) -> None:
        """Wrap the transport ``app`` whose stream is served at ``stream_path`` and may carry its session's replies."""
        self.app = app
        self.stream_path = stream_path
        self.carries_replies = carries_replies

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Exempt the transport's stream, then pass the request through."""
        if not (
            scope["type"] == "http"
            and scope["method"] == "GET"
            and strip_root_path(scope["path"], scope.get("root_path", "")) == self.stream_path
        ):
            await self.app(scope, receive, send)
            return
        mark_current_request_drain_exempt()
        replies = track_current_stream_replies() if self.carries_replies else None
        completed = False

        async def send_until_complete(message: Message) -> None:
            nonlocal completed
            if completed:
                return
            is_body = message["type"] == "http.response.body"
            if is_body and not message.get("more_body", False):
                completed = True
            await send(message)
            if is_body and replies is not None:
                replies.wrote(message.get("body", b""))

        try:
            await self.app(scope, receive, send_until_complete)
        finally:
            if replies is not None:
                replies.end()


class SessionCallAdmissionMiddleware(McpMiddleware):
    """Counts each call of a session served on a long-lived stream (SSE) as a request of its own.

    A call here is a request that reaches the server's middleware: a tool, resource or
    prompt request. The call is admitted on its generation when it enters the middleware
    and released once its task has ended and its session stream has written its reply or
    ended (:func:`~tai42_skeleton.app.admission.admit_session_call`). Requests the session
    answers without the middleware (``ping``, ``logging/setLevel``) are not counted. A streamable-HTTP call is
    counted by its own ``POST`` and admits nothing here. ``initialize`` is answered inline
    by the session's own receive loop, before that loop reads its next message, so the
    session's end cannot overtake it and it is not admitted.
    """

    async def on_request(self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]) -> Any:
        """Admit the call (when it runs on a long-lived stream), then run it."""
        if context.method != "initialize":
            admit_session_call(_request_id(context))
        return await call_next(context)


def _request_id(context: MiddlewareContext[Any]) -> object | None:
    """The JSON-RPC id of the request ``context`` runs, as the MCP SDK request context holds it, or ``None``."""
    fastmcp_context = context.fastmcp_context
    request_context = fastmcp_context.request_context if fastmcp_context is not None else None
    return request_context.request_id if request_context is not None else None


def _sse_transport(app: Starlette, message_path: str) -> SseServerTransport:
    """The MCP SDK transport behind ``app``'s message-path mount, as fastmcp's ``create_sse_app`` assembles it.

    Raises, naming the fact, when the mount is missing or does not serve the SDK transport's
    message endpoint with its session table.
    """
    path = message_path.rstrip("/")
    for route in app.routes:
        if not (isinstance(route, Mount) and route.path == path):
            continue
        endpoint = route.app
        if isinstance(endpoint, RequireAuthMiddleware):
            endpoint = endpoint.app
        transport = getattr(endpoint, "__self__", None)
        if (
            isinstance(transport, SseServerTransport)
            and getattr(endpoint, "__func__", None) is SseServerTransport.handle_post_message
            and isinstance(getattr(transport, "_read_stream_writers", None), dict)
        ):
            return transport
        raise RuntimeError(
            f"the SSE app's message mount {path!r} does not serve the MCP SDK's "
            "SseServerTransport.handle_post_message with its session table; its sessions cannot be ended"
        )
    raise RuntimeError(f"the SSE app has no message mount at {path!r}; its sessions cannot be ended")


def bind_sse_session_ender(app: Starlette, message_path: str) -> None:
    """Record on the SSE ``app`` how to end every session it serves; raises when the app's layout is not the pinned one.

    The legacy HTTP+SSE transport has no session-termination message: a session ends when
    its stream ends. The MCP SDK exposes no call to end one, so the ender reads the session
    table of the SDK transport behind the message-path mount fastmcp registers, and closes
    each session's server-side read stream. The SDK then ends the session its own way: its
    server loop ends, its write side closes behind every reply already handed to it, the SSE
    response completes and the transport drops the session. A request still pending on the
    session is answered by the client's own "connection closed" error.
    """
    transport = _sse_transport(app, message_path)

    async def end_sessions() -> None:
        # The SDK transport's own session table: no public call ends a session.
        for writer in list(transport._read_stream_writers.values()):
            await writer.aclose()

    setattr(app.state, _SSE_SESSION_ENDER, end_sessions)


def transport_session_ender(app: object) -> Callable[[], Awaitable[None]] | None:
    """The session ender :func:`bind_sse_session_ender` recorded on ``app``, or ``None`` when it recorded none."""
    state = getattr(app, "state", None)
    if state is None:
        return None
    return getattr(state, _SSE_SESSION_ENDER, None)
