"""Client-manager coverage: the one HTTP read client, built once from the project."""

from __future__ import annotations

import base64

import httpx

from tai42_monitoring_langfuse.client_manager import LangfuseClientManager
from tai42_monitoring_langfuse.project import LangfuseProject


def _cfg(source: str = "tai", timeout_seconds: int = 30) -> LangfuseProject:
    return LangfuseProject(
        public_key="pk",
        secret_key="sk",
        host="http://langfuse.example:3000",
        source=source,
        timeout_seconds=timeout_seconds,
    )


def test_builds_one_read_client_from_the_project() -> None:
    mgr = LangfuseClientManager(_cfg(timeout_seconds=7))
    client = mgr.active_client()
    try:
        assert mgr.active_client() is client
        assert client.base_url == httpx.URL("http://langfuse.example:3000")
        assert client.timeout == httpx.Timeout(7)
        request = client.build_request("GET", "/api/public/v2/observations")
        assert request.url == httpx.URL("http://langfuse.example:3000/api/public/v2/observations")
        assert isinstance(client.auth, httpx.BasicAuth)
        signed = next(client.auth.auth_flow(request))
        assert signed.headers["authorization"] == "Basic " + base64.b64encode(b"pk:sk").decode()
    finally:
        client.close()


def test_the_source_comes_from_the_project() -> None:
    assert LangfuseClientManager(_cfg(source="staging")).active_source() == "staging"
