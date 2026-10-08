"""The SSE transport's message endpoint is a credential-gated served surface at its bare prefix.

An SSE client posts to ``<message_path>?session_id=...``; the canonical form of that path
drops the trailing slash, so the bare prefix must resolve as the transport's served,
authenticated surface: an unauthenticated caller is refused 401 (never "route not
configured"), and an authenticated non-admin holder passes the access-control gate (the
transport itself then answers for the unknown session). Driven over the real ASGI apps the
server builds, with the real access-control middleware chain in front.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from starlette.testclient import TestClient
from starlette.types import ASGIApp
from tai42_contract.access_control import OWNER_USER_ID_CLAIM
from tai42_contract.app import tai42_app
from tai42_identity_redis import redis_api_key_provider as provider_module
from tai42_identity_redis.settings import redis_identity_settings
from tai42_kit.utils.data.string_util import hash_api_key

from tai42_skeleton.access_control import policy as policy_module
from tai42_skeleton.access_control import store as store_module
from tai42_skeleton.access_control.roles import editor_jq
from tai42_skeleton.app.instance import app

from ..access_control.conftest import FakeAccessControlPg, FakeRedis, _FakeApp, make_client_ctx, make_pg_ctx

# The enforcer's alru cache is created in the test's loop and first used in the
# TestClient's portal loop — a benign loop-change reset that is a test artifact.
pytestmark = pytest.mark.filterwarnings("ignore::async_lru.AlruCacheLoopResetWarning")

_EDITOR_KEY = "editor-raw-key"


@pytest.fixture
def _access_control(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """An editor-owned key (a non-admin holder) and no route rows: the bare message prefix
    resolves only through the transport's own record."""
    identity_key = f"{redis_identity_settings().key_prefix}{hash_api_key(_EDITOR_KEY)}"
    fake = FakeRedis(hashes={identity_key: {"user_id": "ed-key", "description": "d", "owner_user_id": "ed-owner"}})
    # The policy store resolves its Postgres through the registry; a fake transport models a
    # configured deployment, so the default database must be on.
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "test")
    pg = FakeAccessControlPg()
    pg.add_policy("ed-key", scopes=["*"], policy_data={OWNER_USER_ID_CLAIM: "ed-owner"})
    pg.add_policy("ed-owner", scopes=["*"], condition={"content": editor_jq()})
    ctx = make_client_ctx(fake)
    monkeypatch.setattr(policy_module, "client_ctx", ctx)
    monkeypatch.setattr(provider_module, "client_ctx", ctx)
    monkeypatch.setattr(store_module, "client_ctx", make_pg_ctx(pg))
    with tai42_app.bound(_FakeApp()):
        yield


def _sse_app() -> ASGIApp:
    return app.sse_app()


def _http_sse_app() -> ASGIApp:
    return app.http_app(transport="sse")


@pytest.mark.usefixtures("_access_control")
@pytest.mark.parametrize("build", [_sse_app, _http_sse_app], ids=["sse_app", "http_app-sse"])
@pytest.mark.parametrize("path", ["/messages?session_id=x", "/messages/?session_id=x"])
def test_unauthenticated_post_to_the_message_endpoint_is_refused_401(build: Callable[[], ASGIApp], path: str) -> None:
    with TestClient(build()) as client:
        response = client.post(path, json={})
    assert response.status_code == 401, response.text


@pytest.mark.usefixtures("_access_control")
@pytest.mark.parametrize("build", [_sse_app, _http_sse_app], ids=["sse_app", "http_app-sse"])
@pytest.mark.parametrize("path", ["/messages?session_id=x", "/messages/?session_id=x"])
def test_an_editor_post_to_the_message_endpoint_passes_the_gate(build: Callable[[], ASGIApp], path: str) -> None:
    with TestClient(build()) as client:
        response = client.post(path, json={}, headers={"Authorization": f"Bearer {_EDITOR_KEY}"})
    assert response.status_code not in (401, 403), response.text
