"""The policy version is read once per request's access-control decision.

The access-control stack opens a request-scoped memo before authentication; the auth
backend and the resource guard share the one version read; the guard closes the memo as
it hands the request to the app, so everything the endpoint does reads the version live.
"""

from __future__ import annotations

import asyncio

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from tai42_contract.access_control import OWNER_USER_ID_CLAIM
from tai42_identity_redis import redis_api_key_provider as provider_module
from tai42_identity_redis.settings import redis_identity_settings
from tai42_kit.utils.data.string_util import hash_api_key

from tai42_skeleton.access_control import management as management_module
from tai42_skeleton.access_control import policy as policy_module
from tai42_skeleton.access_control import store as store_module
from tai42_skeleton.access_control.adapter import AuthAdapter
from tai42_skeleton.access_control.middleware import PolicyVersionScopeMiddleware
from tai42_skeleton.access_control.policy import policy_enforcer
from tai42_skeleton.access_control.policy_version_scope import close_policy_version_scope, policy_version_scope
from tai42_skeleton.access_control.settings import AccessControlSettings, access_control_settings

from .conftest import FakeAccessControlPg, FakeRedis, _FakeApp, make_client_ctx, make_pg_ctx

# The enforcer's alru cache is created in the test's loop and first used in the
# TestClient's separate portal loop — a benign loop-change reset that is a test
# artifact (the real server holds one loop), not a product warning.
pytestmark = pytest.mark.filterwarnings("ignore::async_lru.AlruCacheLoopResetWarning")

_KEY = "things-key"
_SCOPE = "things-scope"


class _CountingRedis(FakeRedis):
    """A fake Redis that counts the GETs of the policy-version key and can fail them on demand."""

    def __init__(self, version_key: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.version_key = version_key
        self.version_gets = 0
        self.fail_version_get: Exception | None = None

    async def get(self, key):
        if key == self.version_key:
            self.version_gets += 1
            if self.fail_version_get is not None:
                raise self.fail_version_get
        return await super().get(key)


@pytest.fixture
def bound_app():
    """Bind a fake ``tai42_app`` so the enforcer can render the (empty) condition."""
    from tai42_contract.app import tai42_app

    app = _FakeApp()
    with tai42_app.bound(app):
        yield app


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _CountingRedis:
    """A counting fake Redis behind every client the policy read, the bump and the identity provider open."""
    settings = access_control_settings()
    identity_key = f"{redis_identity_settings().key_prefix}{hash_api_key(_KEY)}"
    redis = _CountingRedis(
        settings.policy_version_key,
        strings={settings.policy_version_key: "3"},
        hashes={identity_key: {"user_id": "u1", "description": "d", "owner_user_id": "owner1"}},
    )
    ctx = make_client_ctx(redis)
    monkeypatch.setattr(policy_module, "client_ctx", ctx)
    monkeypatch.setattr(management_module, "client_ctx", ctx)
    monkeypatch.setattr(provider_module, "client_ctx", ctx)
    return redis


def _stack_client(monkeypatch: pytest.MonkeyPatch, fake: _CountingRedis, seen: dict[str, int]) -> TestClient:
    """A test app behind the real access-control middleware stack; its endpoint reads the version itself."""
    pg = FakeAccessControlPg()
    pg.add_route("/things", _SCOPE)
    pg.add_policy("u1", scopes=[_SCOPE], policy_data={OWNER_USER_ID_CLAIM: "owner1"})
    pg.add_policy("owner1", scopes=["*"])
    monkeypatch.setattr(store_module, "client_ctx", make_pg_ctx(pg))
    settings = access_control_settings()

    async def things(request):
        seen["decision_gets"] = fake.version_gets
        # Another worker bumps the version while this request is in its endpoint.
        fake._strings[settings.policy_version_key] = "9"
        seen["endpoint_version"] = await policy_enforcer(settings).current_policy_version()
        seen["after_endpoint_read_gets"] = fake.version_gets
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[Route("/things", things)],
        middleware=AuthAdapter(AccessControlSettings()).get_middleware(),
    )
    return TestClient(app)


def test_the_access_control_decision_reads_the_version_once_and_the_endpoint_reads_live(
    monkeypatch: pytest.MonkeyPatch, fake: _CountingRedis, bound_app
) -> None:
    seen: dict[str, int] = {}
    client = _stack_client(monkeypatch, fake, seen)

    response = client.get("/things", headers={"X-Api-Key": _KEY})

    assert response.status_code == 200
    # The auth backend and the resource guard both decided on the version: one GET between them.
    assert seen["decision_gets"] == 1
    # After the guard handed the request over, the endpoint's read went to Redis and saw the bump.
    assert seen["endpoint_version"] == 9
    assert seen["after_endpoint_read_gets"] == 2


def test_each_request_reads_its_own_version(monkeypatch: pytest.MonkeyPatch, fake: _CountingRedis, bound_app) -> None:
    seen: dict[str, int] = {}
    client = _stack_client(monkeypatch, fake, seen)

    assert client.get("/things", headers={"X-Api-Key": _KEY}).status_code == 200
    assert client.get("/things", headers={"X-Api-Key": _KEY}).status_code == 200

    # Two requests, each one decision GET plus one live endpoint GET.
    assert fake.version_gets == 4


def test_the_auth_stack_opens_the_scope_before_authentication() -> None:
    stack = AuthAdapter(AccessControlSettings()).get_middleware()

    assert stack[0].cls is PolicyVersionScopeMiddleware


async def test_reads_inside_an_open_scope_share_one_get(fake: _CountingRedis) -> None:
    enforcer = policy_enforcer(access_control_settings())

    with policy_version_scope():
        assert await enforcer.current_policy_version() == 3
        assert await enforcer.current_policy_version() == 3

    assert fake.version_gets == 1


async def test_a_bump_inside_the_scope_updates_the_memo(fake: _CountingRedis) -> None:
    enforcer = policy_enforcer(access_control_settings())

    with policy_version_scope():
        assert await enforcer.current_policy_version() == 3
        assert await management_module.bump_policy_version() == 4
        assert await enforcer.current_policy_version() == 4

    assert fake.version_gets == 1


async def test_a_bump_inside_the_scope_before_any_read_is_served_from_the_memo(fake: _CountingRedis) -> None:
    enforcer = policy_enforcer(access_control_settings())

    with policy_version_scope():
        assert await management_module.bump_policy_version() == 4
        assert await enforcer.current_policy_version() == 4

    assert fake.version_gets == 0


async def test_a_closed_scope_reads_live_every_time(fake: _CountingRedis) -> None:
    settings = access_control_settings()
    enforcer = policy_enforcer(settings)

    with policy_version_scope():
        assert await enforcer.current_policy_version() == 3
        close_policy_version_scope()
        fake._strings[settings.policy_version_key] = "5"
        assert await enforcer.current_policy_version() == 5
        assert await enforcer.current_policy_version() == 5
        # A bump after the close is not remembered either: the next read is live.
        await management_module.bump_policy_version()
        assert await enforcer.current_policy_version() == 6

    assert fake.version_gets == 4


async def test_a_read_with_no_scope_reads_live_every_time(fake: _CountingRedis) -> None:
    settings = access_control_settings()
    enforcer = policy_enforcer(settings)

    assert await enforcer.current_policy_version() == 3
    fake._strings[settings.policy_version_key] = "4"
    assert await enforcer.current_policy_version() == 4

    assert fake.version_gets == 2


async def test_a_failed_read_is_not_memoized_and_the_next_read_retries(fake: _CountingRedis) -> None:
    enforcer = policy_enforcer(access_control_settings())

    with policy_version_scope():
        fake.fail_version_get = ConnectionError("redis down")
        with pytest.raises(ConnectionError):
            await enforcer.current_policy_version()
        fake.fail_version_get = None
        assert await enforcer.current_policy_version() == 3
        assert await enforcer.current_policy_version() == 3

    assert fake.version_gets == 2


async def test_concurrent_scopes_hold_separate_versions(fake: _CountingRedis) -> None:
    settings = access_control_settings()
    enforcer = policy_enforcer(settings)
    first_read = asyncio.Event()
    bumped = asyncio.Event()

    async def early() -> tuple[int, int]:
        with policy_version_scope():
            before = await enforcer.current_policy_version()
            first_read.set()
            await bumped.wait()
            return before, await enforcer.current_policy_version()

    async def late() -> int:
        await first_read.wait()
        fake._strings[settings.policy_version_key] = "8"
        bumped.set()
        with policy_version_scope():
            return await enforcer.current_policy_version()

    (early_before, early_after), late_version = await asyncio.gather(early(), late())

    assert (early_before, early_after) == (3, 3)
    assert late_version == 8


async def test_the_scope_is_reset_on_exit(fake: _CountingRedis) -> None:
    enforcer = policy_enforcer(access_control_settings())

    with policy_version_scope():
        await enforcer.current_policy_version()
    await enforcer.current_policy_version()

    assert fake.version_gets == 2


def test_closing_with_no_scope_bound_is_a_no_op() -> None:
    close_policy_version_scope()


async def test_the_scope_middleware_binds_a_memo_for_http_and_websocket_only(fake: _CountingRedis) -> None:
    enforcer = policy_enforcer(access_control_settings())
    reads: dict[str, int] = {}

    async def app(scope, receive, send) -> None:
        start = fake.version_gets
        await enforcer.current_policy_version()
        await enforcer.current_policy_version()
        reads[scope["type"]] = fake.version_gets - start

    middleware = PolicyVersionScopeMiddleware(app)

    async def receive():  # pragma: no cover - never called by the inner app
        raise AssertionError("receive")

    async def send(message) -> None:  # pragma: no cover - never called by the inner app
        raise AssertionError("send")

    for scope_type in ("http", "websocket", "lifespan"):
        await middleware({"type": scope_type}, receive, send)

    assert reads == {"http": 1, "websocket": 1, "lifespan": 2}
