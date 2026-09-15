"""The ``Verifier`` protocol — identity verification + route-id resolution.

Implementations subclass fastmcp ``TokenVerifier`` and add redis/route caching.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from fastmcp.server.auth import AccessToken


@runtime_checkable
class Verifier(Protocol):
    """Protocol for verifying identity tokens and resolving a request path to its resource ids."""

    async def verify_token(self, token: str) -> AccessToken | None:
        """Validate ``token`` to a pure-identity ``AccessToken``, or ``None`` when invalid.

        Scopes are injected later by the policy layer.
        """
        ...

    async def resolve_resource_ids(self, path: str) -> list[str]:
        """Resolve a request ``path`` to the resource ids that protect it.

        Covers exact, auto-normalized, and pattern matches.
        """
        ...


__all__ = ["Verifier"]
