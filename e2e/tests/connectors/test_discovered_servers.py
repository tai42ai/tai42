"""MCP servers added by their address, end to end on a two-replica stack with access control on.

Each scenario adds the OAuth MCP fixture (a protected MCP server that is its own authorization server,
:mod:`tai42_e2e_fixtures.oauth_mcp_server`) through the provider door and drives the public routes with
real keys: discovery, dynamic registration, PKCE, ``resource``, the issuer check, refresh, revocation,
removal, client credentials set by an administrator, the client-identity document, a scope the token
lacks, and the rule that a credential goes only to its provider's own server. Every scenario gets its
own fixture server (a fresh address, so no replica holds its metadata from an earlier scenario).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import psycopg
import pytest
import yaml
from fastmcp.client.client import CallToolResult

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.manifests import (
    DISCOVERED_ENV_CLIENT_ID,
    DISCOVERED_ENV_CLIENT_SECRET,
    DISCOVERED_ENV_PROVIDER_ID,
    DOCUMENT_ORIGIN,
)
from tai42_e2e.seeding import seed_editor_key
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async
from tai42_e2e_fixtures.oauth_mcp_server import OAuthMcpServer

pytestmark = pytest.mark.needs(
    "kind:connectors",
    "kind:identity",
    "mutable",
    "probe-tools",
    "helper:oauth-mcp-server",
    "topology:replicas",
    "store:postgres",
    "files",
    "process",
    "setting:CONNECTORS_REDIRECT_URI_ALLOWLIST=stack-origins+https-test-origin",
)

_DOCUMENT_HOST = urlparse(DOCUMENT_ORIGIN).netloc
_BIND_DEADLINE = 90.0
# Past the reload probe budget (3 s), inside the platform's refresh deadline (20 s).
_REFRESH_DELAY = 19.0


# -- fixtures --------------------------------------------------------------------------------------


@pytest.fixture
def server() -> Iterator[OAuthMcpServer]:
    fixture = OAuthMcpServer()
    fixture.start()
    try:
        yield fixture
    finally:
        fixture.stop()


@pytest.fixture
def second_server() -> Iterator[OAuthMcpServer]:
    fixture = OAuthMcpServer()
    fixture.start()
    try:
        yield fixture
    finally:
        fixture.stop()


@pytest.fixture(scope="module")
def stack(discovered_connectors_stack: TaiStack) -> TaiStack:
    return discovered_connectors_stack


@pytest.fixture(scope="module")
def editor_token(stack: TaiStack) -> str:
    return seed_editor_key(stack.infra, stack.resources, owner_id="door-editor", key_id="door-editor-key")


@pytest.fixture
def admin_a(stack: TaiStack) -> ApiClient:
    return stack.api(port=stack.port_a)


@pytest.fixture
def admin_b(stack: TaiStack) -> ApiClient:
    return stack.api(port=stack.port_b)


# -- helpers ---------------------------------------------------------------------------------------


def _title(provider_id: str, alias: str) -> str:
    return f"{provider_id}_default_{alias}"


def _callback(stack: TaiStack, port: int) -> str:
    return f"{stack.origin(port)}/oauth-bridge.html"


async def _add(api: ApiClient, url: str, provider_id: str, **extra: Any) -> dict[str, Any]:
    added = await api.post(
        "/api/connectors/providers", json={"url": url, "id": provider_id, **extra}, retry_on_reloading=True
    )
    return added["provider"]


async def _status(api: ApiClient, method: str, path: str, json_body: Any = None, **kw: Any) -> tuple[int, Any]:
    response = await api.request_raw(method, path, json=json_body, **kw)
    return response.status_code, response.json()


async def _authorize(authorize_url: str) -> dict[str, str]:
    """Follow the authorize URL to the fixture and read the redirect's query (``code``, ``state``, ``iss``)."""
    async with httpx.AsyncClient(follow_redirects=False, timeout=30.0) as client:
        response = await client.get(authorize_url)
    assert response.status_code == 302, f"authorize answered {response.status_code}: {response.text}"
    return {k: v[0] for k, v in parse_qs(urlparse(response.headers["location"]).query).items()}


async def _complete(api: ApiClient, redirect: dict[str, str]) -> tuple[int, dict[str, Any]]:
    body = {key: redirect[key] for key in ("code", "state", "iss", "error") if key in redirect}
    status, answer = await _status(api, "POST", "/api/connectors/oauth/complete", body)
    return status, answer["data"]


async def _start(api: ApiClient, provider_id: str, alias: str, *, origin: str | None = None) -> dict[str, Any]:
    return await api.post(
        "/api/connectors/connections/start",
        json={"provider_id": provider_id, "alias": alias, "enabled_sub_services": ["default"]},
        headers={"Origin": origin} if origin is not None else None,
        retry_on_reloading=True,
    )


async def _connect(start_api: ApiClient, complete_api: ApiClient, provider_id: str, alias: str) -> str:
    started = await _start(start_api, provider_id, alias)
    status, completed = await _complete(complete_api, await _authorize(started["authorize_url"]))
    assert (status, completed["kind"]) == (200, "success"), completed
    return completed["connection_id"]


async def _reconnect(start_api: ApiClient, complete_api: ApiClient, connection_id: str) -> dict[str, str]:
    started = await start_api.post(
        f"/api/connectors/connections/{connection_id}/reconnect",
        json={"enabled_sub_services": ["default"]},
        retry_on_reloading=True,
    )
    redirect = await _authorize(started["authorize_url"])
    status, completed = await _complete(complete_api, redirect)
    assert (status, completed["kind"]) == (200, "success"), completed
    return redirect


async def _call(stack: TaiStack, tool: str, arguments: dict[str, Any] | None = None, *, port: int | None = None):
    async with stack.mcp(port=port or stack.port_a, auth=stack.auth_token) as mcp:
        return await mcp.call_tool(tool, arguments or {}, raise_on_error=False)


def _ok(result: CallToolResult) -> Any:
    assert not result.is_error, f"the tool call failed: {result.content}"
    return result.data


def _connector_error(result: CallToolResult) -> dict[str, Any]:
    assert result.is_error, f"the tool call succeeded: {result.data}"
    text = "".join(getattr(part, "text", "") for part in result.content)
    return json.loads(text[text.index("{") :])


async def _served(stack: TaiStack, tool: str, *, ports: tuple[int, ...] | None = None) -> None:
    """Wait until ``tool`` is served on every replica in ``ports`` (both by default)."""
    for port in ports or (stack.port_a, stack.port_b):

        async def present(port: int = port) -> bool:
            async with stack.mcp(port=port, auth=stack.auth_token) as mcp:
                return tool in await mcp.tool_names()

        await wait_for_async(present, deadline=_BIND_DEADLINE, message=f"{tool} never served on port {port}")


async def _catalog(api: ApiClient) -> dict[str, dict[str, Any]]:
    listed = await api.get("/api/connectors/providers")
    return {entry["id"]: entry for entry in listed["providers"]}


def _pg(stack: TaiStack) -> psycopg.Connection:
    res = stack.resources
    return psycopg.connect(
        host=res.pg_host, port=res.pg_port, user=res.pg_user, password=res.pg_password, dbname=res.pg_db
    )


def _manifest_document(stack: TaiStack) -> dict[str, Any]:
    return yaml.safe_load((stack.root / "config" / "manifest.yml").read_text()) or {}


async def _replace_manifest(api: ApiClient, document: dict[str, Any]) -> dict[str, Any]:
    return await api.post(
        "/api/manifest/replace", json={"manifest_text": yaml.safe_dump(document)}, retry_on_reloading=True
    )


# -- 1. add and connect with dynamic registration --------------------------------------------------


async def test_add_then_connect_registers_a_client_and_binds_the_tools(
    stack: TaiStack, server: OAuthMcpServer, admin_a: ApiClient, admin_b: ApiClient, uniq: Callable[[str], str]
) -> None:
    provider_id = uniq("srv")
    entry = await _add(admin_a, server.mcp_url, provider_id)
    assert (entry["origin"], entry["removable"], entry["client_source"]) == ("community", True, "unset")

    alias = uniq("a")
    await _connect(admin_a, admin_b, provider_id, alias)
    tool = f"{_title(provider_id, alias)}_ping"
    await _served(stack, tool)
    status = await admin_a.get("/api/mcp-status")
    assert _title(provider_id, alias) in status["bound"], status
    assert _ok(await _call(stack, tool)) == {"ok": "pong"}

    seen = server.counters()
    assert [r["redirect_uris"] for r in seen.registrations] == [[_callback(stack, stack.port_a)]]
    login = seen.authorizations[-1]
    assert login["resource"] == server.mcp_url
    assert login["code_challenge_method"] == "S256"
    (exchange,) = [r for r in seen.token_requests if r["grant_type"] == "authorization_code"]
    assert exchange["resource"] == server.mcp_url
    assert (await _catalog(admin_a))[provider_id]["client_source"] == "dynamic"

    # A login started from the other replica's origin registers a client for that callback.
    await _connect(admin_b, admin_a, provider_id, uniq("b"))
    assert [r["redirect_uris"] for r in server.counters().registrations] == [
        [_callback(stack, stack.port_a)],
        [_callback(stack, stack.port_b)],
    ]

    # The first connection still refreshes under its own client.
    first_client = server.counters().registrations[0]["client_id"]
    refreshes = len(server.counters().refreshes)
    server.expire_access_tokens()
    assert _ok(await _call(stack, tool)) == {"ok": "pong"}
    assert {"client_id": first_client, "resource": server.mcp_url, "accepted": True} in (
        server.counters().refreshes[refreshes:]
    )


# -- 2. expiry -------------------------------------------------------------------------------------


async def test_an_expired_token_is_refreshed_once_with_the_resource(
    stack: TaiStack, server: OAuthMcpServer, admin_a: ApiClient, uniq: Callable[[str], str]
) -> None:
    provider_id, alias = uniq("srv"), uniq("a")
    await _add(admin_a, server.mcp_url, provider_id)
    await _connect(admin_a, admin_a, provider_id, alias)
    tool = f"{_title(provider_id, alias)}_echo"
    await _served(stack, tool, ports=(stack.port_a,))
    before = len(server.counters().refreshes)
    server.expire_access_tokens()
    assert _ok(await _call(stack, tool, {"payload": "x"})) == {"echoed": "x"}
    (registration,) = server.counters().registrations
    assert server.counters().refreshes[before:] == [
        {"client_id": registration["client_id"], "resource": server.mcp_url, "accepted": True}
    ]


# -- 3. no refresh token ---------------------------------------------------------------------------


async def test_without_a_refresh_token_an_expired_grant_asks_for_a_reconnect(
    stack: TaiStack, server: OAuthMcpServer, admin_a: ApiClient, uniq: Callable[[str], str]
) -> None:
    server.set(no_refresh_token=True)
    provider_id, alias = uniq("srv"), uniq("a")
    await _add(admin_a, server.mcp_url, provider_id)
    connection_id = await _connect(admin_a, admin_a, provider_id, alias)
    tool = f"{_title(provider_id, alias)}_ping"
    await _served(stack, tool, ports=(stack.port_a,))
    assert _ok(await _call(stack, tool)) == {"ok": "pong"}

    server.expire_access_tokens()
    assert _connector_error(await _call(stack, tool))["code"] == "reconnect_required"
    await _reconnect(admin_a, admin_a, connection_id)
    assert _ok(await _call(stack, tool)) == {"ok": "pong"}

    await admin_a.delete(f"/api/connectors/connections/{connection_id}", retry_on_reloading=True)
    assert server.counters().revocations[-1] == {"kind": "access"}


# -- 4. no registration; administrators only -------------------------------------------------------


async def test_a_server_without_registration_takes_an_administrators_client(
    stack: TaiStack,
    server: OAuthMcpServer,
    admin_a: ApiClient,
    admin_b: ApiClient,
    editor_token: str,
    uniq: Callable[[str], str],
) -> None:
    server.set(registration_off=True)
    provider_id = uniq("srv")
    await _add(admin_a, server.mcp_url, provider_id)

    status, refused = await _status(
        admin_a,
        "POST",
        "/api/connectors/connections/start",
        {"provider_id": provider_id, "alias": uniq("a"), "enabled_sub_services": ["default"]},
    )
    assert (status, refused.get("code")) == (501, "connector-client-credentials-required"), refused

    editor = admin_a.with_token(editor_token)
    for method, path, body in (
        ("POST", "/api/connectors/providers/discover", {"url": server.mcp_url}),
        ("POST", "/api/connectors/providers", {"url": server.mcp_url, "id": uniq("srv")}),
        ("DELETE", f"/api/connectors/providers/{provider_id}", None),
        ("PUT", f"/api/connectors/providers/{provider_id}/client", {"client_id": "x", "issuer": server.issuer}),
    ):
        response = await editor.request_raw(method, path, json=body)
        assert response.status_code == 403, f"{method} {path}: {response.status_code} {response.text}"

    status, refused = await _status(
        admin_a, "PUT", f"/api/connectors/providers/{provider_id}/client", {"client_id": "hand-app"}
    )
    assert (status, refused["code"], refused["issuers"]) == (400, "connector-client-issuer-required", [server.issuer])

    server.register_by_hand(
        "hand-app",
        secret="hand-secret",
        method="client_secret_post",
        redirect_uris=[_callback(stack, stack.port_a), _callback(stack, stack.port_b)],
    )
    set_client = await admin_a.put(
        f"/api/connectors/providers/{provider_id}/client",
        json={"client_id": "hand-app", "client_secret": "hand-secret", "issuer": server.issuer},
    )
    assert set_client["provider"]["client_source"] == "manual"

    alias = uniq("a")
    await _connect(editor, admin_b.with_token(editor_token), provider_id, alias)
    assert server.counters().registrations == []
    assert server.counters().authorizations[-1]["client_id"] == "hand-app"
    tool = f"{_title(provider_id, alias)}_ping"
    await _served(stack, tool, ports=(stack.port_a,))
    assert _ok(await _call(stack, tool)) == {"ok": "pong"}
    assert (await _catalog(admin_a))[provider_id]["client_source"] == "manual"


# -- 5. an unchallenged initialize -----------------------------------------------------------------


async def test_a_server_that_answers_initialize_without_a_token_is_still_discovered(
    stack: TaiStack, server: OAuthMcpServer, admin_a: ApiClient, uniq: Callable[[str], str]
) -> None:
    server.set(unchallenged_initialize=True)
    provider_id, alias = uniq("srv"), uniq("a")
    looked_up = await admin_a.post("/api/connectors/providers/discover", json={"url": server.mcp_url})
    assert looked_up["issuer"] == server.issuer
    await _add(admin_a, server.mcp_url, provider_id)
    await _connect(admin_a, admin_a, provider_id, alias)
    tool = f"{_title(provider_id, alias)}_ping"
    await _served(stack, tool, ports=(stack.port_a,))
    assert _ok(await _call(stack, tool)) == {"ok": "pong"}


# -- 6. remove -------------------------------------------------------------------------------------


async def test_remove_waits_for_disconnect_then_frees_the_id_and_purges_a_lapsed_connection(
    stack: TaiStack, server: OAuthMcpServer, admin_a: ApiClient, admin_b: ApiClient, uniq: Callable[[str], str]
) -> None:
    provider_id = uniq("srv")
    await _add(admin_a, server.mcp_url, provider_id)
    connection_id = await _connect(admin_a, admin_a, provider_id, uniq("a"))

    status, refused = await _status(admin_a, "DELETE", f"/api/connectors/providers/{provider_id}")
    assert status == 409, refused
    assert "1 live connection" in refused["error"]

    revocations = len(server.counters().revocations)
    await admin_a.delete(f"/api/connectors/connections/{connection_id}", retry_on_reloading=True)
    assert len(server.counters().revocations) == revocations + 1
    removed = await admin_a.delete(f"/api/connectors/providers/{provider_id}", retry_on_reloading=True)
    assert removed["removed_clients"] == 1
    for api in (admin_a, admin_b):

        async def gone(api: ApiClient = api) -> bool:
            return provider_id not in await _catalog(api)

        await wait_for_async(gone, deadline=_BIND_DEADLINE, message=f"{provider_id} still listed")

    # The id is free again; a new client is registered for the re-added provider.
    await _add(admin_a, server.mcp_url, provider_id)
    lapsed = await _connect(admin_a, admin_a, provider_id, uniq("a"))
    assert len(server.counters().registrations) == 2

    with _pg(stack) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE connector_connections SET session_expires_at = now() - interval '1 hour' WHERE connection_id = %s",
            (lapsed,),
        )
        conn.commit()
    await admin_a.delete(f"/api/connectors/providers/{provider_id}", retry_on_reloading=True)
    with _pg(stack) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM connector_connections WHERE connection_id = %s", (lapsed,))
        assert cur.fetchone() == (0,)


# -- 7. a hand-written discovered descriptor with an env client ------------------------------------


async def test_a_hand_written_discovered_descriptor_with_an_env_client(
    stack: TaiStack, oauth_mcp_env_server: OAuthMcpServer, admin_a: ApiClient, uniq: Callable[[str], str]
) -> None:
    oauth_mcp_env_server.register_by_hand(
        DISCOVERED_ENV_CLIENT_ID,
        secret=DISCOVERED_ENV_CLIENT_SECRET,
        method="client_secret_post",
        redirect_uris=[_callback(stack, stack.port_a), _callback(stack, stack.port_b)],
    )
    entry = (await _catalog(admin_a))[DISCOVERED_ENV_PROVIDER_ID]
    assert (entry["removable"], entry["client_source"]) == (False, "env")
    alias = uniq("a")
    await _connect(admin_a, admin_a, DISCOVERED_ENV_PROVIDER_ID, alias)
    tool = f"{_title(DISCOVERED_ENV_PROVIDER_ID, alias)}_ping"
    await _served(stack, tool, ports=(stack.port_a,))
    assert _ok(await _call(stack, tool)) == {"ok": "pong"}
    before = len(oauth_mcp_env_server.counters().refreshes)
    oauth_mcp_env_server.expire_access_tokens()
    assert _ok(await _call(stack, tool)) == {"ok": "pong"}
    assert [r["accepted"] for r in oauth_mcp_env_server.counters().refreshes[before:]] == [True]
    assert oauth_mcp_env_server.counters().registrations == []


# -- 8. a refused client ---------------------------------------------------------------------------


async def test_a_client_the_server_forgot_asks_for_a_reconnect_that_registers_again(
    stack: TaiStack, server: OAuthMcpServer, admin_a: ApiClient, uniq: Callable[[str], str]
) -> None:
    provider_id, alias = uniq("srv"), uniq("a")
    await _add(admin_a, server.mcp_url, provider_id)
    first = await _connect(admin_a, admin_a, provider_id, alias)
    second = await _connect(admin_a, admin_a, provider_id, uniq("b"))
    tool = f"{_title(provider_id, alias)}_ping"
    await _served(stack, tool, ports=(stack.port_a,))
    (registration,) = server.counters().registrations

    server.forget_client(registration["client_id"])
    server.expire_access_tokens()
    assert _connector_error(await _call(stack, tool))["code"] == "reconnect_required"

    await admin_a.delete(f"/api/connectors/connections/{second}", retry_on_reloading=True)
    status, _missing = await _status(admin_a, "GET", f"/api/connectors/connections/{second}")
    assert status == 404

    await _reconnect(admin_a, admin_a, first)
    assert len(server.counters().registrations) == 2
    # The entry that failed to list while its credential was refused is bound again by the
    # background re-probe.
    await _served(stack, tool, ports=(stack.port_a,))
    assert _ok(await _call(stack, tool)) == {"ok": "pong"}

    status, _refused = await _status(admin_a, "DELETE", f"/api/connectors/providers/{provider_id}")
    assert status == 409
    await admin_a.delete(f"/api/connectors/connections/{first}", retry_on_reloading=True)
    await admin_a.delete(f"/api/connectors/providers/{provider_id}", retry_on_reloading=True)


# -- 9. the URL guard ------------------------------------------------------------------------------


async def test_the_guard_refuses_a_private_address_and_a_private_token_endpoint(
    stack: TaiStack, server: OAuthMcpServer, admin_a: ApiClient, uniq: Callable[[str], str]
) -> None:
    status, refused = await _status(
        admin_a, "POST", "/api/connectors/providers/discover", {"url": "https://10.255.255.1/mcp"}
    )
    assert status == 400, refused
    assert "TAI_URL_GUARD_ALLOW_CIDRS" in refused["error"]

    server.set(token_endpoint="https://10.255.255.1/token")
    provider_id = uniq("srv")
    await _add(admin_a, server.mcp_url, provider_id)
    started = await _start(admin_a, provider_id, uniq("a"))
    status, completed = await _complete(admin_a, await _authorize(started["authorize_url"]))
    assert (status, completed["kind"]) == (400, "failed"), completed
    assert [r for r in server.counters().token_requests if r["grant_type"] == "authorization_code"] == []


# -- 10. a reload refreshes what it needs; a slow sign-in service holds nothing ---------------------


async def test_a_reload_refreshes_a_stale_credential_and_never_waits_on_a_slow_one(
    stack: TaiStack, server: OAuthMcpServer, admin_a: ApiClient, uniq: Callable[[str], str]
) -> None:
    provider_id, alias = uniq("srv"), uniq("a")
    await _add(admin_a, server.mcp_url, provider_id)
    connection_id = await _connect(admin_a, admin_a, provider_id, alias)
    tool = f"{_title(provider_id, alias)}_ping"
    await _served(stack, tool, ports=(stack.port_a,))

    # (a) The stored expiry is set back; the manifest write's reload refreshes before its build.
    _ok(await _call(stack, "e2e_age_connection_token", {"connection_id": connection_id}))
    before = len(server.counters().refreshes)
    started = time.monotonic()
    await _replace_manifest(admin_a, _manifest_document(stack))
    took_fast = time.monotonic() - started
    assert _ok(await _call(stack, tool)) == {"ok": "pong"}
    assert len(server.counters().refreshes) == before + 1

    # (b) A sign-in service that holds the refresh past the reload probe budget (3 s) — released by
    # the test once the write returned, and at most inside the refresh deadline (20 s) — holds
    # neither the write nor costs a rotation.
    server.set(refresh_delay=_REFRESH_DELAY)
    _ok(await _call(stack, "e2e_age_connection_token", {"connection_id": connection_id}))
    refreshes = len(server.counters().refreshes)
    started = time.monotonic()
    await _replace_manifest(admin_a, _manifest_document(stack))
    took = time.monotonic() - started
    assert any(r["grant_type"] == "refresh_token" for r in server.counters().token_requests[-1:]), (
        "no refresh reached the fixture during the write"
    )
    assert len(server.counters().refreshes) == refreshes, (
        f"the write waited for the delayed refresh ({took:.1f}s; with a prompt sign-in service {took_fast:.1f}s)"
    )
    assert took < _REFRESH_DELAY
    server.release_refreshes()

    async def answered() -> bool:
        return len(server.counters().refreshes) == refreshes + 1

    await wait_for_async(answered, deadline=_BIND_DEADLINE, message="the delayed refresh never completed")
    server.set(refresh_delay=0.0)
    view = await admin_a.get(f"/api/connectors/connections/{connection_id}")
    assert view["auth_health_state"] == "healthy"
    await _served(stack, tool, ports=(stack.port_a,))
    assert _ok(await _call(stack, tool)) == {"ok": "pong"}
    # The connection holds the rotated refresh token: the next refresh is accepted.
    server.expire_access_tokens()
    assert _ok(await _call(stack, tool)) == {"ok": "pong"}
    assert server.counters().refreshes[-1]["accepted"] is True
    assert len(server.counters().refreshes) == refreshes + 2


# -- 11. a credential goes only to its provider's server -------------------------------------------


async def test_a_connections_credential_goes_only_to_its_providers_server(
    stack: TaiStack,
    server: OAuthMcpServer,
    second_server: OAuthMcpServer,
    admin_a: ApiClient,
    editor_token: str,
    uniq: Callable[[str], str],
) -> None:
    provider_id, alias = uniq("srv"), uniq("a")
    await _add(admin_a, server.mcp_url, provider_id)
    connection_id = await _connect(admin_a, admin_a, provider_id, alias)
    own_tool = f"{_title(provider_id, alias)}_ping"
    await _served(stack, own_tool, ports=(stack.port_a,))

    second_server.set(anonymous_listing=True)
    forged = uniq("forged")
    editor = admin_a.with_token(editor_token)
    await editor.post(
        "/api/mcp-config/entries",
        json={
            "entries": [
                {
                    "title": forged,
                    "config": {"type": "http", "url": second_server.mcp_url},
                    "managed": {"connection_id": connection_id, "provider_id": provider_id, "sub_service": "default"},
                }
            ]
        },
        retry_on_reloading=True,
    )
    await _served(stack, f"{forged}_ping", ports=(stack.port_a,))
    refused = _connector_error(await _call(stack, f"{forged}_ping"))
    assert forged in refused["message"], refused
    assert all("authorization" not in request["headers"] for request in second_server.counters().mcp_requests)
    assert second_server.counters().tool_calls == {}
    assert _ok(await _call(stack, own_tool)) == {"ok": "pong"}

    # The provider leaves the manifest by another door; its connection record keeps the id taken.
    document = _manifest_document(stack)
    document["connectors"] = [c for c in document.get("connectors") or [] if c.get("id") != provider_id]
    await _replace_manifest(admin_a, document)
    status, taken = await _status(
        admin_a, "POST", "/api/connectors/providers", {"url": second_server.mcp_url, "id": provider_id}
    )
    assert status == 409, taken
    assert "connection record" in taken["error"]


# -- 12. the issuer check --------------------------------------------------------------------------


async def test_the_redirects_issuer_is_checked_before_any_token_request(
    stack: TaiStack,
    server: OAuthMcpServer,
    second_server: OAuthMcpServer,
    admin_a: ApiClient,
    uniq: Callable[[str], str],
) -> None:
    provider_id = uniq("srv")
    await _add(admin_a, server.mcp_url, provider_id)

    def exchanges(fixture: OAuthMcpServer) -> int:
        return len([r for r in fixture.counters().token_requests if r["grant_type"] == "authorization_code"])

    redirect = await _authorize((await _start(admin_a, provider_id, uniq("a")))["authorize_url"])
    assert redirect["iss"] == server.issuer
    status, completed = await _complete(admin_a, redirect)
    assert (status, completed["kind"]) == (200, "success")

    for control in ({"wrong_iss": True}, {"wrong_iss": False, "omit_iss": True}):
        server.set(**control)
        before = exchanges(server)
        redirect = await _authorize((await _start(admin_a, provider_id, uniq("a")))["authorize_url"])
        status, completed = await _complete(admin_a, redirect)
        assert (status, completed) == (400, {"kind": "failed", "reason": "IssuerMismatch"}), control
        assert exchanges(server) == before

    # A provider error carried with a wrong issuer is refused, and its text is never answered.
    server.set(omit_iss=False, wrong_iss=True)
    redirect = await _authorize((await _start(admin_a, provider_id, uniq("a")))["authorize_url"])
    status, answer = await _status(
        admin_a,
        "POST",
        "/api/connectors/oauth/complete",
        {"state": redirect["state"], "error": "access_denied: SENTINEL-provider-text", "iss": redirect["iss"]},
    )
    assert status == 400
    assert "SENTINEL-provider-text" not in json.dumps(answer)

    # A server that does not advertise the parameter but sends one: equal to the login's issuer
    # completes, another is refused.
    second_server.set(unadvertised_iss=True)
    other = uniq("srv")
    await _add(admin_a, second_server.mcp_url, other)
    redirect = await _authorize((await _start(admin_a, other, uniq("a")))["authorize_url"])
    status, completed = await _complete(admin_a, redirect)
    assert (status, completed["kind"]) == (200, "success")
    second_server.set(wrong_iss=True)
    before = exchanges(second_server)
    redirect = await _authorize((await _start(admin_a, other, uniq("a")))["authorize_url"])
    status, completed = await _complete(admin_a, redirect)
    assert (status, completed["kind"]) == (400, "failed")
    assert exchanges(second_server) == before


# -- 13. PKCE support is checked -------------------------------------------------------------------


async def test_a_server_without_pkce_is_refused_before_any_login(
    stack: TaiStack, server: OAuthMcpServer, admin_a: ApiClient, admin_b: ApiClient, uniq: Callable[[str], str]
) -> None:
    server.set(omit_pkce_methods=True)
    status, refused = await _status(admin_a, "POST", "/api/connectors/providers/discover", {"url": server.mcp_url})
    assert status == 400
    assert refused["step"] == "pkce-unsupported"
    assert "PKCE" in refused["error"]

    # Written by an administrator through the manifest, the provider cannot start a login either.
    provider_id = uniq("srv")
    document = _manifest_document(stack)
    document["connectors"] = [
        *(document.get("connectors") or []),
        {
            "id": provider_id,
            "display_name": "No PKCE",
            "kind": "oauth",
            "origin": "community",
            "category": "other",
            "sub_services": {
                "default": {
                    "id": "default",
                    "display_name": "Default",
                    "mcp_server": {"type": "http", "url": server.mcp_url},
                }
            },
        },
    ]
    await _replace_manifest(admin_a, document)
    status, refused = await _status(
        admin_b,
        "POST",
        "/api/connectors/connections/start",
        {"provider_id": provider_id, "alias": uniq("a"), "enabled_sub_services": ["default"]},
    )
    assert status == 400, refused
    assert "pkce-unsupported" in refused["error"]
    assert server.counters().authorizations == []


# -- 14. the client-identity document --------------------------------------------------------------


async def test_a_login_from_an_https_origin_uses_the_client_identity_document(
    stack: TaiStack, server: OAuthMcpServer, admin_a: ApiClient, uniq: Callable[[str], str]
) -> None:
    server.set(client_id_document=True)
    server.serve_documents_of(DOCUMENT_ORIGIN, address=stack.origin(stack.port_a), host=_DOCUMENT_HOST)
    provider_id, alias = uniq("srv"), uniq("a")
    await _add(admin_a, server.mcp_url, provider_id)
    document_url = f"{DOCUMENT_ORIGIN}/oauth/client-metadata.json"

    started = await _start(admin_a, provider_id, alias, origin=DOCUMENT_ORIGIN)
    redirect = await _authorize(started["authorize_url"])
    status, completed = await _complete(admin_a, redirect)
    assert (status, completed["kind"]) == (200, "success"), completed
    seen = server.counters()
    assert seen.registrations == []
    assert seen.authorizations[-1]["client_id"] == document_url
    assert seen.document_fetches == [{"client_id": document_url, "accepted": True}]
    (exchange,) = [r for r in seen.token_requests if r["grant_type"] == "authorization_code"]
    assert exchange["client_id"] == document_url
    tool = f"{_title(provider_id, alias)}_ping"
    await _served(stack, tool, ports=(stack.port_a,))
    assert _ok(await _call(stack, tool)) == {"ok": "pong"}
    server.expire_access_tokens()
    assert _ok(await _call(stack, tool)) == {"ok": "pong"}
    assert server.counters().refreshes[-1]["accepted"] is True

    # From the stack's plain-http origin the same provider registers a client instead.
    await _connect(admin_a, admin_a, provider_id, uniq("b"))
    assert [r["redirect_uris"] for r in server.counters().registrations] == [[_callback(stack, stack.port_a)]]

    # With the document turned off it is not served and the https login registers a client too.
    stack.restart_with_env({"CONNECTORS_CLIENT_ID_DOCUMENT": "false"})
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(f"{stack.origin(stack.port_a)}/oauth/client-metadata.json")
        assert response.status_code == 404
        started = await _start(admin_a, provider_id, uniq("c"), origin=DOCUMENT_ORIGIN)
        status, completed = await _complete(admin_a, await _authorize(started["authorize_url"]))
        assert (status, completed["kind"]) == (200, "success"), completed
        assert server.counters().registrations[-1]["redirect_uris"] == [f"{DOCUMENT_ORIGIN}/oauth-bridge.html"]
    finally:
        stack.restart_with_env({"CONNECTORS_CLIENT_ID_DOCUMENT": None})


# -- 15. a scope the token lacks -------------------------------------------------------------------


async def test_a_scope_the_token_lacks_is_answered_and_asked_for_at_the_next_reconnect(
    stack: TaiStack, server: OAuthMcpServer, admin_a: ApiClient, uniq: Callable[[str], str]
) -> None:
    server.set(scope_demands={"echo": "mcp.write"})
    provider_id, alias = uniq("srv"), uniq("a")
    await _add(admin_a, server.mcp_url, provider_id)
    connection_id = await _connect(admin_a, admin_a, provider_id, alias)
    first_scopes = set(server.counters().authorizations[-1].get("scope", "").split())
    echo = f"{_title(provider_id, alias)}_echo"
    await _served(stack, echo, ports=(stack.port_a,))

    refreshes = len(server.counters().refreshes)
    refused = _connector_error(await _call(stack, echo, {"payload": "x"}))
    assert (refused["code"], refused["scopes"]) == ("insufficient_scope", ["mcp.write"])
    assert server.counters().tool_calls["echo"] == 1
    assert len(server.counters().refreshes) == refreshes
    view = await admin_a.get(f"/api/connectors/connections/{connection_id}")
    assert (view["auth_health_state"], view["scopes_wanted"]) == ("healthy", ["mcp.write"])
    assert _ok(await _call(stack, f"{_title(provider_id, alias)}_ping")) == {"ok": "pong"}

    await _reconnect(admin_a, admin_a, connection_id)
    assert set(server.counters().authorizations[-1]["scope"].split()) == first_scopes | {"mcp.write"}
    assert _ok(await _call(stack, echo, {"payload": "y"})) == {"echoed": "y"}
    view = await admin_a.get(f"/api/connectors/connections/{connection_id}")
    assert view["scopes_wanted"] == []
