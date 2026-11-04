"""The HTTP edge of the writer's own exit: every route's change settles the exit it owes after its reply.

Each HTTP request runs under a writer-exit scope (:mod:`~tai42_skeleton.app.writer_exit`): a
change the request makes whose recycle roll arms the writer's own restart owes that exit into it,
whatever route or handler made the change — an operation route, a custom route, an error reply
rendered by an exception handler. Once the inner app returned and its reply's last body chunk was
sent, the exit runs: the worker's graceful stop then lets this reply, and every other in-flight
one, finish. A reply that was never sent (the inner app raised, or the request was cancelled)
abandons the exit, ERROR-logged, so the writer stays a worker awaiting a respawn. A failed exit is
ERROR-logged with its traceback; the reply it follows is already sent.

It sits in the base app's own middleware stack, ahead of every other base entry, so every reply
the stack sends passes through it.
"""

from __future__ import annotations

import logging

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tai42_skeleton.app.writer_exit import writer_exit_scope

logger = logging.getLogger(__name__)


class WriterExitMiddleware:
    """Run the writer's exit a request's change owes once that request's reply is sent."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve the request under a writer-exit scope, then settle it once the reply was sent."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        replied = False

        async def send_tracking_reply(message: Message) -> None:
            nonlocal replied
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                replied = True

        with writer_exit_scope() as exits:
            await self.app(scope, receive, send_tracking_reply)
            if not replied:
                exits.abandon("the request's reply was never sent")
                return
            try:
                await exits.settle()
            except Exception:
                logger.exception("writer exit: the writer's own restart after its reply failed")
