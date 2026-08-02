"""MCP liveness probe over a faked pooled FastMCP client."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import tai42_skeleton.connectors.runtime.probe as probe_mod
from tai42_skeleton.connectors.runtime.probe import probe

from .conftest import make_noauth_stdio_descriptor, make_oauth_descriptor


class _FakeMcpClient:
    def __init__(self, *, tools=None, error: Exception | None = None) -> None:
        self._tools = tools or []
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def list_tools(self):
        if self._error is not None:
            raise self._error
        return self._tools


@pytest.fixture
def install_client(monkeypatch):
    captured = {}

    def _install(*, tools=None, error=None):
        client = _FakeMcpClient(tools=tools, error=error)

        @asynccontextmanager
        async def fake_client_ctx(client_cls, **kwargs):
            captured["kwargs"] = kwargs
            yield client

        monkeypatch.setattr(probe_mod, "client_ctx", fake_client_ctx)
        return captured

    return _install


# -- probe -------------------------------------------------------------------


async def test_probe_unknown_sub_service_is_false(install_client):
    desc = make_oauth_descriptor()
    assert await probe(desc, "nonexistent") is False


async def test_probe_live_returns_true(install_client):
    install_client(tools=[SimpleNamespace(name="t", description="d")])
    desc = make_oauth_descriptor()
    assert await probe(desc, "mail", access_token="at") is True


async def test_probe_unreachable_returns_false(install_client):
    install_client(error=RuntimeError("connect failed"))
    desc = make_oauth_descriptor()
    assert await probe(desc, "mail", access_token="at") is False


async def test_probe_stdio_builds_env_from_config_values(install_client):
    captured = install_client(tools=[])
    desc = make_noauth_stdio_descriptor()
    assert await probe(desc, "search", config_values={"api_key": "k"}) is True
    cfg = captured["kwargs"]["config"]
    assert cfg["config"]["type"] == "stdio"
    assert cfg["config"]["env"]["api_key"] == "k"


async def test_probe_http_sets_bearer_header(install_client):
    captured = install_client(tools=[])
    desc = make_oauth_descriptor()
    await probe(desc, "mail", access_token="my-token")
    cfg = captured["kwargs"]["config"]
    assert cfg["config"]["headers"]["Authorization"] == "Bearer my-token"
