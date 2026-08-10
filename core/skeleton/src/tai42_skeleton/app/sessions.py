"""Active-MCP-session registry and the ``list_changed`` broadcast primitive.

FastMCP (3.4.2) exposes NO "notify all connected sessions" helper and NO
``on_disconnect`` hook: its only list-changed emit paths are per-session
(``session.send_tool_list_changed()`` and friends). A manifest reload runs
OUTSIDE any session, so there is nothing to call — the broadcast machinery is
net-new skeleton infrastructure built here on top of the per-session emit.

The registry captures live sessions through :class:`SessionTrackingMiddleware`
(which sees every incoming message) and holds them WEAKLY, so a collected
session drops out on its own; it additionally prunes any session whose send
raises. Membership is thus self-healing and never depends on a disconnect
callback that does not exist.
"""

import logging
import threading
import weakref
from typing import TYPE_CHECKING, Any

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext

from tai42_skeleton.app.reload_gate import REJECT_MESSAGE, ReloadGate

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# SINGULAR store-kind -> the per-session FastMCP send method (plural MCP
# notification namespace). Only the kinds with a live list to invalidate appear
# (``tool`` is not a store kind; ``preset`` / ``ac_policy`` do not emit). A kind
# with no entry is a programming error and raises loudly — matching the
# raise-on-missing pattern used by the extension-kind property dicts.
_KIND_SEND_METHOD: dict[str, str] = {
    "tool": "send_tool_list_changed",
    "prompt": "send_prompt_list_changed",
    "resource": "send_resource_list_changed",
}


class SessionRegistry:
    """Skeleton-owned registry of active MCP sessions + the ``list_changed``
    broadcast helper FastMCP does not provide."""

    def __init__(self) -> None:
        # The active sessions, weakly held so a collected session falls out with no
        # disconnect hook. Broadcasts run on the sessions' own serving loop, so only
        # the session identity is tracked.
        self._sessions: weakref.WeakSet[Any] = weakref.WeakSet()
        # ``WeakSet`` is not thread-safe, so a NON-REENTRANT lock guards every
        # ``_sessions`` read/write. It is held ONLY for quick synchronous work
        # (snapshot the list; discard a session), NEVER across an ``await`` —
        # wrapping the awaited per-session sends would hold a lock across an await.
        # Mirrors the discipline in ``SubMcpAppRouter``.
        self._lock = threading.Lock()

    def track(self, session: Any) -> None:
        """Record ``session`` as active. Idempotent — re-tracking a known session
        is a cheap set add."""
        with self._lock:
            self._sessions.add(session)

    def active_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    @staticmethod
    def _send_method(kind: str) -> str:
        try:
            return _KIND_SEND_METHOD[kind]
        except KeyError:
            raise ValueError(
                f"Unknown list_changed kind {kind!r}; expected one of {sorted(_KIND_SEND_METHOD)}."
            ) from None

    async def emit_list_changed(self, kind: str) -> None:
        """Broadcast ``{kind}s/list_changed`` to every active session.

        ``kind`` is SINGULAR (``tool`` / ``prompt`` / ``resource``) and is mapped
        to the plural MCP notification namespace internally. Per-session
        prune-and-continue: a send that raises marks that session dead — it is
        pruned (logged, a visible recovery, never a silent swallow) and the
        broadcast reaches the remaining sessions; one dead session never aborts
        the whole broadcast. Call this from a coroutine running on the sessions'
        own loop (the in-process registration-mutation path)."""
        method = self._send_method(kind)
        # Snapshot under the lock (quick dict work), then await each send OFF the
        # lock so a slow/awaiting send never holds it.
        with self._lock:
            sessions = list(self._sessions)
        for session in sessions:
            await self._send_one(session, method)

    async def _send_one(self, session: Any, method: str) -> None:
        try:
            await getattr(session, method)()
        except Exception:
            logger.warning(
                "list_changed broadcast: pruning session after send failure",
                exc_info=True,
            )
            with self._lock:
                self._sessions.discard(session)


class SessionTrackingMiddleware(Middleware):
    """Register the calling session into the :class:`SessionRegistry` on every
    incoming message — the skeleton's stand-in for the on-connect hook FastMCP
    does not expose. Added once at construction and never re-added (the server
    object outlives every reload)."""

    def __init__(self, registry: SessionRegistry) -> None:
        self._registry = registry

    async def on_message(
        self,
        context: MiddlewareContext[Any],
        call_next: "Callable[[MiddlewareContext[Any]], Awaitable[Any]]",
    ) -> Any:
        ctx = context.fastmcp_context
        if ctx is not None:
            try:
                session = ctx.session
            except Exception:
                # No established MCP session on this message yet — normal during the
                # handshake, before the session exists (``ctx.session`` raises). There
                # is nothing to track; the session is registered on a later message
                # once established, so this is a transient, not a failure.
                session = None
            if session is not None:
                try:
                    self._registry.track(session)
                except Exception:
                    # An established session that cannot be tracked (e.g. not
                    # weak-referenceable) will miss every future list_changed
                    # broadcast — a real capability loss, so log it LOUDLY rather
                    # than hiding it.
                    logger.warning("session tracking failed — client will miss list_changed", exc_info=True)
        return await call_next(context)


class ReloadRejectionMiddleware(Middleware):
    """Reject an MCP session tool call while a config reload holds the reload gate.

    A session ``tools/call`` is a run surface exactly like ``POST /api/run-tool``,
    so it inherits the same retriable rejection: while the gate is held the call
    raises a :class:`~fastmcp.exceptions.ToolError` carrying the shared reloading
    message (client-visible), rather than dispatching against registries a reload
    is tearing down and rebuilding. Registered once at construction alongside
    :class:`SessionTrackingMiddleware`."""

    def __init__(self, gate: ReloadGate) -> None:
        self._gate = gate

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: "Callable[[MiddlewareContext[Any]], Awaitable[Any]]",
    ) -> Any:
        if self._gate.locked:
            raise ToolError(REJECT_MESSAGE)
        return await call_next(context)
