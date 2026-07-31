import httpx

from tai42_kit.clients.base import PooledClient, reject_unknown_connection_kwargs


class HttpxClient(PooledClient[httpx.AsyncClient]):
    """Pooled ``httpx.AsyncClient`` for app-owned outbound HTTP.

    Connection params arrive as kwargs (``timeout``, ``max_connections``,
    ``max_keepalive_connections``, ``keepalive_expiry``); unspecified ones keep
    httpx's defaults. ``trust_env=False`` ignores ambient proxy env vars.
    """

    _LIMIT_KEYS = ("max_connections", "max_keepalive_connections", "keepalive_expiry")
    _ALLOWED_KWARGS = frozenset({"timeout", *_LIMIT_KEYS})

    async def _create(self, **kwargs) -> httpx.AsyncClient:
        reject_unknown_connection_kwargs("Httpx client", kwargs, self._ALLOWED_KWARGS)
        limits = httpx.Limits(**{k: kwargs[k] for k in self._LIMIT_KEYS if k in kwargs})
        client_kwargs = {"trust_env": False, "limits": limits}
        if "timeout" in kwargs:
            client_kwargs["timeout"] = httpx.Timeout(kwargs["timeout"])
        return httpx.AsyncClient(**client_kwargs)

    async def _close(self, client: httpx.AsyncClient):
        await client.aclose()
