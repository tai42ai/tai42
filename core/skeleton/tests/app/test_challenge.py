"""The ``WWW-Authenticate`` pointer on a 401 from an MCP endpoint, over the real access-control stack.

The served app is the real streamable-HTTP app with the real adapter stack (the credential
backend, the resource guard, the MCP library's inner gate) over in-memory stores, an
identity provider that declares an authorization server, and an admin, editor and viewer
key. One case per identity kind the core decides; then the header rewrite itself, driven
directly: an existing challenge gains the pointer, one that carries it is untouched, and
nothing but the 401 of an MCP endpoint is ever touched.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from starlette.testclient import TestClient
from starlette.types import Message, Scope
from tai42_contract.app import tai42_app

from tai42_skeleton.access_control import adapter as adapter_module
from tai42_skeleton.access_control.challenge import McpChallengeMiddleware
from tai42_skeleton.app.instance import app

from ..access_control.conftest import _FakeApp
from ._door_fixtures import (
    PUBLIC_BASE_URL,
    STATIC_TOKEN,
    STATIC_USER,
    DeclaringTokenProvider,
    fresh_serving_core,
    public_base_url,
    register_declaring_provider,
)
from ._served_app import ADMIN_KEY, VIEWER_KEY, install_access_control

pytestmark = pytest.mark.filterwarnings("ignore::async_lru.AlruCacheLoopResetWarning")

POINTER = f'Bearer resource_metadata="{PUBLIC_BASE_URL}/.well-known/oauth-protected-resource/mcp"'
MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
}


@pytest.fixture(autouse=True)
def _stack(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    access = install_access_control(monkeypatch)
    access.pg.add_policy(STATIC_USER, scopes=["*"])
    register_declaring_provider(DeclaringTokenProvider())
    fresh_serving_core(monkeypatch)
    with tai42_app.bound(_FakeApp()):
        yield


def _client() -> TestClient:
    return TestClient(app.http_app(), base_url="http://internal-host:8000")


def _post(client: TestClient, path: str = "/mcp", **headers: str):
    return client.post(path, json=INITIALIZE, headers={**MCP_HEADERS, **headers})


def test_no_credential_on_mcp_gets_the_pointer(monkeypatch: pytest.MonkeyPatch) -> None:
    with public_base_url(monkeypatch, PUBLIC_BASE_URL), _client() as client:
        response = _post(client)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == POINTER


def test_an_invalid_credential_on_mcp_gets_the_pointer(monkeypatch: pytest.MonkeyPatch) -> None:
    with public_base_url(monkeypatch, PUBLIC_BASE_URL), _client() as client:
        response = _post(client, Authorization="Bearer not-a-valid-key")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == POINTER


def test_a_key_sent_only_as_x_api_key_on_mcp_gets_the_pointer_on_the_inner_gate_challenge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The outer gate admits the key; the MCP library's inner gate refuses a request with no
    # ``Authorization`` header with a bare ``Bearer`` challenge, which gains the pointer.
    with public_base_url(monkeypatch, PUBLIC_BASE_URL), _client() as client:
        response = _post(client, **{"X-Api-Key": ADMIN_KEY})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == POINTER


def test_a_key_sent_only_as_x_api_key_under_the_sub_mcp_mount_is_admitted(monkeypatch: pytest.MonkeyPatch) -> None:
    # Sub-MCP apps carry no inner gate: the outer gate admits the key and the router answers.
    with public_base_url(monkeypatch, PUBLIC_BASE_URL), _client() as client:
        response = _post(client, "/app/never-registered", **{"X-Api-Key": ADMIN_KEY})
    assert response.status_code == 404
    assert "www-authenticate" not in response.headers


def test_the_static_token_of_the_declaring_provider_is_admitted(monkeypatch: pytest.MonkeyPatch) -> None:
    # Admitted by both gates: the transport itself answers the session-less message.
    with public_base_url(monkeypatch, PUBLIC_BASE_URL), _client() as client:
        response = client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={**MCP_HEADERS, **_bearer()}
        )
    assert response.status_code == 400, response.text
    assert "www-authenticate" not in response.headers


def _bearer() -> dict[str, str]:
    return {"Authorization": f"Bearer {STATIC_TOKEN}"}


def test_a_viewer_post_is_refused_403_with_no_challenge(monkeypatch: pytest.MonkeyPatch) -> None:
    with public_base_url(monkeypatch, PUBLIC_BASE_URL), _client() as client:
        response = _post(client, Authorization=f"Bearer {VIEWER_KEY}")
    assert response.status_code == 403
    assert "www-authenticate" not in response.headers


def test_a_401_on_an_api_path_carries_no_pointer(monkeypatch: pytest.MonkeyPatch) -> None:
    with public_base_url(monkeypatch, PUBLIC_BASE_URL), _client() as client:
        response = client.get("/api/tools")
    assert response.status_code == 401
    assert "www-authenticate" not in response.headers


def test_the_pointer_is_built_from_the_public_url_whatever_the_request_host_and_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with public_base_url(monkeypatch, PUBLIC_BASE_URL), _client() as client:
        response = _post(client, Host="proxy-facing.example", **{"X-Forwarded-Proto": "http"})
    assert response.headers["www-authenticate"] == POINTER


def test_the_pointer_carries_the_prefix_of_an_embedded_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    with public_base_url(monkeypatch, "https://host.example/tenant"), _client() as client:
        response = _post(client)
    assert response.headers["www-authenticate"] == (
        'Bearer resource_metadata="https://host.example/tenant/.well-known/oauth-protected-resource/mcp"'
    )


def _without_challenge(monkeypatch: pytest.MonkeyPatch) -> None:
    class _PassThrough:
        def __init__(self, app_: object) -> None:
            self.app = app_

        async def __call__(self, scope, receive, send) -> None:
            await self.app(scope, receive, send)  # type: ignore[operator]

    monkeypatch.setattr(adapter_module, "McpChallengeMiddleware", _PassThrough)
    fresh_serving_core(monkeypatch)


def _recorded(response) -> tuple[int, list[tuple[str, str]], bytes]:
    headers = sorted((name, value) for name, value in response.headers.items() if name != "date")
    return response.status_code, headers, response.content


@pytest.mark.parametrize("case", ["no-credential", "invalid-credential", "x-api-key-only"])
def test_with_nothing_advertised_the_response_is_byte_identical_to_one_without_the_middleware(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    headers = {
        "no-credential": {},
        "invalid-credential": {"Authorization": "Bearer not-a-valid-key"},
        "x-api-key-only": {"X-Api-Key": ADMIN_KEY},
    }[case]
    with public_base_url(monkeypatch, None):
        with _client() as client:
            with_middleware = _recorded(_post(client, **headers))
        _without_challenge(monkeypatch)
        with TestClient(app.http_app(), base_url="http://internal-host:8000") as client:
            without_middleware = _recorded(_post(client, **headers))
    assert with_middleware == without_middleware


def test_the_recording_without_the_middleware_has_no_pointer_even_when_advertised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The control for the byte-identical comparison: the stack built without the middleware
    # really runs without it.
    with public_base_url(monkeypatch, PUBLIC_BASE_URL):
        _without_challenge(monkeypatch)
        with TestClient(app.http_app(), base_url="http://internal-host:8000") as client:
            response = _post(client)
    assert response.status_code == 401
    assert "www-authenticate" not in response.headers


def test_status_and_body_never_change_when_the_pointer_is_added(monkeypatch: pytest.MonkeyPatch) -> None:
    with public_base_url(monkeypatch, None), _client() as client:
        plain = _post(client)
    with public_base_url(monkeypatch, PUBLIC_BASE_URL), _client() as client:
        pointed = _post(client)
    assert (plain.status_code, plain.content) == (pointed.status_code, pointed.content)
    assert "www-authenticate" not in plain.headers
    assert pointed.headers["www-authenticate"] == POINTER


# -- the header rewrite, driven directly ----------------------------------------------


async def _through_challenge(
    path: str, status: int, headers: list[tuple[bytes, bytes]], scope_type: str = "http"
) -> list[Message]:
    async def inner(scope, receive, send) -> None:
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": b"refused"})

    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {"type": scope_type, "path": path, "raw_path": path.encode(), "root_path": "", "headers": []}
    await McpChallengeMiddleware(inner)(scope, receive, send)
    return sent


def _www_authenticate(sent: list[Message]) -> list[str]:
    return [value.decode() for name, value in sent[0]["headers"] if name.lower() == b"www-authenticate"]


@pytest.fixture
def _advertised(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    app.http_app()  # records the transport surfaces
    with public_base_url(monkeypatch, PUBLIC_BASE_URL):
        yield


@pytest.mark.usefixtures("_advertised")
@pytest.mark.parametrize(
    ("existing", "expected"),
    [
        ([], [POINTER]),
        ([(b"www-authenticate", b"Bearer")], [POINTER]),
        (
            [(b"WWW-Authenticate", b'Bearer error="invalid_token", error_description="expired"')],
            [f'Bearer error="invalid_token", error_description="expired", {POINTER.removeprefix("Bearer ")}'],
        ),
        (
            [(b"www-authenticate", b'Bearer resource_metadata="https://elsewhere.example/doc"')],
            ['Bearer resource_metadata="https://elsewhere.example/doc"'],
        ),
        ([(b"www-authenticate", b'Basic realm="x"')], ['Basic realm="x"', POINTER]),
    ],
)
async def test_the_rewrite_guarantees_one_pointer(existing, expected) -> None:
    sent = await _through_challenge("/mcp", 401, existing)
    assert _www_authenticate(sent) == expected
    assert sent[0]["status"] == 401
    assert sent[1]["body"] == b"refused"


@pytest.mark.usefixtures("_advertised")
async def test_a_non_401_response_and_a_non_mcp_401_are_untouched() -> None:
    assert _www_authenticate(await _through_challenge("/mcp", 403, [])) == []
    assert _www_authenticate(await _through_challenge("/mcp", 200, [])) == []
    assert _www_authenticate(await _through_challenge("/api/tools", 401, [])) == []


@pytest.mark.usefixtures("_advertised")
async def test_a_malformed_path_gets_no_header() -> None:
    assert _www_authenticate(await _through_challenge("/mcp/%00", 401, [])) == []


@pytest.mark.usefixtures("_advertised")
async def test_a_websocket_scope_passes_through_untouched() -> None:
    seen: list[Scope] = []

    async def inner(scope, receive, send) -> None:
        seen.append(scope)

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        raise AssertionError("nothing is sent")

    await McpChallengeMiddleware(inner)({"type": "websocket", "path": "/mcp"}, receive, send)
    assert len(seen) == 1


def test_the_challenge_is_the_outermost_member_of_the_four_member_stack() -> None:
    from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
    from starlette.middleware.authentication import AuthenticationMiddleware

    from tai42_skeleton.access_control.adapter import AuthAdapter
    from tai42_skeleton.access_control.middleware import ResourceGuardMiddleware
    from tai42_skeleton.access_control.settings import AccessControlSettings

    stack = [middleware.cls for middleware in AuthAdapter(AccessControlSettings(enable=True)).get_middleware()]
    assert stack == [McpChallengeMiddleware, AuthenticationMiddleware, AuthContextMiddleware, ResourceGuardMiddleware]
