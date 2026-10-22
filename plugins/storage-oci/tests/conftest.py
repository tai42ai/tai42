"""Bind a light stub app before the backend is imported.

``tai42_storage_oci.storage`` registers ``OciStorage`` and reaches its client via
``tai42_app`` at import time, so the stub is bound here first. Tests drive the
client by setting ``stub_clients.client`` to a mock (see the ``oci_client``
fixture).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from oci.exceptions import ServiceError
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
            raise RuntimeError("test must set stub_clients.client before using the OCI backend")
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


def service_error(status: int, code: str = "ObjectNotFound") -> ServiceError:
    return ServiceError(status, code, {}, f"{code} ({status})")


def not_found_error() -> ServiceError:
    return service_error(404)


def forbidden_error() -> ServiceError:
    return service_error(403, "NotAuthorized")


@pytest.fixture
def oci_client() -> Iterator[Any]:
    client = AsyncMock()
    _stub_app.clients.client = client
    yield client
    _stub_app.clients.client = None


@pytest.fixture(autouse=True)
def _configured_bucket(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from tai42_storage_oci import settings as settings_module

    monkeypatch.setenv("STORAGE_OCI_BUCKET", "tenant-bucket")
    settings_module.oci_settings.cache_clear()
    yield
    settings_module.oci_settings.cache_clear()
