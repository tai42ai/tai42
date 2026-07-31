"""Bind a light stub app before the backend is imported.

``tai42_storage_s3.storage`` registers ``S3Storage`` and reaches its S3 client via
``tai42_app`` at import time, so the stub is bound here first. Tests drive the
client by setting ``stub_clients.client`` to a mock (see the ``s3_client`` fixture).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from tai42_contract.app import tai42_app


class _ClientCtx:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def __aenter__(self) -> Any:
        return self._client

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _StubClients:
    def __init__(self) -> None:
        self.client: Any = None

    def client_ctx(self, client_cls: Any, settings: Any = None, **kwargs: Any) -> _ClientCtx:
        if self.client is None:
            raise RuntimeError("test must set stub_clients.client before using the S3 backend")
        return _ClientCtx(self.client)


class _StubStorage:
    def __init__(self) -> None:
        self.registered: Any = None

    def register_storage(self, cls: Any = None) -> Any:
        def decorator(c: Any) -> Any:
            self.registered = c
            return c

        return decorator(cls) if cls is not None else decorator


class _StubApp:
    def __init__(self) -> None:
        self.storage = _StubStorage()
        self.clients = _StubClients()


_stub_app = _StubApp()
tai42_app.bind(_stub_app)


@pytest.fixture
def s3_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A mock S3 client wired into ``client_ctx`` with ``s3_settings`` stubbed."""
    from tai42_storage_s3 import storage as storage_module

    client = AsyncMock()
    # get_paginator is a synchronous boto call; only the page iteration awaits.
    client.get_paginator = MagicMock()

    _stub_app.clients.client = client
    monkeypatch.setattr(storage_module, "s3_settings", lambda: SimpleNamespace(bucket="b"))
    try:
        yield client
    finally:
        _stub_app.clients.client = None


class FakeBody:
    """Stand-in for a streamed S3 response body (async context manager + read)."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aenter__(self) -> FakeBody:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def read(self) -> bytes:
        return self._data


class FakePaginator:
    """Records ``paginate`` kwargs and yields the configured pages asynchronously."""

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages
        self.paginate_kwargs: dict[str, Any] | None = None

    def paginate(self, **kwargs: Any) -> Any:
        self.paginate_kwargs = kwargs

        async def _gen() -> Any:
            for page in self._pages:
                yield page

        return _gen()


def not_found_error(operation: str) -> Any:
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, operation)


def forbidden_error(operation: str) -> Any:
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": "403", "Message": "Forbidden"}}, operation)
