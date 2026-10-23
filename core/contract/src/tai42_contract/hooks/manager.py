"""The ``HooksManager`` protocol — the hook registration + dispatch seam.

Implementations add jq evaluation, monitoring spans, and concurrency.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from tai42_contract.hooks.models import HookParams


@runtime_checkable
class HooksManager(Protocol):
    """Registration and dispatch seam for event hooks."""

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
        """Keep one run per hook on ``topic`` for ``payload``, then return; the runs happen asynchronously.

        The hook acts when its visit cancels, commits a resume or take claim, or starts the tool. A
        run that fails or is interrupted before its hook acts is retried with backoff, up to the
        configured number of attempts; then, or at once for a failure no retry can change, it is held
        as failed and reported. After the hook acts, the run is never repeated automatically, with one
        exception: a run interrupted after it started a tool that declares ``TOOL_META_CRASH_RESUME``
        replays that start alone, with the same tool input, extras and idempotency key. Every attempt,
        a replayed start included, counts toward the configured number of attempts, so no run is
        repeated without end. Every other failure or interruption after the hook acts is held as
        failed and reported. A run stopped while its hook was making a resume or take claim is retried
        only when the entry is still open, which shows the claim did not commit; when the entry was
        resolved by then, the run is held as failed, because the platform cannot tell whether this
        run's claim or another resolution resolved it. An operator can retry a failed run: a run whose
        hook started a tool replays that start alone, and any other run runs the hook again as a fresh
        run. The exception is a run whose hook is known to have committed a resume or a take. It can
        only be discarded: the interactions reaper redelivers a claimed resume, and a taken outcome is
        consumed.
        """
        ...


__all__ = ["HooksManager"]
