"""Poolable-client contract.

The pure interface ``AppClients.client_ctx`` is generic over: a client whose
connections are pooled per event loop + connection params. The concrete pooling
engine (``PooledClient``) and the connect/close hooks live in the implementing
layer; this names only the surface a consumer holds.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class BaseClient[T](Protocol):
    """A poolable client.

    ``current`` yields a connected client pooled per running loop + connection
    key; ``close`` drops a pooled connection for the running loop. The kit
    ``PooledClient`` implements this; tool/plugin clients subclass that.
    """

    def current(self, **kwargs: Any) -> AbstractAsyncContextManager[T]:
        """Yield a connected client, pooled per loop + connection params."""
        ...

    async def close(self, **kwargs: Any) -> None:
        """Close and drop the pooled client for the running loop, if present."""
        ...


__all__ = ["BaseClient"]
