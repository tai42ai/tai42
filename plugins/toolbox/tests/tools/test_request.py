"""Tests for the ``request`` tool entrypoint.

The tool runs through tai42-kit's real curl client, so the request is exercised
against a live local server (an httpx-level mock such as respx cannot intercept
curl_cffi). The ``curl_app`` fixture binds an app whose ``clients.client_ctx``
forwards to tai42-kit's pool."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from tai42_contract.app import tai42_app

from tai42_toolbox.tools.request import request


@pytest.mark.usefixtures("curl_app")
def test_request_returns_response_data(local_server: Any) -> None:
    local_server.configure(body=b'{"ok": true}', status=200, content_type="application/json")
    result = asyncio.run(request(f"{local_server.base_url}/data"))
    assert result["status_code"] == 200
    assert result["ok"] is True
    assert "ok" in result["text"]
    assert local_server.requests == ["GET"]


@pytest.mark.usefixtures("curl_app")
def test_request_honours_the_method(local_server: Any) -> None:
    local_server.configure(body=b"created", status=201, content_type="text/plain")
    result = asyncio.run(request(f"{local_server.base_url}/items", method="post"))
    assert result["status_code"] == 201
    assert local_server.requests == ["POST"]


@pytest.mark.usefixtures("curl_app")
def test_request_reuses_a_keyed_session(local_server: Any) -> None:
    local_server.configure(body=b"ok", status=200, content_type="text/plain")

    async def two_calls() -> None:
        # First call starts with an empty jar and stores the server's Set-Cookie; the second keeps
        # the jar (no clear), so a genuinely reused session sends that cookie back.
        await request(f"{local_server.base_url}/keyed", session_key="shared")
        await request(f"{local_server.base_url}/keyed", session_key="shared", clear_session_cookies=False)

    asyncio.run(two_calls())

    assert local_server.received_cookies[0] is None
    assert local_server.received_cookies[1] is not None
    assert "sid=session-marker" in local_server.received_cookies[1]


@pytest.mark.usefixtures("curl_app")
def test_a_keyed_request_shares_its_pool_through_share_key(local_server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # The reuse key selects the pooled session as ``share_key``; ``session_params`` carry only
    # session options, so they reach ``AsyncSession`` verbatim.
    local_server.configure(body=b"ok", status=200, content_type="text/plain")
    seen: list[dict[str, Any]] = []
    clients = tai42_app.clients
    real_client_ctx = clients.client_ctx

    def spy(client_cls: Any, settings: Any = None, *, fresh: bool = False, **kwargs: Any) -> Any:
        seen.append({"fresh": fresh, **kwargs})
        return real_client_ctx(client_cls, settings, fresh=fresh, **kwargs)

    monkeypatch.setattr(clients, "client_ctx", spy)
    asyncio.run(request(f"{local_server.base_url}/keyed", session_key="shared", session_params={"timeout": 5}))
    asyncio.run(request(f"{local_server.base_url}/one-shot", session_params={"timeout": 5}))

    assert seen == [
        {"fresh": False, "session_params": {"timeout": 5}, "share_key": "shared"},
        {"fresh": True, "session_params": {"timeout": 5}},
    ]


def test_registration(load_registrations: Callable[[str], Any]) -> None:
    app = load_registrations("tai42_toolbox.tools.request")
    assert set(app.tools.registered) == {"request"}
