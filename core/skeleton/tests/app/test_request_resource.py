"""The request resource the credential backend binds for the identity providers.

An identity provider reads ``get_request_resource()`` while it validates a token: the public
URL of the MCP endpoint the request addresses — only when the deployment advertises an
authorization server AND the path as sent is the canonical path the gate decides on — else
``None``. Driven through the real backend and verifier with a fixture provider that records
what it reads, over the transport surfaces recorded the way the serving app records them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from starlette.authentication import AuthenticationError
from starlette.requests import Request
from tai42_contract.access_control.context import get_request_resource
from tai42_contract.app import tai42_app

from tai42_skeleton.access_control.adapter import AuthAdapter
from tai42_skeleton.access_control.backend import AccessControlAuthBackend
from tai42_skeleton.access_control.settings import access_control_settings
from tai42_skeleton.app.http import record_sub_mcp_mount
from tai42_skeleton.app.route_registry import route_registry
from tai42_skeleton.app.serving_core import record_sse_surface
from tai42_skeleton.app.sub_mcp_app import ROOT_PREFIX

from ..access_control.conftest import _FakeApp
from ._door_fixtures import (
    PUBLIC_BASE_URL,
    STATIC_TOKEN,
    STATIC_USER,
    DeclaringTokenProvider,
    public_base_url,
    register_declaring_provider,
)
from ._served_app import install_access_control

pytestmark = pytest.mark.filterwarnings("ignore::async_lru.AlruCacheLoopResetWarning")


@pytest.fixture(autouse=True)
def _surfaces(monkeypatch: pytest.MonkeyPatch, streamable_http_mounted) -> None:
    monkeypatch.setattr(route_registry, "_routes", dict(route_registry._routes))
    record_sse_surface("/sse", "/messages/")
    record_sub_mcp_mount(ROOT_PREFIX)


@pytest.fixture(autouse=True)
def _stores(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    access = install_access_control(monkeypatch)
    access.pg.add_policy(STATIC_USER, scopes=["*"])
    with tai42_app.bound(_FakeApp()):
        yield


def _backend(provider: DeclaringTokenProvider, monkeypatch: pytest.MonkeyPatch) -> AccessControlAuthBackend:
    register_declaring_provider(provider)
    monkeypatch.setenv("ACCESS_CONTROL_AUTH_PROVIDERS", '["door-fixture"]')
    settings = access_control_settings()
    return AccessControlAuthBackend(AuthAdapter(settings)._internal_verifier, settings)


def _conn(raw_target: str, *, token: str = STATIC_TOKEN, root_path: str = "") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": raw_target,
            "raw_path": raw_target.encode("ascii"),
            "root_path": root_path,
            "query_string": b"",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }
    )


@pytest.mark.parametrize(
    ("target", "resource"),
    [
        ("/mcp", "/mcp"),
        ("/mcp/", "/mcp"),
        ("/sse", "/sse"),
        ("/messages/", "/sse"),
        ("/messages", "/sse"),
        ("/app/svc", "/app/svc"),
        ("/app/svc/sse", "/app/svc/sse"),
        ("/app/svc/messages/", "/app/svc/sse"),
    ],
)
async def test_an_mcp_endpoint_request_reads_the_endpoint_resource(
    monkeypatch: pytest.MonkeyPatch, target: str, resource: str
) -> None:
    provider = DeclaringTokenProvider()
    with public_base_url(monkeypatch, PUBLIC_BASE_URL):
        backend = _backend(provider, monkeypatch)
        await backend.authenticate(_conn(target))
    assert provider.seen_resources == [f"{PUBLIC_BASE_URL}{resource}"]


async def test_under_a_host_prefix_the_resource_is_the_public_url_plus_the_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = DeclaringTokenProvider()
    with public_base_url(monkeypatch, "https://host.example/tenant"):
        backend = _backend(provider, monkeypatch)
        await backend.authenticate(_conn("/tenant/mcp", root_path="/tenant"))
    assert provider.seen_resources == ["https://host.example/tenant/mcp"]


async def test_an_api_request_reads_none(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = DeclaringTokenProvider()
    with public_base_url(monkeypatch, PUBLIC_BASE_URL):
        backend = _backend(provider, monkeypatch)
        await backend.authenticate(_conn("/api/tools"))
    assert provider.seen_resources == [None]


async def test_nothing_advertised_reads_none(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = DeclaringTokenProvider()
    with public_base_url(monkeypatch, None):
        backend = _backend(provider, monkeypatch)
        await backend.authenticate(_conn("/mcp"))
    assert provider.seen_resources == [None]


_DOT_SPELLINGS = [
    "/api/storage/dirs/../../../mcp",
    "/api/storage/dirs/%2e%2e/%2e%2e/%2e%2e/mcp",
    "/api/storage/dirs/..%2F..%2F..%2Fmcp",
    "//mcp",
    "/mcp//",
    "/api/storage/dirs/../../../sse",
    "/api/storage/dirs/../../../messages/",
    "/api/storage/dirs/../../../app/svc",
    "/messages/../app/svc/messages/",
]


@pytest.mark.parametrize("target", _DOT_SPELLINGS)
async def test_a_path_not_sent_canonically_reads_none_and_a_door_bound_token_is_refused(
    monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    recording = DeclaringTokenProvider()
    door_bound = DeclaringTokenProvider(accepted_resource=f"{PUBLIC_BASE_URL}/mcp")
    with public_base_url(monkeypatch, PUBLIC_BASE_URL):
        await _backend(recording, monkeypatch).authenticate(_conn(target))
        with pytest.raises(AuthenticationError):
            await _backend(door_bound, monkeypatch).authenticate(_conn(target))
    assert recording.seen_resources == [None]


async def test_a_door_bound_token_is_accepted_at_its_own_endpoint_only(monkeypatch: pytest.MonkeyPatch) -> None:
    door_bound = DeclaringTokenProvider(accepted_resource=f"{PUBLIC_BASE_URL}/mcp")
    with public_base_url(monkeypatch, PUBLIC_BASE_URL):
        backend = _backend(door_bound, monkeypatch)
        await backend.authenticate(_conn("/mcp"))
        with pytest.raises(AuthenticationError):
            await backend.authenticate(_conn("/app/svc"))
        with pytest.raises(AuthenticationError):
            await backend.authenticate(_conn("/api/tools"))


async def test_the_resource_never_outlives_the_provider_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = DeclaringTokenProvider()
    with public_base_url(monkeypatch, PUBLIC_BASE_URL):
        backend = _backend(provider, monkeypatch)
        await backend.authenticate(_conn("/mcp"))
        assert get_request_resource() is None
        with pytest.raises(AuthenticationError):
            await backend.authenticate(_conn("/mcp", token="not-the-token"))
        assert get_request_resource() is None


class _RaisingProvider(DeclaringTokenProvider):
    async def validate_token(self, token: str):
        self.seen_resources.append(get_request_resource())
        raise RuntimeError("provider store unreachable")


async def test_the_resource_is_reset_after_a_provider_chain_that_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _RaisingProvider()
    with public_base_url(monkeypatch, PUBLIC_BASE_URL):
        backend = _backend(provider, monkeypatch)
        with pytest.raises(AuthenticationError):
            await backend.authenticate(_conn("/mcp"))
    assert provider.seen_resources == [f"{PUBLIC_BASE_URL}/mcp"]
    assert get_request_resource() is None


class _InterleavingProvider(DeclaringTokenProvider):
    async def validate_token(self, token: str):
        first = get_request_resource()
        for _ in range(5):
            await asyncio.sleep(0)
        self.seen_resources.append(first)
        self.seen_resources.append(get_request_resource())
        return await super().validate_token(token)


async def test_two_interleaved_requests_do_not_see_each_other(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _InterleavingProvider()
    with public_base_url(monkeypatch, PUBLIC_BASE_URL):
        backend = _backend(provider, monkeypatch)
        await asyncio.gather(backend.authenticate(_conn("/mcp")), backend.authenticate(_conn("/app/svc")))
    pairs = list(zip(provider.seen_resources[0::3], provider.seen_resources[1::3], strict=True))
    assert sorted(pairs) == [
        (f"{PUBLIC_BASE_URL}/app/svc", f"{PUBLIC_BASE_URL}/app/svc"),
        (f"{PUBLIC_BASE_URL}/mcp", f"{PUBLIC_BASE_URL}/mcp"),
    ]
