"""The MCP door, e2e: the 401 pointer, the protected-resource metadata documents, the sessions behind them.

An identity provider that declares an authorization server (the fixture provider, which
accepts one static token) and ``ACCESS_CONTROL_PUBLIC_BASE_URL`` turn the door on: a 401 from
an MCP endpoint names that endpoint's metadata document, which names the declared server and
the endpoint's public URL. With either half missing the door advertises nothing, and a
declaring provider with no public URL says so once in the boot log.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from tai42_e2e import Infra
from tai42_e2e.booting import boot_stack
from tai42_e2e.manifests import (
    MCP_DOOR_AUTHORIZATION_SERVER,
    MCP_DOOR_PUBLIC_BASE_URL,
    MCP_DOOR_STATIC_TOKEN,
    MCP_DOOR_USER_ID,
    build_mcp_door_stack,
    build_mcp_door_studio_routes_stack,
    build_mcp_door_without_provider_stack,
    build_mcp_door_without_public_url_stack,
)
from tai42_e2e.seeding import seed_principal_policy
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.needs(
    "kind:identity",
    "store:postgres",
    "store:redis",
    "setting:seeded-access-control",
    "setting:ACCESS_CONTROL_PUBLIC_BASE_URL",
)

DOC = "/.well-known/oauth-protected-resource"
MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
PROBE_TOOL = "e2e_echo"


def _pointer(endpoint: str) -> str:
    return f'Bearer resource_metadata="{MCP_DOOR_PUBLIC_BASE_URL}{DOC}{endpoint}"'


def _boot_door(infra: Infra, root: Path, builder) -> Iterator[TaiStack]:
    for stack in boot_stack(infra, root, builder, seed_auth=True):
        # The fixture provider's principal: a top-level admin policy, so its static token is admitted.
        seed_principal_policy(stack.resources, user_id=MCP_DOOR_USER_ID, scopes=["*"])
        yield stack


@pytest.fixture(scope="module")
def door_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    yield from _boot_door(infra, tmp_path_factory.mktemp("mcp-door"), build_mcp_door_stack)


@pytest.fixture(scope="module")
def door_without_public_url_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    yield from _boot_door(infra, tmp_path_factory.mktemp("mcp-door-no-url"), build_mcp_door_without_public_url_stack)


@pytest.fixture(scope="module")
def door_without_provider_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    yield from boot_stack(
        infra, tmp_path_factory.mktemp("mcp-door-no-provider"), build_mcp_door_without_provider_stack, seed_auth=True
    )


@pytest.fixture(scope="module")
def door_studio_routes_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    yield from _boot_door(infra, tmp_path_factory.mktemp("mcp-door-studio"), build_mcp_door_studio_routes_stack)


async def _register_sub_mcp(stack: TaiStack, slug: str, transport: str, port: int | None = None) -> None:
    await stack.api(port=port).post(
        "/api/sub-mcp", json={"slug": slug, "tools": [PROBE_TOOL], "transport": transport}, retry_on_reloading=True
    )


# -- 1. the pointer and the document --------------------------------------------------------


@pytest.mark.needs("probe-tools", "topology:replicas")
def test_an_unauthenticated_mcp_post_points_at_the_document_that_names_the_server(door_stack: TaiStack) -> None:
    with httpx.Client(base_url=door_stack.base_url(), timeout=30.0) as client:
        refused = client.post("/mcp", json=PING, headers=MCP_HEADERS)
        assert refused.status_code == 401, refused.text
        assert refused.headers["www-authenticate"] == _pointer("/mcp")
        document = client.get(f"{DOC}/mcp")
    assert document.status_code == 200, document.text
    assert document.json() == {
        "resource": f"{MCP_DOOR_PUBLIC_BASE_URL}/mcp",
        "authorization_servers": [MCP_DOOR_AUTHORIZATION_SERVER],
        "bearer_methods_supported": ["header"],
    }
    assert document.headers["cache-control"] == "no-store"


# -- 2. the declaring provider's token holds a session -----------------------------------------


@pytest.mark.needs("probe-tools", "topology:replicas")
async def test_the_static_token_lists_tools(door_stack: TaiStack) -> None:
    async with door_stack.mcp(auth=MCP_DOOR_STATIC_TOKEN) as mcp:
        assert PROBE_TOOL in await mcp.tool_names()


# -- 3. a slug registered on another replica --------------------------------------------------


@pytest.mark.needs("probe-tools", "mutable", "topology:replicas", "setting:SUB_MCP_REDIS_URL")
async def test_a_slug_registered_on_a_gets_the_pointer_and_its_document_on_b(
    door_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    slug = uniq("door").replace("_", "-")
    await _register_sub_mcp(door_stack, slug, "http", port=door_stack.port_a)
    with httpx.Client(base_url=door_stack.base_url(door_stack.port_b), timeout=30.0) as client:
        refused = client.post(f"/app/{slug}", json=PING, headers=MCP_HEADERS)
        document = client.get(f"{DOC}/app/{slug}")
    assert refused.status_code == 401, refused.text
    assert refused.headers["www-authenticate"] == _pointer(f"/app/{slug}")
    assert document.status_code == 200, document.text
    assert document.json()["resource"] == f"{MCP_DOOR_PUBLIC_BASE_URL}/app/{slug}"


# -- 4. an API key on /mcp; an /api 401 -------------------------------------------------------


@pytest.mark.needs("probe-tools", "topology:replicas")
async def test_an_api_key_still_works_on_mcp_and_an_api_401_has_no_pointer(door_stack: TaiStack) -> None:
    async with door_stack.mcp(auth=door_stack.auth_token) as mcp:
        assert PROBE_TOOL in await mcp.tool_names()
    with httpx.Client(base_url=door_stack.base_url(), timeout=30.0) as client:
        refused = client.get("/api/auth/me")
    assert refused.status_code == 401, refused.text
    assert "www-authenticate" not in refused.headers


# -- 5. no declaring provider ---------------------------------------------------------------


def test_without_a_declaring_provider_nothing_is_advertised(door_without_provider_stack: TaiStack) -> None:
    with httpx.Client(base_url=door_without_provider_stack.base_url(), timeout=30.0) as client:
        refused = client.post("/mcp", json=PING, headers=MCP_HEADERS)
        document = client.get(f"{DOC}/mcp")
    assert refused.status_code == 401, refused.text
    assert "resource_metadata" not in refused.headers.get("www-authenticate", "")
    assert document.status_code == 404


# -- 6. a declaring provider and no public URL --------------------------------------------------


def test_without_a_public_url_nothing_is_advertised_and_the_boot_log_says_so(
    door_without_public_url_stack: TaiStack,
) -> None:
    with httpx.Client(base_url=door_without_public_url_stack.base_url(), timeout=30.0) as client:
        refused = client.post("/mcp", json=PING, headers=MCP_HEADERS)
        document = client.get(f"{DOC}/mcp")
    assert refused.status_code == 401, refused.text
    assert "resource_metadata" not in refused.headers.get("www-authenticate", "")
    assert document.status_code == 404
    boot_logs = door_without_public_url_stack.process("serve").log_path.read_text(encoding="utf-8", errors="replace")
    warnings = [
        line
        for line in boot_logs.splitlines()
        if "WARNING" in line and "ACCESS_CONTROL_PUBLIC_BASE_URL" in line and "door-static" in line
    ]
    assert len(warnings) == 1, warnings


# -- 7. an SSE sub-MCP app --------------------------------------------------------------------


@pytest.mark.needs("probe-tools", "mutable", "topology:replicas")
async def test_an_sse_sub_mcp_app_holds_a_session_with_the_static_token(
    door_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    slug = uniq("door-sse").replace("_", "-")
    await _register_sub_mcp(door_stack, slug, "sse")
    async with door_stack.mcp(path=f"/app/{slug}/sse", auth=MCP_DOOR_STATIC_TOKEN) as mcp:
        assert PROBE_TOOL in await mcp.tool_names()


# -- 8. a deployment that serves the Studio routes ----------------------------------------------


@pytest.mark.needs("probe-tools", "mutable")
async def test_with_the_studio_routes_the_sub_mcp_stream_is_guarded_and_holds_a_session(
    door_studio_routes_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    stack = door_studio_routes_stack
    slug = uniq("door-studio").replace("_", "-")
    await _register_sub_mcp(stack, slug, "sse")
    with httpx.Client(base_url=stack.base_url(), timeout=30.0) as client:
        refused = client.get(f"/app/{slug}/sse", headers={"Accept": "text/event-stream"})
    assert refused.status_code == 401, refused.text
    assert refused.headers["www-authenticate"] == _pointer(f"/app/{slug}/sse")
    async with stack.mcp(path=f"/app/{slug}/sse", auth=MCP_DOOR_STATIC_TOKEN) as mcp:
        assert PROBE_TOOL in await mcp.tool_names()
