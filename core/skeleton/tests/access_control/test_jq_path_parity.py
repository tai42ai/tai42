"""A policy condition reads ONE ``.request.path`` — the canonical, root-stripped path — on every door.

The same target reached over HTTP (any spelling of it: a trailing slash, dot segments, an
encoded ``?``/space, a mount prefix) and as a tool dispatch hands the jq condition the same
path, so a condition written against the canonical path answers one verdict at both doors.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastmcp.server.auth import AccessToken
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.responses import PlainTextResponse
from starlette.types import Message
from tai42_contract.app import tai42_app

from tai42_skeleton.access_control import policy as policy_module
from tai42_skeleton.access_control import store as store_module
from tai42_skeleton.access_control.adapter import handle_auth_error
from tai42_skeleton.access_control.backend import AccessControlAuthBackend
from tai42_skeleton.access_control.middleware import ResourceGuardMiddleware
from tai42_skeleton.access_control.settings import AccessControlSettings
from tai42_skeleton.access_control.verifier import AccessControlVerifier
from tai42_skeleton.authz import check
from tai42_skeleton.authz.identity import CallerIdentity
from tai42_skeleton.operations import OperationRegistry, operation
from tai42_skeleton.operations.errors import PermissionDeniedError

from ..authz.conftest import _FakeApp, _recorded_routes
from .conftest import FakeAccessControlPg, FakeRedis, make_client_ctx, make_pg_ctx

pytestmark = pytest.mark.filterwarnings("ignore::async_lru.AlruCacheLoopResetWarning")

_TEMPLATE = "/api/things/{thing}/inspect"


class _TokenVerifier:
    async def verify_token(self, token: str) -> AccessToken | None:
        return AccessToken(token=token, client_id="u1", scopes=[], claims={}) if token == "tok" else None


@pytest.fixture
def parity_pg(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeAccessControlPg]:
    pg = FakeAccessControlPg()
    redis = FakeRedis()
    monkeypatch.setattr(store_module, "client_ctx", make_pg_ctx(pg))
    monkeypatch.setattr(policy_module, "client_ctx", make_client_ctx(redis))
    with _recorded_routes((_TEMPLATE,), "write"), tai42_app.bound(_FakeApp()):
        yield pg


async def _http_status(settings: AccessControlSettings, raw_target: str, root_path: str) -> int:
    async def endpoint(scope, receive, send) -> None:
        await PlainTextResponse("ok")(scope, receive, send)

    guard = ResourceGuardMiddleware(
        endpoint,
        AccessControlVerifier(settings, providers=[]),
        settings.public_resource_id,
        settings.authenticated_always_allowed_paths,
    )
    app = AuthenticationMiddleware(
        guard,
        backend=AccessControlAuthBackend(cast("AccessControlVerifier", _TokenVerifier()), settings),
        on_error=handle_auth_error,
    )
    scope: dict[str, Any] = {
        "type": "http",
        "method": "POST",
        "path": raw_target.replace("%3F", "?").replace("%20", " "),
        "raw_path": raw_target.encode("ascii"),
        "root_path": root_path,
        "query_string": b"",
        "headers": [(b"authorization", b"Bearer tok")],
    }
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(dict(message))

    await app(scope, receive, send)
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


async def _tool_edge_admits(settings: AccessControlSettings, thing: str) -> bool:
    reg = OperationRegistry()

    @operation(name="inspect_thing", summary="Inspect", tags=["things"], registry=reg)
    async def inspect_thing(**_: Any) -> dict:
        return {}

    meta = reg.get("inspect_thing")
    meta.route_template = _TEMPLATE
    meta.http_method = "POST"
    identity = CallerIdentity(user_id="u1", claims={}, effective_scopes=("*",))
    try:
        await check(identity, meta, {"thing": thing}, settings=settings)
    except PermissionDeniedError:
        return False
    return True


@pytest.mark.parametrize(
    ("raw_target", "root_path", "thing", "canonical"),
    [
        ("/api/things/t1/inspect", "", "t1", "/api/things/t1/inspect"),
        ("/api/things/t1/inspect/", "", "t1", "/api/things/t1/inspect"),
        ("/api/./things//t1/inspect", "", "t1", "/api/things/t1/inspect"),
        ("/api/things/a%3Fb/inspect", "", "a?b", "/api/things/a?b/inspect"),
        ("/api/things/a%20b/inspect", "", "a b", "/api/things/a b/inspect"),
        ("/mnt/api/things/t1/inspect", "/mnt", "t1", "/api/things/t1/inspect"),
    ],
    ids=["plain", "trailing-slash", "dot-and-double-slash", "encoded-question-mark", "encoded-space", "root-path"],
)
async def test_both_doors_read_the_canonical_path(
    parity_pg: FakeAccessControlPg, raw_target: str, root_path: str, thing: str, canonical: str
) -> None:
    settings = AccessControlSettings()
    parity_pg.add_policy("u1", scopes=["*"], condition={"content": f'.request.path == "{canonical}"'})
    assert await _http_status(settings, raw_target, root_path) == 200
    assert await _tool_edge_admits(settings, thing) is True


@pytest.mark.parametrize(
    ("raw_target", "thing"),
    [("/api/things/t1/inspect/", "t1"), ("/mnt/api/things/t1/inspect", "t1")],
    ids=["trailing-slash", "root-path"],
)
async def test_a_condition_on_a_non_canonical_spelling_denies_at_both_doors(
    parity_pg: FakeAccessControlPg, raw_target: str, thing: str
) -> None:
    settings = AccessControlSettings()
    root_path = "/mnt" if raw_target.startswith("/mnt") else ""
    parity_pg.add_policy("u1", scopes=["*"], condition={"content": f'.request.path == "{raw_target}"'})
    assert await _http_status(settings, raw_target, root_path) == 403
    assert await _tool_edge_admits(settings, thing) is False


async def test_an_encoded_slash_denies_at_both_doors(parity_pg: FakeAccessControlPg) -> None:
    # The router decodes ``%2F`` into a separator, so a non-raw route never serves the
    # one-segment form the canonical path keeps; both doors refuse it before any condition.
    settings = AccessControlSettings()
    parity_pg.add_policy("u1", scopes=["*"], condition={"content": "true"})
    assert await _http_status(settings, "/api/things/a%2Fb/inspect", "") == 403
    assert await _tool_edge_admits(settings, "a/b") is False
