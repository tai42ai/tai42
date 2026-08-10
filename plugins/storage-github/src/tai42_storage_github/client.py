"""Pooled ``httpx.AsyncClient`` for the GitHub storage backend, per event loop.

``trust_env=False`` ignores ambient proxy env vars; connection limits and timeout
come from :func:`~tai42_storage_github.settings.github_storage_settings`.
"""

from __future__ import annotations

import httpx
from tai42_kit.clients import PooledClient

from tai42_storage_github.settings import github_storage_settings


class GithubHttpxClient(PooledClient[httpx.AsyncClient]):
    async def _create(self, **kwargs: object) -> httpx.AsyncClient:
        settings = github_storage_settings()
        return httpx.AsyncClient(
            trust_env=False,
            timeout=httpx.Timeout(settings.timeout_total),
            limits=httpx.Limits(
                max_connections=settings.max_connections,
                max_keepalive_connections=settings.max_keepalive_connections,
                keepalive_expiry=settings.keepalive_expiry,
            ),
        )

    async def _close(self, client: httpx.AsyncClient) -> None:
        await client.aclose()
