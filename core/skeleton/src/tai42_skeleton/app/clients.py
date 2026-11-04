"""The app's client surface — a thin delegate over the tai42-kit pooled-client framework.

Surfaces the pooling on the app: ``client_ctx(SomeClient, settings=…, fresh=…)``
yields a connected client, pooled per event loop + connection params (or a one-shot
off-pool client when ``fresh=True``). The pooling itself lives in ``tai42_kit.clients``,
and a loop's end releases its pools through ``tai42_kit.loop_bound`` — this only exposes
the client door on the app, so callers reach a client by class and never import a
concrete client here.
"""

from contextlib import AbstractAsyncContextManager
from typing import Any, cast

from tai42_contract.clients import BaseClient
from tai42_kit.clients import PooledClient
from tai42_kit.clients import client_ctx as _pool_client_ctx


class ClientsFacet:
    """``app.clients`` — concrete ``tai42_contract.app.AppClients`` facet, forwarding to the kit pool."""

    def client_ctx[ClientT](
        self,
        client_cls: type[BaseClient[ClientT]],
        settings: Any = None,
        *,
        fresh: bool = False,
        **kwargs: Any,
    ) -> AbstractAsyncContextManager[ClientT]:
        """Yield a pooled (or ``fresh``) connected client for ``client_cls``."""
        # The contract types the client class as the BaseClient interface; the kit
        # pool needs the PooledClient impl it always is at runtime (every app-owned
        # client subclasses PooledClient). This delegate is the seam that knows the
        # two are the same object.
        return _pool_client_ctx(cast("type[PooledClient[ClientT]]", client_cls), settings, fresh=fresh, **kwargs)
