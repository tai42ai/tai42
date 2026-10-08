"""Every door reads a principal's standing the same way: one table of principals through every door.

The doors: the HTTP edge (backend + resource guard), the tool edge for a request, the
agent-run fire door, the execution-key assert, the bind scan (the key assert plus the
token-free evaluability read), the capability projection, the operations caller resolution,
and claim-link creation. A principal with no standing — disabled, or owned by a disabled or
policy-less owner — is refused at every door; a credential whose owner claim is not the
owner its stored policy names is refused wherever a credential is verified, while the
tokenless doors (which read only the stored owner) stand it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from typing import Any, cast

import pytest
from fastmcp.server.auth import AccessToken
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.responses import PlainTextResponse
from starlette.types import Message
from tai42_contract.access_control import (
    KEY_FINGERPRINT_CLAIM,
    OWNER_USER_ID_CLAIM,
    reset_request_user_id,
    set_request_user_id,
)
from tai42_contract.app import tai42_app

from tai42_skeleton.access_control import claim_links as claim_links_module
from tai42_skeleton.access_control import management as management_module
from tai42_skeleton.access_control import policy as policy_module
from tai42_skeleton.access_control import projection
from tai42_skeleton.access_control import store as store_module
from tai42_skeleton.access_control.adapter import handle_auth_error
from tai42_skeleton.access_control.backend import AccessControlAuthBackend
from tai42_skeleton.access_control.claim_links import ClaimLinkError, create_claim_link
from tai42_skeleton.access_control.middleware import ResourceGuardMiddleware
from tai42_skeleton.access_control.policy import policy_enforcer
from tai42_skeleton.access_control.settings import AccessControlSettings
from tai42_skeleton.access_control.verifier import AccessControlVerifier
from tai42_skeleton.authz import check
from tai42_skeleton.authz.execution import (
    ExecutionKeyAuthorityError,
    ExecutionKeyScan,
    assert_key_carries_authority,
    authorize_execution_agent_run,
)
from tai42_skeleton.authz.identity import CallerIdentity
from tai42_skeleton.operations import OperationRegistry, operation
from tai42_skeleton.operations._authority import resolve_caller
from tai42_skeleton.operations.errors import PermissionDeniedError

from ..access_control.conftest import FakeAccessControlPg, FakeRedis, make_client_ctx, make_pg_ctx
from .conftest import _FakeApp

pytestmark = pytest.mark.filterwarnings("ignore::async_lru.AlruCacheLoopResetWarning")

_TRUE = {"content": "true"}

# principal -> (stored owner, the owner its credential claims)
_PRINCIPALS: dict[str, tuple[str | None, str | None]] = {
    "unowned": (None, None),
    "owned": ("o-ok", "o-ok"),
    "disabled": ("o-ok", "o-ok"),
    "owner-disabled": ("o-disabled", "o-disabled"),
    "owner-no-policy": ("o-missing", "o-missing"),
    "owner-mismatch": ("o-ok", "o-other"),
}

_STANDS = {"unowned", "owned"}
# The doors that compare a verified credential's owner claim with the stored owner.
_VERIFYING_DOORS = {"http", "tool-edge", "projection", "claim-link"}


def _fingerprint(principal: str) -> str:
    return f"fp-{principal}"


@pytest.fixture
def table(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeAccessControlPg]:
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "test")
    pg = FakeAccessControlPg()
    redis = FakeRedis()
    rctx = make_client_ctx(redis)
    monkeypatch.setattr(store_module, "client_ctx", make_pg_ctx(pg))
    monkeypatch.setattr(policy_module, "client_ctx", rctx)
    monkeypatch.setattr(management_module, "client_ctx", rctx)
    monkeypatch.setattr(claim_links_module, "client_ctx", rctx)
    monkeypatch.setattr(projection, "_registry_routes", list)
    monkeypatch.setattr(projection, "_all_agent_names", list)
    monkeypatch.setattr(projection, "_mintable", lambda: False)

    async def _no_mounts() -> dict:
        return {}

    async def _no_tools() -> list:
        return []

    monkeypatch.setattr(projection, "_sub_mcp_routes", _no_mounts)
    monkeypatch.setattr(projection, "_all_registry_tools", _no_tools)
    projection.reset_projection_cache()

    for owner in ("o-ok", "o-other"):
        pg.add_policy(owner, scopes=["*"], condition=_TRUE)
        pg.add_principal(owner)
    pg.add_policy("o-disabled", scopes=["*"], condition=_TRUE, policy_data={"disabled": True})
    pg.add_principal("o-disabled", disabled=True)
    for principal, (owner, _claimed) in _PRINCIPALS.items():
        policy_data: dict[str, Any] = {KEY_FINGERPRINT_CLAIM: _fingerprint(principal)}
        if owner is not None:
            policy_data[OWNER_USER_ID_CLAIM] = owner
        if principal == "disabled":
            policy_data["disabled"] = True
        # The unowned principal carries a condition so it is not the admin discriminator.
        pg.add_policy(principal, scopes=["*"], policy_data=policy_data, condition=_TRUE)
        pg.add_principal(principal)
    with tai42_app.bound(_FakeApp()):
        yield pg
    projection.reset_projection_cache()


def _verified_claims(principal: str) -> dict[str, Any]:
    claimed = _PRINCIPALS[principal][1]
    return {"sub": principal} | ({OWNER_USER_ID_CLAIM: claimed} if claimed is not None else {})


def _fire_claims(principal: str) -> dict[str, Any]:
    owner = _PRINCIPALS[principal][0]
    return {OWNER_USER_ID_CLAIM: owner} if owner is not None else {}


class _Verifier:
    async def verify_token(self, token: str) -> AccessToken | None:
        return AccessToken(token=token, client_id=token, scopes=[], claims=_verified_claims(token))


def _settings() -> AccessControlSettings:
    return AccessControlSettings(enable=True)


async def _http(principal: str) -> bool:
    settings = _settings()

    async def endpoint(scope, receive, send) -> None:
        await PlainTextResponse("ok")(scope, receive, send)

    guard = ResourceGuardMiddleware(
        endpoint, AccessControlVerifier(settings, providers=[]), settings.public_resource_id
    )
    app = AuthenticationMiddleware(
        guard,
        backend=AccessControlAuthBackend(cast("AccessControlVerifier", _Verifier()), settings),
        on_error=handle_auth_error,
    )
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(dict(message))

    await app(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/things/read",
            "raw_path": b"/api/things/read",
            "root_path": "",
            "query_string": b"",
            "headers": [(b"authorization", f"Bearer {principal}".encode())],
        },
        receive,
        send,
    )
    return next(m["status"] for m in sent if m["type"] == "http.response.start") == 200


async def _tool_edge(principal: str) -> bool:
    reg = OperationRegistry()

    @operation(name="read_things", summary="Read", tags=["things"], registry=reg)
    async def read_things(**_: Any) -> dict:
        return {}

    meta = reg.get("read_things")
    meta.route_template = "/api/things/read"
    meta.http_method = "POST"
    identity = CallerIdentity(user_id=principal, claims=_verified_claims(principal), effective_scopes=("*",))
    try:
        await check(identity, meta, {}, settings=_settings())
    except PermissionDeniedError:
        return False
    return True


async def _agent_run_fire(principal: str) -> bool:
    identity = CallerIdentity(
        user_id=principal, claims=_fire_claims(principal), execution_key_fingerprint=_fingerprint(principal)
    )
    try:
        await authorize_execution_agent_run(identity, "faker", settings=_settings())
    except PermissionDeniedError:
        return False
    return True


async def _key_assert(principal: str) -> bool:
    try:
        await assert_key_carries_authority(
            policy_enforcer(_settings()), principal, bound_fingerprint=_fingerprint(principal)
        )
    except ExecutionKeyAuthorityError:
        return False
    return True


async def _bind_scan(principal: str) -> bool:
    try:
        await ExecutionKeyScan().assert_usable(principal, bound_fingerprint=_fingerprint(principal))
    except ExecutionKeyAuthorityError:
        return False
    return True


async def _projection(principal: str) -> bool:
    try:
        await projection.build_projection(principal, ["*"], _verified_claims(principal))
    except PermissionDeniedError:
        return False
    return True


async def _caller(principal: str) -> bool:
    token = set_request_user_id(principal)
    try:
        await resolve_caller()
    except PermissionDeniedError:
        return False
    finally:
        reset_request_user_id(token)
    return True


async def _claim_link(principal: str, monkeypatch: pytest.MonkeyPatch) -> bool:
    monkeypatch.setattr(claim_links_module, "_verifier", lambda _settings: _Verifier())
    try:
        await create_claim_link(
            api_key=principal, caller_id="admin", caller_is_admin=True, caller_owner_claim=None, ttl_seconds=None
        )
    except ClaimLinkError as exc:
        if exc.status != 400:
            raise
        return False
    return True


_DOORS: dict[str, Callable[..., Awaitable[bool]]] = {
    "http": _http,
    "tool-edge": _tool_edge,
    "agent-run-fire": _agent_run_fire,
    "key-assert": _key_assert,
    "bind-scan": _bind_scan,
    "projection": _projection,
    "operations-caller": _caller,
    "claim-link": _claim_link,
}


def _expected(principal: str, door: str) -> bool:
    if principal in _STANDS:
        return True
    if principal == "owner-mismatch":
        return door not in _VERIFYING_DOORS
    return False


@pytest.mark.parametrize("door", list(_DOORS))
@pytest.mark.parametrize("principal", list(_PRINCIPALS))
async def test_every_door_reads_one_standing(
    table: FakeAccessControlPg, monkeypatch: pytest.MonkeyPatch, principal: str, door: str
) -> None:
    call = _DOORS[door]
    if door == "claim-link":
        admitted = await call(principal, monkeypatch)
    else:
        admitted = await call(principal)
    assert admitted is _expected(principal, door)
