"""Pooled aioboto3 S3 client.

``S3Client`` subclasses ``tai42_kit.clients.PooledClient`` — one connected client
per event loop. Its connection comes entirely from the cached ``s3_settings``
singleton, so the pool key stays empty (a single configured client per loop).

Every request the client sends runs from an empty context. The HTTP session opens its
connections, and arms their keep-alive timers, inside a request, and each keeps the context
it was created in for the connection's life, so a pooled keep-alive connection would
otherwise keep the context of the caller whose request opened it.
"""

from __future__ import annotations

import asyncio
import contextvars
from typing import Any

import aioboto3
from aiobotocore.awsrequest import AioAWSResponse
from aiobotocore.config import AioConfig
from aiobotocore.httpsession import AIOHTTPSession
from botocore.exceptions import ConnectionError as BotocoreConnectionError
from botocore.exceptions import HTTPClientError
from tai42_kit.clients.base import PooledClient, is_loop_bound_runtime_error, reject_unknown_connection_kwargs

from tai42_storage_s3.settings import s3_settings

# Connection comes entirely from settings, so no connection kwargs are accepted;
# anything passed would split the pool key and is rejected loudly.
_ALLOWED_KWARGS: frozenset[str] = frozenset()


class _EmptyContextHTTPSession(AIOHTTPSession):
    """The S3 HTTP session, sending every request from an empty context.

    The connection, its transport and its keep-alive timers are created while a request is
    sent, so none of them carries the caller's context variables.
    """

    async def send(self, request: Any) -> AioAWSResponse:
        return await asyncio.create_task(super().send(request), context=contextvars.Context())


class S3Client(PooledClient[Any]):
    """A pooled aioboto3 S3 client, one per event loop, configured from ``s3_settings``."""

    async def _create(self, **kwargs: Any) -> Any:
        reject_unknown_connection_kwargs("S3 client", kwargs, _ALLOWED_KWARGS)
        settings = s3_settings()

        endpoint = settings.endpoint
        if endpoint and not endpoint.startswith(("http://", "https://")):
            protocol = "https" if settings.secure else "http"
            endpoint = f"{protocol}://{endpoint}"

        session = aioboto3.Session()
        context = session.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=settings.access_key.get_secret_value() if settings.access_key else None,
            aws_secret_access_key=settings.secret_key.get_secret_value() if settings.secret_key else None,
            region_name=settings.region,
            verify=settings.verify_ssl,
            use_ssl=settings.secure,
            config=AioConfig(
                http_session_cls=_EmptyContextHTTPSession,
                connect_timeout=settings.connect_timeout,
                read_timeout=settings.read_timeout,
                s3={"addressing_style": settings.addressing_style},
                request_checksum_calculation=settings.request_checksum_calculation,
            ),
        )
        # Enter the aioboto3 client context and return the live client; the pool
        # closes it via ``_close``, so no per-instance state is kept.
        return await context.__aenter__()

    async def _close(self, client: Any) -> None:
        await client.close()

    def _disconnection_exceptions(self) -> tuple[type[Exception], ...]:
        # botocore signals a dead connection via its own exceptions, not builtin
        # ConnectionError. A ClientError (an API error over a healthy connection)
        # matches neither, so it never evicts the pooled client.
        return (BotocoreConnectionError, HTTPClientError)

    def _is_disconnection_error(self, exc: BaseException) -> bool:
        # The loop-bound aiohttp transport surfaces use-after-loop-close as a plain
        # RuntimeError; match those by message so an unrelated RuntimeError from
        # caller code never tears down the pool.
        return super()._is_disconnection_error(exc) or is_loop_bound_runtime_error(exc)
