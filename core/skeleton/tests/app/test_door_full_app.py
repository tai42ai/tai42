"""The MCP door on the assembled app (``create_app`` served by uvicorn), in every served shape.

A declaring identity provider and ``ACCESS_CONTROL_PUBLIC_BASE_URL`` turn the door on. Each
shape checks the pointer an unauthenticated request gets and the document it points at: the
streamable, stateless and SSE boots, a sub-MCP app of each transport, a reload of a
streamable and of an SSE boot, the Studio routes mounted, access control off, and the app
embedded under a host prefix (with the prefix in the public URL). On the SSE doors a real
stream is opened, the announced message address is taken from it, and that exact address is
posted to: unauthenticated it gets the stream's pointer; with the credential it is accepted,
and the provider read the stream's public URL as the request resource.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest

from tai42_skeleton.app.instance import app
from tai42_skeleton.app.reload_gate import reload_gate

from ._door_fixtures import (
    ISSUER,
    PUBLIC_BASE_URL,
    STATIC_TOKEN,
    STATIC_USER,
    DeclaringTokenProvider,
    install_declaring_provider,
)
from ._served_app import (
    INITIALIZE_BODY,
    MCP_POST_HEADERS,
    REQUEST_TIMEOUT_S,
    TOOL_NAME,
    ResponseLog,
    bearer,
    install_access_control,
    manifest,
    served,
    sse_session_tools,
    streamable_session_tools,
    studio_bundle,
)

pytestmark = [
    pytest.mark.filterwarnings("ignore::async_lru.AlruCacheLoopResetWarning"),
    # See ``test_transport_sessions``: the SDK's streamable-HTTP POST leaves its SSE reader unclosed.
    pytest.mark.filterwarnings("ignore:Unclosed <MemoryObjectReceiveStream:ResourceWarning"),
]

DOC = "/.well-known/oauth-protected-resource"


def _pointer(endpoint: str, base: str = PUBLIC_BASE_URL) -> str:
    return f'Bearer resource_metadata="{base}{DOC}{endpoint}"'


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> DeclaringTokenProvider:
    access = install_access_control(monkeypatch)
    access.pg.add_policy(STATIC_USER, scopes=["*"])
    declaring = DeclaringTokenProvider()
    install_declaring_provider(monkeypatch, declaring)
    monkeypatch.setenv("ACCESS_CONTROL_PUBLIC_BASE_URL", PUBLIC_BASE_URL)
    monkeypatch.setattr(app.config.config_manager, "read_env", dict)
    return declaring


async def _register(slug: str, transport: str) -> None:
    await app.sub_app.mcp_sub_app_router.register_sub_mcp_app(slug, [TOOL_NAME], transport=transport)  # type: ignore[attr-defined]


async def _refused_post(client: httpx.AsyncClient, path: str) -> httpx.Response:
    return await client.post(path, content=INITIALIZE_BODY, headers=MCP_POST_HEADERS)


async def _assert_door(client: httpx.AsyncClient, request_path: str, endpoint: str, *, method: str = "POST") -> None:
    if method == "POST":
        refused = await _refused_post(client, request_path)
    else:
        refused = await client.get(request_path, headers={"Accept": "text/event-stream"})
    assert refused.status_code == 401, (request_path, refused.status_code)
    assert refused.headers["www-authenticate"] == _pointer(endpoint)
    document = await client.get(f"{DOC}{endpoint}")
    assert document.status_code == 200, document.text
    assert document.json() == {
        "resource": f"{PUBLIC_BASE_URL}{endpoint}",
        "authorization_servers": [ISSUER],
        "bearer_methods_supported": ["header"],
    }


def _client(base_url: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base_url, timeout=REQUEST_TIMEOUT_S)


@pytest.mark.usefixtures("provider")
@pytest.mark.parametrize("router_set", ["none", "all"])
@pytest.mark.parametrize("stateless", [False, True], ids=["stateful", "stateless"])
async def test_the_streamable_boot_points_at_its_document(
    monkeypatch: pytest.MonkeyPatch, tmp_path, router_set: str, stateless: bool
) -> None:
    if router_set == "all":
        monkeypatch.setenv("STUDIO_DIST_PATH", str(studio_bundle(tmp_path)))
    async with served(monkeypatch, manifest(router_set), stateless_http=stateless) as server:
        async with _client(server.base_url) as client:
            await _assert_door(client, "/mcp", "/mcp")
            await _assert_door(client, "/mcp/", "/mcp")
        log = ResponseLog()
        assert TOOL_NAME in await streamable_session_tools(f"{server.base_url}/mcp", bearer(STATIC_TOKEN), log)


@pytest.mark.usefixtures("provider")
@pytest.mark.parametrize("router_set", ["none", "all"])
async def test_the_sse_boot_points_at_the_stream_document(
    monkeypatch: pytest.MonkeyPatch, tmp_path, router_set: str
) -> None:
    if router_set == "all":
        monkeypatch.setenv("STUDIO_DIST_PATH", str(studio_bundle(tmp_path)))
    async with served(monkeypatch, manifest(router_set), transport="sse") as server, _client(server.base_url) as client:
        await _assert_door(client, "/sse", "/sse", method="GET")
        await _assert_door(client, "/messages/?session_id=0", "/sse")


@pytest.mark.usefixtures("provider")
@pytest.mark.parametrize("router_set", ["none", "all"])
async def test_each_sub_mcp_transport_points_at_its_own_document(
    monkeypatch: pytest.MonkeyPatch, tmp_path, router_set: str
) -> None:
    if router_set == "all":
        monkeypatch.setenv("STUDIO_DIST_PATH", str(studio_bundle(tmp_path)))
    async with served(monkeypatch, manifest(router_set)) as server:
        await _register("http-svc", "http")
        await _register("sse-svc", "sse")
        async with _client(server.base_url) as client:
            await _assert_door(client, "/app/http-svc", "/app/http-svc")
            await _assert_door(client, "/app/http-svc", "/app/http-svc", method="GET")
            await _assert_door(client, "/app/sse-svc/sse", "/app/sse-svc/sse", method="GET")
            await _assert_door(client, "/app/sse-svc/messages/?session_id=0", "/app/sse-svc/sse")
            # A slug no router knows gets the same answer.
            await _assert_door(client, "/app/never-registered", "/app/never-registered")


async def _reload() -> None:
    await reload_gate.run(app.admin.reload_config, reimports=True)


@pytest.mark.usefixtures("provider")
@pytest.mark.parametrize(
    ("transport", "probe", "endpoint", "method"),
    [("http", "/mcp", "/mcp", "POST"), ("sse", "/sse", "/sse", "GET")],
    ids=["streamable", "sse"],
)
async def test_a_reload_keeps_the_door(
    monkeypatch: pytest.MonkeyPatch, transport: str, probe: str, endpoint: str, method: str
) -> None:
    async with served(monkeypatch, manifest("none"), transport=transport) as server:  # type: ignore[arg-type]
        await _reload()
        async with _client(server.base_url) as client:
            await _assert_door(client, probe, endpoint, method=method)


async def test_with_access_control_off_nothing_is_advertised(monkeypatch: pytest.MonkeyPatch) -> None:
    install_declaring_provider(monkeypatch, DeclaringTokenProvider())
    monkeypatch.setenv("ACCESS_CONTROL_ENABLE", "false")
    monkeypatch.setenv("ACCESS_CONTROL_PUBLIC_BASE_URL", PUBLIC_BASE_URL)
    async with served(monkeypatch, manifest("none")) as server, _client(server.base_url) as client:
        admitted = await _refused_post(client, "/mcp")
        document = await client.get(f"{DOC}/mcp")
    assert admitted.status_code != 401
    assert "www-authenticate" not in admitted.headers
    assert document.status_code == 404


@pytest.mark.usefixtures("provider")
async def test_embedded_under_a_prefix_the_pointer_and_the_document_carry_it(monkeypatch: pytest.MonkeyPatch) -> None:
    base = f"{PUBLIC_BASE_URL}/host"
    monkeypatch.setenv("ACCESS_CONTROL_PUBLIC_BASE_URL", base)
    async with served(monkeypatch, manifest("none"), prefix="/host") as server:
        await _register("http-svc", "http")
        await _register("sse-svc", "sse")
        async with _client(server.origin) as client:
            refused = await _refused_post(client, "/host/mcp")
            document = await client.get(f"/host{DOC}/mcp")
            sub_refused = await client.get("/host/app/sse-svc/sse", headers={"Accept": "text/event-stream"})
        assert refused.status_code == 401
        assert refused.headers["www-authenticate"] == _pointer("/mcp", base)
        assert document.json()["resource"] == f"{base}/mcp"
        assert sub_refused.headers["www-authenticate"] == _pointer("/app/sse-svc/sse", base)
        for url, run in (
            (f"{server.base_url}/mcp", streamable_session_tools),
            (f"{server.base_url}/app/http-svc", streamable_session_tools),
            (f"{server.base_url}/app/sse-svc/sse", sse_session_tools),
        ):
            assert TOOL_NAME in await run(url, bearer(STATIC_TOKEN), ResponseLog())


@pytest.mark.usefixtures("provider")
async def test_an_sse_boot_embedded_under_a_prefix_holds_a_session_with_the_door_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ACCESS_CONTROL_PUBLIC_BASE_URL", f"{PUBLIC_BASE_URL}/host")
    async with served(monkeypatch, manifest("none"), transport="sse", prefix="/host") as server:
        assert TOOL_NAME in await sse_session_tools(f"{server.base_url}/sse", bearer(STATIC_TOKEN), ResponseLog())


# -- the SSE doors, driven by a real stream ------------------------------------------------------


@asynccontextmanager
async def _announced_message_address(client: httpx.AsyncClient, stream_path: str) -> AsyncIterator[str]:
    """Open the SSE stream with the static token and yield the message address it announces."""
    async with client.stream(
        "GET", stream_path, headers={"Accept": "text/event-stream", **bearer(STATIC_TOKEN)}
    ) as stream:
        assert stream.status_code == 200
        async for line in stream.aiter_lines():
            if line.startswith("data:"):
                yield line.removeprefix("data:").strip()
                return
    raise AssertionError("the stream announced no message address")


@pytest.mark.parametrize(
    ("transport", "stream_path", "endpoint"),
    [("sse", "/sse", "/sse"), ("http", "/app/sse-svc/sse", "/app/sse-svc/sse")],
    ids=["sse-boot", "sse-sub-mcp"],
)
async def test_the_announced_message_address_is_guarded_and_bound_to_the_stream_resource(
    monkeypatch: pytest.MonkeyPatch, provider: DeclaringTokenProvider, transport: str, stream_path: str, endpoint: str
) -> None:
    async with served(monkeypatch, manifest("none"), transport=transport) as server:  # type: ignore[arg-type]
        await _register("sse-svc", "sse")
        async with _client(server.origin) as client, _announced_message_address(client, stream_path) as address:
            assert address.split("?")[0].endswith("/messages/")
            refused = await client.post(address, content=json.dumps({"jsonrpc": "2.0", "method": "ping", "id": 1}))
            assert refused.status_code == 401
            assert refused.headers["www-authenticate"] == _pointer(endpoint)
            provider.seen_resources.clear()
            accepted = await client.post(
                address,
                content=INITIALIZE_BODY,
                headers={"Content-Type": "application/json", **bearer(STATIC_TOKEN)},
            )
            assert accepted.status_code == 202, accepted.text
            assert provider.seen_resources == [f"{PUBLIC_BASE_URL}{endpoint}"]
