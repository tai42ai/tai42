"""The ``HooksManager`` protocol — the hook registration + dispatch seam.

Implementations add jq evaluation, monitoring spans, and concurrency."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from tai42_contract.hooks.models import HookParams


@runtime_checkable
class HooksManager(Protocol):
    async def register(self, params: HookParams) -> bool:
        """Register a hook; return whether it was newly stored."""
        ...

    async def unregister(self, name: str) -> bool:
        """Drop a hook by name; return whether one was removed."""
        ...

    async def list_hooks(self) -> dict[str, HookParams]:
        """All registered hooks keyed by name."""
        ...

    async def list_hooks_by_topic(self, topic: str) -> dict[str, HookParams]:
        """Registered hooks for ``topic`` keyed by name."""
        ...

    async def on_event(self, topic: str, payload: dict[str, Any]) -> None:
        """Fire every hook whose condition matches ``payload`` for ``topic``."""
        ...


__all__ = ["HooksManager"]
