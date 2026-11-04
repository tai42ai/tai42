"""MCP liveness probe over a faked pooled FastMCP client."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from tai42_contract.connectors.providers import McpServerDescriptor, SubServiceDescriptor

import tai42_skeleton.connectors.runtime.probe as probe_mod
from tai42_skeleton.connectors.runtime.probe import probe
from tai42_skeleton.connectors.runtime.resolver import ManagedAuth

from .conftest import make_noauth_http_descriptor, make_noauth_stdio_descriptor, make_oauth_descriptor


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
    assert await probe(desc, "mail", auth=ManagedAuth(access_token="at")) is True


async def test_probe_unreachable_returns_false(install_client):
    install_client(error=RuntimeError("connect failed"))
    desc = make_oauth_descriptor()
    assert await probe(desc, "mail", auth=ManagedAuth(access_token="at")) is False


async def test_probe_stdio_noauth_env_merges_over_the_descriptor_env(install_client):
    captured = install_client(tools=[])
    desc = make_noauth_stdio_descriptor().model_copy(
        update={
            "sub_services": {
                "search": SubServiceDescriptor(
                    id="search",
                    display_name="Search",
                    mcp_server=McpServerDescriptor(
                        type="stdio", command="tai-mcp-widgets", env={"api_key": "static", "region": "eu"}
                    ),
                )
            }
        }
    )
    assert await probe(desc, "search", auth=ManagedAuth(env={"api_key": "k"})) is True
    cfg = captured["kwargs"]["config"]
    assert cfg["config"]["type"] == "stdio"
    assert cfg["config"]["env"] == {"api_key": "k", "region": "eu"}


async def test_probe_http_oauth_carries_a_lower_cased_bearer_header(install_client):
    captured = install_client(tools=[])
    desc = make_oauth_descriptor()
    await probe(desc, "mail", auth=ManagedAuth(access_token="my-token"))
    cfg = captured["kwargs"]["config"]
    assert cfg["config"]["headers"]["authorization"] == "Bearer my-token"
    assert "Authorization" not in cfg["config"]["headers"]


async def test_probe_without_auth_injects_nothing(install_client):
    captured = install_client(tools=[])
    desc = make_oauth_descriptor()
    await probe(desc, "mail")
    cfg = captured["kwargs"]["config"]
    assert not cfg["config"]["headers"]


async def test_probe_http_noauth_header_differing_in_case_collapses_to_one(install_client):
    """A client header whose name differs only in case from a descriptor header replaces it: one header, lower-cased."""
    captured = install_client(tools=[])
    desc = make_noauth_http_descriptor().model_copy(
        update={
            "sub_services": {
                "main": SubServiceDescriptor(
                    id="main",
                    display_name="Main",
                    mcp_server=McpServerDescriptor(
                        type="http", url="https://httpsvc.test/mcp", extra_headers={"X-Token": "static"}
                    ),
                )
            }
        }
    )
    await probe(desc, "main", auth=ManagedAuth(headers={"x-TOKEN": "client"}))
    cfg = captured["kwargs"]["config"]
    assert cfg["config"]["headers"] == {"x-token": "client"}


@pytest.mark.usefixtures("serving_here")
async def test_a_probe_that_stops_waiting_leaves_the_start_running_to_completion(monkeypatch, caplog):
    import asyncio
    import logging
    import threading

    from tai42_skeleton.tools import mcp_start

    cancelled, finished = threading.Event(), threading.Event()

    class _SlowClient(_FakeMcpClient):
        async def list_tools(self):
            try:
                await asyncio.sleep(0.4)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            finished.set()
            return []

    @asynccontextmanager
    async def slow_client_ctx(client_cls, **kwargs):
        yield _SlowClient()

    monkeypatch.setattr(probe_mod, "client_ctx", slow_client_ctx)
    monkeypatch.setattr(probe_mod, "_PROBE_WAIT_SECONDS", 0.05)
    try:
        with caplog.at_level(logging.INFO, logger=probe_mod.__name__):
            assert await probe(make_oauth_descriptor(), "mail", auth=ManagedAuth(access_token="at")) is False
            assert await asyncio.to_thread(finished.wait, 5)
            for _ in range(100):
                if any("finished after its wait" in r.getMessage() for r in caplog.records):
                    break
                await asyncio.sleep(0.01)
        assert not cancelled.is_set()
        [late] = [r.getMessage() for r in caplog.records if "finished after its wait" in r.getMessage()]
        assert late == f"connectors: probe of {make_oauth_descriptor().id}/mail finished after its wait: listed"
    finally:
        await mcp_start.aclose()
