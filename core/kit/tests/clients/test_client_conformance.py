"""Every concrete pooled client satisfies the contract ``BaseClient`` Protocol.

Each impl imports its driver at module top, so a client whose extra is not
installed is skipped (mcp/http ride on base deps and always run)."""

import importlib
import inspect

import pytest
from tai42_contract.clients import BaseClient

from tai42_kit.clients.impl import mcp as mcp_mod

CASES = [
    ("tai42_kit.clients.impl.http", "HttpxClient", "httpx"),
    ("tai42_kit.clients.impl.mcp", "FastMCPClient", "fastmcp"),
    ("tai42_kit.clients.impl.redis", "RedisClient", "redis"),
    ("tai42_kit.clients.impl.redis", "SyncRedisClient", "redis"),
    ("tai42_kit.clients.impl.curl", "CurlClient", "curl_cffi"),
    ("tai42_kit.clients.impl.postgres", "PostgresClient", "psycopg_pool"),
]


@pytest.mark.parametrize(("module", "cls_name", "driver"), CASES, ids=[c[1] for c in CASES])
def test_client_satisfies_base_client_protocol(module, cls_name, driver):
    pytest.importorskip(driver)
    cls = getattr(importlib.import_module(module), cls_name)
    assert isinstance(cls(), BaseClient)


def test_fastmcp_not_connected_message_is_still_the_one_we_match():
    # FastMCPClient evicts a dead pooled client by matching fastmcp's own
    # "Client is not connected" RuntimeError from the session property. Exercise
    # the real property on an unconnected client so a fastmcp wording change fails
    # here loudly instead of leaving a corpse pooled forever.
    from fastmcp import Client

    client = Client({"mcpServers": {"srv": {"url": "http://host/mcp"}}})
    with pytest.raises(RuntimeError) as excinfo:
        _ = client.session
    assert mcp_mod.FastMCPClient()._is_disconnection_error(excinfo.value) is True


def test_fastmcp_dead_session_markers_present_in_installed_source():
    # The dead-session markers couple to literal strings fastmcp raises when its
    # background session task collapses. Pin them against the installed fastmcp
    # source so an upgrade that reworded any one fails here instead of silently
    # defeating the eviction predicate.
    from fastmcp.client import client as fastmcp_client

    source = inspect.getsource(fastmcp_client)
    for marker in mcp_mod._DEAD_SESSION_ERROR_MARKERS:
        assert marker in source, f"fastmcp no longer raises {marker!r}; update the eviction markers"


def test_mcp_session_terminated_shape_present_in_installed_source():
    # The session-terminated predicate couples to the exact ErrorData the mcp SDK
    # emits when a POST hits a stale session id (HTTP 404 -> _send_session_terminated_error).
    # Pin that raise site against the installed mcp source so an upgrade that
    # changes the code or message fails here instead of silently defeating eviction.
    from mcp.client import streamable_http

    source = inspect.getsource(streamable_http.StreamableHTTPTransport._send_session_terminated_error)
    assert f"code={mcp_mod._SESSION_TERMINATED_CODE}" in source, (
        f"mcp no longer raises code={mcp_mod._SESSION_TERMINATED_CODE}; update the session-terminated predicate"
    )
    assert f'message="{mcp_mod._SESSION_TERMINATED_MESSAGE}"' in source, (
        f"mcp no longer raises message {mcp_mod._SESSION_TERMINATED_MESSAGE!r}; update the session-terminated predicate"
    )
