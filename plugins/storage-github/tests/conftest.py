"""Bind a light fake ``tai42_app`` before the backend is imported.

``tai42_storage_github.storage`` registers ``GithubStorage`` and reaches the pooled
HTTP client via ``tai42_app`` at import time, so a fake is bound here first. The
registration decorator is a passthrough, and ``client_ctx`` yields whatever mock
client a test installs via the ``client`` fixture.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.storage import Storage


class _FakeStorageFacet:
    def __init__(self) -> None:
        self.registered: type[Storage] | None = None

    def register_storage(self, cls: type[Storage] | None = None):
        def decorator(target: type[Storage]) -> type[Storage]:
            self.registered = target
            return target

        return decorator(cls) if cls is not None else decorator


class _FakeClientsFacet:
    def __init__(self) -> None:
        self.client: object | None = None

    def client_ctx(self, client_cls: type, settings: object = None, *, fresh: bool = False, **kwargs: object):
        client = self.client

        @asynccontextmanager
        async def _ctx() -> AsyncIterator[object]:
            if client is None:
                raise RuntimeError("test must install a mock client via the `client` fixture")
            yield client

        return _ctx()


class _FakeApp:
    def __init__(self) -> None:
        self.storage = _FakeStorageFacet()
        self.clients = _FakeClientsFacet()


_fake_app = _FakeApp()
tai42_app.bind(_fake_app)


@pytest.fixture
def settings() -> SimpleNamespace:
    """Fake settings with a sentinel token, patched into the backend per test."""
    return SimpleNamespace(
        username="acme",
        repo="content",
        branch="main",
        token=SimpleNamespace(get_secret_value=lambda: "s3cr3t-token-value"),
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, settings: SimpleNamespace) -> AsyncMock:
    """An AsyncMock HTTP client yielded by ``tai42_app.clients.client_ctx``."""
    from tai42_storage_github import storage as storage_mod

    monkeypatch.setattr(storage_mod, "github_storage_settings", lambda: settings)
    mock = AsyncMock()
    _fake_app.clients.client = mock
    return mock


def make_response(*, status: int = 200, json_body: object = None, text: str = "", content: bytes | None = None):
    """A MagicMock httpx.Response; ``raise_for_status`` raises a real error on 4xx/5xx."""
    from unittest.mock import MagicMock

    from httpx import HTTPStatusError, Request, Response

    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = {} if json_body is None else json_body
    resp.text = text
    resp.content = content if content is not None else text.encode("utf-8")
    if status >= 400:
        real = Response(status, request=Request("GET", "https://example.invalid"))
        resp.raise_for_status.side_effect = HTTPStatusError("err", request=real.request, response=real)
    else:
        resp.raise_for_status.return_value = None
    return resp
