"""The ``PolicyEnforcer`` protocol — fetch a user's policy + live context and
enforce a jq expression. Implementations add redis fetch + lru caching."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from tai42_contract.access_control.models import AccessPolicy


@runtime_checkable
class PolicyEnforcer(Protocol):
    async def get_policy(self, user_id: str) -> AccessPolicy:
        """Fetch policy (scopes + rules) for a specific user."""
        ...

    async def get_live_context(self, user_id: str) -> dict[str, Any]:
        """Fetch dynamic context (usage, counters) — always fresh, never cached,
        so enforcement is based on live data."""
        ...

    async def get_auth_data(self, user_id: str) -> tuple[AccessPolicy, dict[str, Any]]:
        """Return ``(policy, live_context)`` for ``user_id``."""
        ...

    def enforce(self, context: dict[str, Any], expression: str | None, *, condition_configured: bool = False) -> None:
        """Evaluate the jq ``expression`` against ``context``; raise on violation
        (never silently allow).

        ``condition_configured`` distinguishes "no policy condition is set" from
        "a condition is set but rendered to an empty string": when a condition WAS
        configured (``condition_configured=True``) yet ``expression`` is
        falsy/empty, this DENIES (raises) rather than allowing — an empty render
        must never fail open. Only a genuinely-absent condition
        (``condition_configured=False``, the default) is a no-op that allows."""
        ...


__all__ = ["PolicyEnforcer"]
