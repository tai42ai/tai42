"""The admin-only principal-management operations.

Covers create (row + role, id defaulting, duplicate 409, unknown role 400), list, update
(display name for any kind, disabled through the single writer), delete (revokes owned keys
+ row), and the two fences a disable/delete passes: the ownership fence (a login-attaching
provider that holds the principal's login refuses here with a 409, routing to its users
door) and the last-admin guard (the last enabled admin principal cannot be disabled or
deleted). An OIDC-provisioned human — a human no attaching provider claims — is disabled and
deleted here like any un-owned principal. Also the not-found 404s and the admin fence on
every door.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from starlette.requests import Request
from tai42_contract.access_control import registry
from tai42_contract.access_control.identity import ApiKeyIdentityProvider
from tai42_contract.access_control.models import AccessPolicy

import tai42_skeleton.versioning as versioning_module
from tai42_skeleton.access_control import management
from tai42_skeleton.access_control.roles import seed_default_roles
from tai42_skeleton.app.route_registry import _SpecApp
from tai42_skeleton.operations import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    register_operation_route,
)
from tai42_skeleton.operations import principals as principals_ops
from tai42_skeleton.operations._authority import Caller
from tai42_skeleton.operations.decorator import operation_metadata_of

from .conftest import FakeAccessControlPg, FakeRedis, make_client_ctx
from .test_policy_store import _MemStore


class _SpyProvider(ApiKeyIdentityProvider):
    def __init__(self) -> None:
        self.identities: dict[str, str] = {}

    async def validate_token(self, token: str):  # pragma: no cover - unused
        return None

    async def provision(self, user_id: str, description: str, *, owner_user_id: str) -> str:
        self.identities[user_id] = description
        return f"sk-{user_id}"

    async def revoke(self, user_id: str) -> bool:
        return self.identities.pop(user_id, None) is not None

    async def update_description(self, user_id: str, description: str) -> bool:  # pragma: no cover - unused
        return user_id in self.identities

    async def list_identities(self) -> list[tuple[str, str]]:
        return list(self.identities.items())


class _FakeLoginProvider:
    """A login-attaching provider stand-in: ``has_login`` is true for the ids it holds.

    The door reads this through the monkeypatched ``_login_attaching_providers`` seam, so a
    test models "a provider that holds this principal's login" (a password human) by adding
    the id, and "a provider present but not this principal's owner" (an OIDC human) by
    leaving it out.
    """

    def __init__(self, logins: set[str]) -> None:
        self._logins = logins

    async def has_login(self, user_id: str) -> bool:
        return user_id in self._logins


@pytest.fixture
def mem() -> _MemStore:
    return _MemStore()


@pytest.fixture(autouse=True)
def _wire_versioned_store(monkeypatch, mem: _MemStore) -> None:
    monkeypatch.setattr(versioning_module, "versioned_store", lambda: mem)


@pytest.fixture(autouse=True)
def login_provider(monkeypatch) -> set[str]:
    """A single registered login-attaching provider whose held logins the test controls.

    Defaults to holding NO login, so a principal is un-owned (a service principal, an
    OIDC-provisioned human, the keys-only owner) unless a test adds its id.
    """
    logins: set[str] = set()
    provider = _FakeLoginProvider(logins)
    monkeypatch.setattr(principals_ops, "_login_attaching_providers", lambda: [("accounts", provider)])
    return logins


@pytest.fixture
def redis_mgmt(monkeypatch) -> FakeRedis:
    fake = FakeRedis(strings={})
    monkeypatch.setattr(management, "client_ctx", make_client_ctx(fake))
    return fake


@pytest.fixture
def provider() -> _SpyProvider:
    spy = _SpyProvider()
    registry._REGISTRY["redis"] = lambda _settings: spy
    return spy


@pytest.fixture
def admin_caller(monkeypatch) -> None:
    async def _admin() -> Caller:
        return Caller(caller_id="admin1", policy=AccessPolicy(scopes=["*"]), is_admin=True, owner_claim=None)

    monkeypatch.setattr(principals_ops, "resolve_caller", _admin)


@pytest.fixture
def non_admin_caller(monkeypatch) -> None:
    async def _editor() -> Caller:
        return Caller(caller_id="editor1", policy=AccessPolicy(scopes=["read"]), is_admin=False, owner_claim=None)

    monkeypatch.setattr(principals_ops, "resolve_caller", _editor)


async def _seed_roles(pg: FakeAccessControlPg) -> None:
    await seed_default_roles()


async def test_create_principal_creates_row_and_applies_role(
    pg: FakeAccessControlPg, provider: _SpyProvider, redis_mgmt: FakeRedis, admin_caller
) -> None:
    await _seed_roles(pg)
    row = await principals_ops.create_principal(user_id="svc", kind="service", display_name="A Service", role="editor")
    assert row.user_id == "svc"
    assert row.kind == "service"
    assert row.display_name == "A Service"
    assert row.created_by == "admin1"  # the admin who created it
    assert pg.principal("svc") is not None
    # The role landed on the principal's own policy row.
    assert pg.policy("svc") is not None


async def test_create_principal_mints_an_id_when_absent(
    pg: FakeAccessControlPg, provider: _SpyProvider, redis_mgmt: FakeRedis, admin_caller
) -> None:
    await _seed_roles(pg)
    row = await principals_ops.create_principal(user_id=None, kind="human", display_name="H", role="viewer")
    assert row.user_id.startswith("usr-")
    assert pg.principal(row.user_id) is not None


async def test_create_duplicate_principal_is_409(
    pg: FakeAccessControlPg, provider: _SpyProvider, redis_mgmt: FakeRedis, admin_caller
) -> None:
    await _seed_roles(pg)
    pg.add_principal("svc", kind="service", display_name="X")
    with pytest.raises(ConflictError, match="already exists"):
        await principals_ops.create_principal(user_id="svc", kind="service", display_name="X", role="editor")


async def test_create_unknown_role_is_400_and_compensates(
    pg: FakeAccessControlPg, provider: _SpyProvider, redis_mgmt: FakeRedis, admin_caller
) -> None:
    await _seed_roles(pg)
    with pytest.raises(BadRequestError, match="unknown role"):
        await principals_ops.create_principal(user_id="svc", kind="service", display_name="X", role="ghost")
    # Compensation: the principal row does not survive an unknown-role create.
    assert pg.principal("svc") is None


async def test_list_principals(
    pg: FakeAccessControlPg, provider: _SpyProvider, redis_mgmt: FakeRedis, admin_caller
) -> None:
    pg.add_principal("a", display_name="A")
    pg.add_principal("b", display_name="B")
    rows = await principals_ops.list_principals()
    assert {r.user_id for r in rows.root} == {"a", "b"}


async def test_update_display_name(
    pg: FakeAccessControlPg, provider: _SpyProvider, redis_mgmt: FakeRedis, admin_caller
) -> None:
    pg.add_principal("a", display_name="Old")
    pg.add_policy("a", scopes=["read"])
    row = await principals_ops.update_principal(user_id="a", display_name="New")
    assert row.display_name == "New"


async def test_update_disabled_flips_both_homes(
    pg: FakeAccessControlPg, provider: _SpyProvider, redis_mgmt: FakeRedis, admin_caller
) -> None:
    # An admin principal with a co-admin present, so the last-admin guard does not fire and
    # the disabled flip's both-home mechanics are what is under test.
    pg.add_principal("a", kind="service", display_name="A")
    pg.add_policy("a", scopes=["*"])
    pg.add_principal("co", kind="service", display_name="Co")
    pg.add_policy("co", scopes=["*"])
    row = await principals_ops.update_principal(user_id="a", disabled=True)
    assert row.disabled is True
    # The enforcement projection on the policy row moves with the authoritative column.
    assert pg.policy_body("a")["policy_data"]["disabled"] is True
    await principals_ops.update_principal(user_id="a", disabled=False)
    assert "disabled" not in pg.policy_body("a")["policy_data"]


async def test_update_nothing_is_400(
    pg: FakeAccessControlPg, provider: _SpyProvider, redis_mgmt: FakeRedis, admin_caller
) -> None:
    pg.add_principal("a", display_name="A")
    with pytest.raises(BadRequestError, match="nothing to change"):
        await principals_ops.update_principal(user_id="a")


async def test_update_missing_principal_is_404(
    pg: FakeAccessControlPg, provider: _SpyProvider, redis_mgmt: FakeRedis, admin_caller
) -> None:
    with pytest.raises(NotFoundError, match="not found"):
        await principals_ops.update_principal(user_id="ghost", display_name="X")


async def test_update_disabled_on_a_login_owned_principal_is_409(
    pg: FakeAccessControlPg, provider: _SpyProvider, redis_mgmt: FakeRedis, admin_caller, login_provider: set[str]
) -> None:
    # A provider holds this principal's login, so its disabled state is owned by that
    # provider's users door.
    pg.add_principal("h", kind="human", display_name="H")
    pg.add_policy("h", scopes=["*"])
    login_provider.add("h")
    with pytest.raises(ConflictError, match="accounts provider"):
        await principals_ops.update_principal(user_id="h", disabled=True)
    # Nothing flipped on either home.
    assert pg.principal("h")["disabled"] is False
    assert "disabled" not in pg.policy_body("h")["policy_data"]


async def test_update_disabled_on_an_oidc_human_flips_both_homes(
    pg: FakeAccessControlPg, provider: _SpyProvider, redis_mgmt: FakeRedis, admin_caller
) -> None:
    # An OIDC-provisioned human: a human principal no attaching provider claims (login lives
    # at the issuer). The principals door disables it directly, both homes updated. A
    # co-admin keeps it clear of the last-admin guard.
    pg.add_principal("oidc:idp:sub", kind="human", display_name="H")
    pg.add_policy("oidc:idp:sub", scopes=["*"])
    pg.add_principal("co", kind="service", display_name="Co")
    pg.add_policy("co", scopes=["*"])
    row = await principals_ops.update_principal(user_id="oidc:idp:sub", disabled=True)
    assert row.disabled is True
    assert pg.principal("oidc:idp:sub")["disabled"] is True
    assert pg.policy_body("oidc:idp:sub")["policy_data"]["disabled"] is True


async def test_update_human_display_name_is_allowed(
    pg: FakeAccessControlPg, provider: _SpyProvider, redis_mgmt: FakeRedis, admin_caller, login_provider: set[str]
) -> None:
    # A display-name edit is accepted for any kind, even a login-owned human — no ownership
    # or last-admin fence guards a rename.
    pg.add_principal("h", kind="human", display_name="Old")
    pg.add_policy("h", scopes=["*"])
    login_provider.add("h")
    row = await principals_ops.update_principal(user_id="h", display_name="New")
    assert row.display_name == "New"


async def test_delete_login_owned_principal_is_409(
    pg: FakeAccessControlPg, provider: _SpyProvider, redis_mgmt: FakeRedis, admin_caller, login_provider: set[str]
) -> None:
    # A provider owns this principal's login, so it is deleted through that provider's users
    # door, not this one.
    pg.add_principal("h", kind="human", display_name="H")
    pg.add_policy("h", scopes=["*"])
    login_provider.add("h")
    with pytest.raises(ConflictError, match="accounts provider"):
        await principals_ops.delete_principal(user_id="h")
    assert pg.principal("h") is not None


async def test_delete_oidc_human_is_allowed(
    pg: FakeAccessControlPg, provider: _SpyProvider, redis_mgmt: FakeRedis, admin_caller
) -> None:
    # An OIDC-provisioned human with a co-admin present is deleted through the principals
    # door: both the policy row and the principal row go.
    pg.add_principal("oidc:idp:sub", kind="human", display_name="H")
    pg.add_policy("oidc:idp:sub", scopes=["*"])
    pg.add_principal("co", kind="service", display_name="Co")
    pg.add_policy("co", scopes=["*"])
    result = await principals_ops.delete_principal(user_id="oidc:idp:sub")
    assert result == {"user_id": "oidc:idp:sub", "deleted": True}
    assert pg.principal("oidc:idp:sub") is None
    assert pg.policy("oidc:idp:sub") is None


async def test_delete_service_principal_revokes_owned_keys(
    pg: FakeAccessControlPg, provider: _SpyProvider, redis_mgmt: FakeRedis, admin_caller
) -> None:
    pg.add_principal("a", kind="service", display_name="A")
    await management.access_control_store().create_policy("a", [])
    await management.add_user_api_key("a-key", "machine", [], owner_user_id="a")
    result = await principals_ops.delete_principal(user_id="a")
    assert result == {"user_id": "a", "deleted": True}
    assert "a-key" not in provider.identities  # owned key revoked
    assert pg.principal("a") is None
    assert pg.policy("a") is None


async def test_delete_missing_principal_is_404(
    pg: FakeAccessControlPg, provider: _SpyProvider, redis_mgmt: FakeRedis, admin_caller
) -> None:
    with pytest.raises(NotFoundError, match="not found"):
        await principals_ops.delete_principal(user_id="ghost")


async def test_disable_last_enabled_admin_is_409(
    pg: FakeAccessControlPg, provider: _SpyProvider, redis_mgmt: FakeRedis, admin_caller
) -> None:
    # The only enabled admin principal (a service principal here): disabling it would strand
    # the deployment with no admin.
    pg.add_principal("a", kind="service", display_name="A")
    pg.add_policy("a", scopes=["*"])
    with pytest.raises(ConflictError, match="last enabled admin"):
        await principals_ops.update_principal(user_id="a", disabled=True)
    assert pg.principal("a")["disabled"] is False


async def test_delete_last_enabled_admin_service_is_409(
    pg: FakeAccessControlPg, provider: _SpyProvider, redis_mgmt: FakeRedis, admin_caller
) -> None:
    pg.add_principal("a", kind="service", display_name="A")
    pg.add_policy("a", scopes=["*"])
    with pytest.raises(ConflictError, match="last enabled admin"):
        await principals_ops.delete_principal(user_id="a")
    assert pg.principal("a") is not None


async def test_delete_last_enabled_admin_human_is_409(
    pg: FakeAccessControlPg, provider: _SpyProvider, redis_mgmt: FakeRedis, admin_caller
) -> None:
    # An OIDC human that is the last enabled admin: the same guard fires, whatever the kind.
    pg.add_principal("oidc:idp:sub", kind="human", display_name="H")
    pg.add_policy("oidc:idp:sub", scopes=["*"])
    with pytest.raises(ConflictError, match="last enabled admin"):
        await principals_ops.delete_principal(user_id="oidc:idp:sub")
    assert pg.principal("oidc:idp:sub") is not None


async def test_delete_non_admin_only_principal_is_allowed(
    pg: FakeAccessControlPg, provider: _SpyProvider, redis_mgmt: FakeRedis, admin_caller
) -> None:
    # The last-admin guard concerns ADMINS only: a non-admin principal is deletable even as
    # the only remaining principal.
    pg.add_principal("n", kind="service", display_name="N")
    pg.add_policy("n", scopes=["read"])
    result = await principals_ops.delete_principal(user_id="n")
    assert result == {"user_id": "n", "deleted": True}
    assert pg.principal("n") is None


async def test_double_delete_second_is_404(
    pg: FakeAccessControlPg, provider: _SpyProvider, redis_mgmt: FakeRedis, admin_caller
) -> None:
    # Sequential proof of the double-delete outcome the ROW-LEVEL delete rowcount decides
    # over real Postgres: the first delete wins, the second re-reads under the guard, finds
    # the row gone, and 404s loudly (never a silent second success). A co-admin keeps both
    # deletes clear of the last-admin guard.
    pg.add_principal("a", kind="service", display_name="A")
    pg.add_policy("a", scopes=["read"])
    pg.add_principal("co", kind="service", display_name="Co")
    pg.add_policy("co", scopes=["*"])
    assert await principals_ops.delete_principal(user_id="a") == {"user_id": "a", "deleted": True}
    with pytest.raises(NotFoundError, match="not found"):
        await principals_ops.delete_principal(user_id="a")


async def test_last_admin_refusal_is_atomic_no_partial_write(
    pg: FakeAccessControlPg, provider: _SpyProvider, redis_mgmt: FakeRedis, admin_caller
) -> None:
    # The last enabled admin: the refusal must fire INSIDE the advisory-locked guard
    # transaction, BEFORE any row is touched, so no partial write escapes. The fake records
    # every statement in order; asserting the advisory lock was taken but neither the DELETE
    # nor the disabled UPDATE ever ran proves the count precedes — and short-circuits — the
    # mutation, and the guard's snapshot rollback leaves both rows intact.
    pg.add_principal("a", kind="service", display_name="A")
    pg.add_policy("a", scopes=["*"])
    with pytest.raises(ConflictError, match="last enabled admin"):
        await principals_ops.delete_principal(user_id="a")
    assert any(s.startswith("SELECT pg_advisory_xact_lock") for s in pg.executed)
    assert not any(s.startswith("DELETE FROM access_control_principals") for s in pg.executed)
    assert not any(s.startswith("DELETE FROM access_control_policies") for s in pg.executed)
    assert pg.principal("a") is not None
    assert pg.policy("a") is not None


@pytest.mark.parametrize(
    "call",
    [
        lambda: principals_ops.list_principals(),
        lambda: principals_ops.create_principal(user_id="x", kind="service", display_name="X", role="editor"),
        lambda: principals_ops.update_principal(user_id="x", display_name="X"),
        lambda: principals_ops.delete_principal(user_id="x"),
    ],
)
async def test_principals_doors_are_admin_only(
    pg: FakeAccessControlPg, provider: _SpyProvider, redis_mgmt: FakeRedis, non_admin_caller, call
) -> None:
    with pytest.raises(ForbiddenError, match="administrators"):
        await call()


def _http_request(method: str, path: str, *, body: dict | None = None, path_params: dict | None = None) -> Request:
    """A minimal ASGI request carrying an optional JSON body, for driving a route handler."""
    payload = json.dumps(body or {}).encode()

    async def receive() -> dict:
        return {"type": "http.request", "body": payload, "more_body": False}

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "path_params": path_params or {},
    }
    return Request(scope, receive)


async def test_principals_routes_serialize_created_at_over_http(
    pg: FakeAccessControlPg, provider: _SpyProvider, redis_mgmt: FakeRedis, admin_caller
) -> None:
    """The list/create/update routes answer 200 with a JSON-native ISO ``created_at``.

    Driven through the route adapter — the seam that JSON-encodes the response body — so the
    principal's timezone-aware ``created_at`` reaches the wire as an ISO-8601 string, which
    the JSON encoder cannot do for a raw ``datetime``.
    """
    await _seed_roles(pg)
    pg.add_principal("seed", kind="service", display_name="Seed")
    pg.add_policy("seed", scopes=["read"])

    list_handler = register_operation_route(
        _SpecApp(),
        operation_metadata_of(principals_ops.list_principals),
        path="/api/auth/principals",
        method="GET",
        action="secret",
    )
    create_handler = register_operation_route(
        _SpecApp(),
        operation_metadata_of(principals_ops.create_principal),
        path="/api/auth/principals",
        method="POST",
        action="fenced",
    )
    update_handler = register_operation_route(
        _SpecApp(),
        operation_metadata_of(principals_ops.update_principal),
        path="/api/auth/principals/{user_id}",
        method="PUT",
        action="fenced",
    )

    list_resp = await list_handler(_http_request("GET", "/api/auth/principals"))
    assert list_resp.status_code == 200
    listed = json.loads(bytes(list_resp.body))["data"]
    assert datetime.fromisoformat(listed[0]["created_at"]).tzinfo is not None

    create_resp = await create_handler(
        _http_request("POST", "/api/auth/principals", body={"kind": "service", "display_name": "Svc", "role": "editor"})
    )
    assert create_resp.status_code == 200
    created = json.loads(bytes(create_resp.body))["data"]
    assert datetime.fromisoformat(created["created_at"]).tzinfo is not None

    update_resp = await update_handler(
        _http_request(
            "PUT", "/api/auth/principals/seed", body={"display_name": "Renamed"}, path_params={"user_id": "seed"}
        )
    )
    assert update_resp.status_code == 200
    updated = json.loads(bytes(update_resp.body))["data"]
    assert updated["display_name"] == "Renamed"
    assert datetime.fromisoformat(updated["created_at"]).tzinfo is not None
