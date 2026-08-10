"""Every concrete pooled client satisfies the contract ``BaseClient`` Protocol.

Each impl imports its driver at module top, so a client whose extra is not
installed is skipped (mcp/http ride on base deps and always run)."""

import importlib

import pytest
from tai42_contract.clients import BaseClient

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
