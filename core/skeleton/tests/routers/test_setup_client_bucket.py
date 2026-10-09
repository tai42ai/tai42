"""The setup door's throttle keys on the kit's client bucket, under the declared proxy trust."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from starlette.requests import Request
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.routers import setup as setup_router


def _request(*, client: tuple[str, int] | None, headers: dict[str, str] | None = None) -> Request:
    payload = json.dumps({"setup_token": "t", "owner_display_name": "Owner"}).encode()
    scope: dict[str, Any] = {
        "type": "http",
        "method": "POST",
        "path": "/api/setup",
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": client,
    }

    async def _receive() -> dict[str, Any]:
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(scope, _receive)


@pytest.fixture
def trusted_proxy(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TAI_RATE_LIMIT_TRUSTED_PROXIES", '["198.51.100.7"]')
    reset_all_settings()
    yield
    monkeypatch.delenv("TAI_RATE_LIMIT_TRUSTED_PROXIES")
    reset_all_settings()


async def _bucket(request: Request) -> str:
    return (await setup_router._extract_setup(request))["client_ip"]


async def test_an_ipv4_peer_keeps_its_address() -> None:
    assert await _bucket(_request(client=("198.51.100.7", 1))) == "198.51.100.7"


async def test_ipv6_peers_in_one_slash64_share_a_bucket() -> None:
    first = await _bucket(_request(client=("2001:db8:1:2::10", 1)))
    second = await _bucket(_request(client=("2001:db8:1:2:ffff::20", 1)))
    assert first == second == "2001:db8:1:2::"


async def test_no_peer_is_the_unknown_bucket() -> None:
    assert await _bucket(_request(client=None)) == "unknown"


async def test_forwarded_for_is_ignored_without_declared_trust() -> None:
    request = _request(client=("198.51.100.7", 1), headers={"X-Forwarded-For": "203.0.113.5"})
    assert await _bucket(request) == "198.51.100.7"


async def test_a_trusted_proxy_yields_the_forwarded_client(trusted_proxy: None) -> None:
    first = await _bucket(_request(client=("198.51.100.7", 1), headers={"X-Forwarded-For": "203.0.113.5"}))
    second = await _bucket(_request(client=("198.51.100.7", 1), headers={"X-Forwarded-For": "203.0.113.6"}))
    assert (first, second) == ("203.0.113.5", "203.0.113.6")
