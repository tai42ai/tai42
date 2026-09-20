"""Sub-MCP app routing contract — the registration + ASGI seam.

Implementations build/cache per-slug ASGI sub-apps and apply auth middleware.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel

if TYPE_CHECKING:
    from starlette.applications import Starlette

# ASGI shapes, spelled with stdlib typing only (no asgiref dependency).
Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]


class RouteConfig(BaseModel):
    """The tools and transport exposed by one registered sub-MCP app."""

    tools: list[str]
    transport: str = "http"


@runtime_checkable
class SubMcpAppRouter(Protocol):
    """Registration and ASGI-dispatch seam for per-slug sub-MCP apps."""

    @property
    def root_prefix(self) -> str:
        """The URL prefix every sub-MCP app mounts under."""
        ...

    @property
    def routes(self) -> dict[str, RouteConfig]:
        """The registered routes keyed by slug."""
        ...

    async def register_sub_mcp_app(self, slug: str, tools: list[str], transport: str = "http") -> None:
        """Register (or reload) a sub-MCP app exposing ``tools`` under ``slug``."""
        ...

    async def unregister_sub_mcp_app(self, slug: str) -> None:
        """Drop a sub-MCP app and tear down its ASGI lifespan."""
        ...

    def lifespan(self, app: Starlette) -> AbstractAsyncContextManager[None]:
        """ASGI lifespan that owns every built sub-app's exit stack."""
        ...

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI entrypoint: dispatch ``scope`` to the slug's sub-app behind auth."""
        ...


__all__ = ["Message", "Receive", "RouteConfig", "Scope", "Send", "SubMcpAppRouter"]
