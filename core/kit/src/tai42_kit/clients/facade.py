"""The generic client facade — one way to get any pooled client.

``client_ctx(SomeClient, settings)`` returns an async context manager yielding a
connected client, pooled and reused per event-loop + connection params (the
pooling lives in ``PooledClient``). A client is a plain ``PooledClient`` subclass,
and callers reach it by class — no container, no wiring.

- ``settings`` (a ``ClientSettings``) supplies connection kwargs via
  ``client_kwargs()``; or pass raw ``**kwargs`` for clients with none.
- ``fresh=True`` yields a one-shot client built outside the pool and closed on
  exit (for a blocking op that must not hold a shared-pool connection).
"""

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from tai42_kit.clients.base import PooledClient
from tai42_kit.clients.settings import ClientSettings


def client_ctx[T](
    client_cls: type[PooledClient[T]],
    settings: ClientSettings | None = None,
    *,
    fresh: bool = False,
    **kwargs: Any,
) -> AbstractAsyncContextManager[T]:
    if settings is not None and kwargs:
        raise ValueError("client_ctx: pass either `settings` or **kwargs, not both")
    kw = settings.client_kwargs() if settings is not None else kwargs
    if fresh:
        return _fresh_client(client_cls, kw)
    return client_cls().current(**kw)


@asynccontextmanager
async def _fresh_client[T](client_cls: type[PooledClient[T]], kw: dict) -> AsyncIterator[T]:
    inst = client_cls()
    client = await inst._create(**kw)
    try:
        yield client
    finally:
        await inst._close(client)
