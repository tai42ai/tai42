"""Every MCP transport the platform serves holds a real client session, in every served shape.

Each session is the pinned SDK's own client (streamable HTTP or SSE: initialise, list
tools) against the assembled app (``create_app``) served by uvicorn on a loopback port,
with the access-control gate on and an administrator key. Every response the client
receives is recorded through the SDK's HTTP-client factory hook: no response in a
session is a redirect (a redirect is where a deployment behind a TLS-ending proxy loses
the credential), and every GET stream of a session is answered by the transport
(``text/event-stream``), never by another route.

The shapes: the SSE boot; an SSE and an http sub-MCP app; the platform mounted in a host
application under a prefix; a reload of an SSE, a stateless and a stateful boot; an http
sub-MCP app on a stateless boot. Each runs with the Studio routes mounted (with and without
a built bundle) and without them. Two guard cases run on the real access-control stack with
no credential: a GET under the sub-MCP mount is refused by the outer guard, and a path whose
canonical form is a public route never reaches a sub-app however it is spelled.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import httpx
import pytest

from tai42_skeleton.app import applied_env
from tai42_skeleton.app.instance import app
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.app.sub_mcp_app import SubMcpAppRouter

from ._served_app import (
    ADMIN_KEY,
    INITIALIZE_BODY,
    MCP_POST_HEADERS,
    REQUEST_TIMEOUT_S,
    TOOL_NAME,
    ResponseLog,
    bearer,
    install_access_control,
    manifest,
    raw_request,
    served,
    sse_session_tools,
    streamable_session_tools,
    studio_bundle,
)

pytestmark = [
    pytest.mark.filterwarnings("ignore::async_lru.AlruCacheLoopResetWarning"),
    # The SDK answers a streamable-HTTP POST with an SSE response whose reader stream the
    # SSE response iterates to its end and never closes (``sse_starlette`` 3.4.6
    # ``EventSourceResponse._stream_response``); anyio reports that reader as unclosed when it
    # is freed. A third-party resource report, matched narrowly; the served harness collects
    # garbage before it returns, so the report lands inside the test that caused it.
    pytest.mark.filterwarnings("ignore:Unclosed <MemoryObjectReceiveStream:ResourceWarning"),
]

# "all" mounts the Studio routes, the SPA catch-all among them; "all-without-bundle" mounts
# them with no built Studio (the catch-all then answers 404); "none" mounts none of them.
ROUTER_SETS = ["all", "all-without-bundle", "none"]
HOST_PREFIX = "/host"


@pytest.fixture(autouse=True)
def _gate(monkeypatch: pytest.MonkeyPatch) -> None:
    install_access_control(monkeypatch)


@pytest.fixture(autouse=True)
def _process_state_restored(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Restore what a boot or a reload leaves on the process: the env a reload applied and the route registry."""
    from tai42_skeleton.app.route_registry import route_registry

    snapshot = dict(os.environ)
    monkeypatch.setattr(route_registry, "_routes", dict(route_registry._routes))
    monkeypatch.setattr(route_registry, "_control_plane_mount", route_registry._control_plane_mount)
    monkeypatch.setattr(app.config.config_manager, "read_env", dict)
    yield
    os.environ.clear()
    os.environ.update(snapshot)
    applied_env._loaded_env_keys = set()


def _routers(router_set: str, monkeypatch: pytest.MonkeyPatch, tmp_path) -> dict:
    if router_set == "all":
        monkeypatch.setenv("STUDIO_DIST_PATH", str(studio_bundle(tmp_path)))
        return manifest("all")
    if router_set == "all-without-bundle":
        monkeypatch.delenv("STUDIO_DIST_PATH", raising=False)
        return manifest("all")
    return manifest("none")


def _router() -> SubMcpAppRouter:
    return app.sub_app.mcp_sub_app_router  # type: ignore[return-value]


async def _register(slug: str, transport: str) -> None:
    await _router().register_sub_mcp_app(slug, [TOOL_NAME], transport=transport)


def _assert_held(log: ResponseLog, tools: list[str]) -> None:
    assert TOOL_NAME in tools
    assert log.redirects() == [], log.entries
    for method, path, status, content_type in log.get_streams():
        assert status == 200, (method, path, status, content_type)
        assert content_type.startswith("text/event-stream"), (method, path, status, content_type)


async def _reload() -> None:
    await reload_gate.run(app.admin.reload_config, reimports=True)


# -- the SSE boot ---------------------------------------------------------------


@pytest.mark.parametrize("router_set", ROUTER_SETS)
async def test_sse_boot_holds_a_session(router_set: str, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    body = _routers(router_set, monkeypatch, tmp_path)
    async with served(monkeypatch, body, transport="sse") as server:
        log = ResponseLog()
        tools = await sse_session_tools(f"{server.base_url}/sse", bearer(ADMIN_KEY), log)
    _assert_held(log, tools)


# -- the sub-MCP apps ----------------------------------------------------


@pytest.mark.parametrize("router_set", ROUTER_SETS)
async def test_sse_sub_mcp_app_holds_a_session(router_set: str, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    body = _routers(router_set, monkeypatch, tmp_path)
    async with served(monkeypatch, body) as server:
        await _register("sse-svc", "sse")
        log = ResponseLog()
        tools = await sse_session_tools(f"{server.base_url}/app/sse-svc/sse", bearer(ADMIN_KEY), log)
    _assert_held(log, tools)


@pytest.mark.parametrize("router_set", ROUTER_SETS)
async def test_http_sub_mcp_app_holds_a_session(router_set: str, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    body = _routers(router_set, monkeypatch, tmp_path)
    async with served(monkeypatch, body) as server:
        await _register("http-svc", "http")
        log = ResponseLog()
        tools = await streamable_session_tools(f"{server.base_url}/app/http-svc", bearer(ADMIN_KEY), log)
    _assert_held(log, tools)


# -- the platform mounted in a host application under a prefix -----------------


@pytest.mark.parametrize("router_set", ROUTER_SETS)
async def test_streamable_boot_under_a_host_prefix_holds_sessions(
    router_set: str, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    body = _routers(router_set, monkeypatch, tmp_path)
    async with served(monkeypatch, body, prefix=HOST_PREFIX) as server:
        await _register("http-svc", "http")
        await _register("sse-svc", "sse")
        main_log, http_log, sse_log = ResponseLog(), ResponseLog(), ResponseLog()
        main_tools = await streamable_session_tools(f"{server.base_url}/mcp", bearer(ADMIN_KEY), main_log)
        http_tools = await streamable_session_tools(f"{server.base_url}/app/http-svc", bearer(ADMIN_KEY), http_log)
        sse_tools = await sse_session_tools(f"{server.base_url}/app/sse-svc/sse", bearer(ADMIN_KEY), sse_log)
    _assert_held(main_log, main_tools)
    _assert_held(http_log, http_tools)
    _assert_held(sse_log, sse_tools)


@pytest.mark.parametrize("router_set", ROUTER_SETS)
async def test_sse_boot_under_a_host_prefix_holds_a_session(
    router_set: str, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    body = _routers(router_set, monkeypatch, tmp_path)
    async with served(monkeypatch, body, transport="sse", prefix=HOST_PREFIX) as server:
        log = ResponseLog()
        tools = await sse_session_tools(f"{server.base_url}/sse", bearer(ADMIN_KEY), log)
    _assert_held(log, tools)


# -- a reload rebuilds what was booted -----------------------------------


async def test_a_reload_of_an_sse_boot_serves_a_new_sse_session(monkeypatch: pytest.MonkeyPatch) -> None:
    async with served(monkeypatch, manifest("none"), transport="sse") as server:
        before = ResponseLog()
        _assert_held(before, await sse_session_tools(f"{server.base_url}/sse", bearer(ADMIN_KEY), before))
        await _reload()
        after = ResponseLog()
        tools = await sse_session_tools(f"{server.base_url}/sse", bearer(ADMIN_KEY), after)
    _assert_held(after, tools)


async def _assert_stateless(base_url: str) -> None:
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
        post = await client.post(base_url, content=INITIALIZE_BODY, headers={**MCP_POST_HEADERS, **bearer(ADMIN_KEY)})
        get = await client.get(base_url, headers={"Accept": "text/event-stream", **bearer(ADMIN_KEY)})
    assert post.status_code == 200, post.text
    assert "mcp-session-id" not in post.headers
    assert not get.headers.get("content-type", "").startswith("text/event-stream"), get.status_code


async def test_a_reload_of_a_stateless_boot_stays_stateless(monkeypatch: pytest.MonkeyPatch) -> None:
    async with served(monkeypatch, manifest("none"), stateless_http=True) as server:
        await _assert_stateless(f"{server.base_url}/mcp")
        await _reload()
        await _assert_stateless(f"{server.base_url}/mcp")


async def test_an_http_sub_mcp_app_on_a_stateless_boot_is_stateless(monkeypatch: pytest.MonkeyPatch) -> None:
    async with served(monkeypatch, manifest("none"), stateless_http=True) as server:
        await _register("http-svc", "http")
        await _assert_stateless(f"{server.base_url}/app/http-svc")


async def test_a_stateful_boot_holds_sessions_before_and_after_a_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    async with served(monkeypatch, manifest("none")) as server:
        before = ResponseLog()
        _assert_held(before, await streamable_session_tools(f"{server.base_url}/mcp", bearer(ADMIN_KEY), before))
        await _reload()
        after = ResponseLog()
        tools = await streamable_session_tools(f"{server.base_url}/mcp", bearer(ADMIN_KEY), after)
    _assert_held(after, tools)


# -- no credential under the sub-MCP mount is refused by the outer guard ----------


@pytest.mark.parametrize("prefix", ["", HOST_PREFIX], ids=["direct", "host-prefix"])
@pytest.mark.parametrize("router_set", ROUTER_SETS)
async def test_an_unauthenticated_get_under_the_sub_mcp_mount_is_refused(
    router_set: str, prefix: str, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    body = _routers(router_set, monkeypatch, tmp_path)
    async with served(monkeypatch, body, prefix=prefix) as server:
        await _register("sse-svc", "sse")
        await _register("http-svc", "http")
        for target in (f"{prefix}/app/sse-svc/sse", f"{prefix}/app/http-svc"):
            for method in ("GET", "HEAD"):
                status, _headers, _payload = await raw_request(
                    server.origin, method, target, headers={"Accept": "text/event-stream"}
                )
                assert status == 401, (method, target, status)


# -- a sub-app serves only the path the gate decided on ---------------------------

_PUBLIC_TARGETS = ["/api/login/claim", "/universal_webhook/x"]


def _dot_spellings(slug: str, transport: str) -> list[str]:
    """Spellings delivered under the sub-MCP mount whose canonical form is a public route."""
    spellings = [f"/app//{slug}/../../..{target}" for target in _PUBLIC_TARGETS]
    if transport == "sse":
        spellings += [f"/app//{slug}/messages/../../../..{target}" for target in _PUBLIC_TARGETS]
    return spellings


@pytest.mark.parametrize("prefix", ["", HOST_PREFIX], ids=["direct", "host-prefix"])
@pytest.mark.parametrize("transport", ["http", "sse"])
async def test_a_non_canonical_path_never_reaches_a_sub_app(
    transport: str, prefix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    slug = f"{transport}-svc"
    # The public targets are served: the login door (declared pre-auth) and the webhook ingress.
    body = {**manifest("none"), "routers_modules": ["tai42_skeleton.routers.login", "tai42_skeleton.routers.hooks"]}
    async with served(monkeypatch, body, prefix=prefix) as server:
        await _register(slug, transport)
        for spelling in _dot_spellings(slug, transport):
            status, headers, _payload = await raw_request(
                server.origin,
                "POST",
                f"{prefix}{spelling}?session_id=0",
                body=INITIALIZE_BODY,
                headers=MCP_POST_HEADERS,
            )
            assert "mcp-session-id" not in headers, (spelling, status)
            assert status == 404, (spelling, status)
            # Nothing was looked up, built or dispatched for the slug.
            assert slug not in _router()._server_cache, spelling
