"""The registry of deferred-call kinds: how a call is captured at stage and applied after the reply.

The outbox dispatches a row's calls by kind and never knows what a kind calls; a module that owns a
kind registers it once at import.
"""

from __future__ import annotations

import threading
from typing import Any, Protocol


class DeferredCallKind(Protocol):
    """How one kind of deferred call is captured and run."""

    async def capture(self, target: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """The JSON payload that runs ``target`` with ``arguments`` later, captured in the caller's context."""
        ...

    async def apply(self, payload: dict[str, Any], *, idempotency_key: str) -> None:
        """Run the captured call; ``idempotency_key`` is stable across every re-run of this call."""
        ...

    async def resumable(self, payload: dict[str, Any]) -> bool:
        """Whether the call may be re-run after a process exit interrupted it."""
        ...


_lock = threading.Lock()
_kinds: dict[str, DeferredCallKind] = {}


def register_deferred_call_kind(kind: str, handler: DeferredCallKind) -> None:
    """Register ``handler`` for ``kind``; a second registration of one kind raises ``ValueError``."""
    with _lock:
        if kind in _kinds:
            raise ValueError(f"deferred call kind {kind!r} is already registered")
        _kinds[kind] = handler


def deferred_call_kind(kind: str) -> DeferredCallKind:
    """The handler registered for ``kind``; an unknown kind raises ``LookupError``."""
    with _lock:
        handler = _kinds.get(kind)
    if handler is None:
        raise LookupError(f"no deferred call kind {kind!r} is registered")
    return handler
