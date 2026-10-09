"""The provider door is for administrators only: each of its four routes refuses an editor holding the
seeded editor role, through the full authenticate flow, while the editor still reaches the connect door
and a viewer still reads the catalog."""

from __future__ import annotations

import pytest

from tai42_skeleton.access_control import policy as policy_module
from tai42_skeleton.access_control import store as store_module
from tai42_skeleton.access_control.backend import AuthorizationError
from tai42_skeleton.access_control.roles import editor_jq, viewer_jq
from tai42_skeleton.access_control.settings import AccessControlSettings

from .conftest import FakeAccessControlPg, FakeRedis, make_client_ctx, make_pg_ctx
from .test_backend import _backend, _conn, _FakeVerifier

_DOOR = [
    ("POST", "/api/connectors/providers/discover"),
    ("POST", "/api/connectors/providers"),
    ("DELETE", "/api/connectors/providers/remote"),
    ("PUT", "/api/connectors/providers/remote/client"),
]


@pytest.fixture
def store_pg(monkeypatch: pytest.MonkeyPatch) -> FakeAccessControlPg:
    fake = FakeAccessControlPg()
    monkeypatch.setattr(store_module, "client_ctx", make_pg_ctx(fake))
    return fake


@pytest.fixture
async def roles(monkeypatch, bound_app, store_pg):
    import tai42_skeleton.versioning as versioning_module
    from tai42_skeleton.access_control import role_grants as role_grants_module
    from tai42_skeleton.access_control.role_gate import reset_route_index
    from tai42_skeleton.access_control.roles import seed_default_roles

    from .test_policy_store import _MemStore

    bound_app.http = object()
    mem = _MemStore()
    monkeypatch.setattr(versioning_module, "versioned_store", lambda: mem)
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "secret")
    role_grants_module.reset_role_grants_cache()
    reset_route_index()
    await seed_default_roles()
    store_pg.add_policy("ed", scopes=["*"], condition={"content": editor_jq()}, policy_data={"role": "editor"})
    store_pg.add_policy("vw", scopes=["*"], condition={"content": viewer_jq()}, policy_data={"role": "viewer"})
    store_pg.add_policy("root", scopes=["*"])
    monkeypatch.setattr(policy_module, "client_ctx", make_client_ctx(FakeRedis()))
    return _backend(_FakeVerifier({"ed": "ed", "vw": "vw", "root": "root"}), AccessControlSettings())


@pytest.mark.parametrize(("method", "path"), _DOOR)
async def test_an_editor_is_refused_every_door_route(roles, method: str, path: str) -> None:
    from tai42_skeleton.access_control.role_gate import DenialCause

    with pytest.raises(AuthorizationError) as denied:
        await roles.authenticate(_conn({"Authorization": "Bearer ed"}, path=path, method=method))
    assert denied.value.cause is DenialCause.HARD_FENCE


@pytest.mark.parametrize(("method", "path"), _DOOR)
async def test_an_administrator_passes_every_door_route(roles, method: str, path: str) -> None:
    _creds, user = await roles.authenticate(_conn({"Authorization": "Bearer root"}, path=path, method=method))
    assert user.is_authenticated


async def test_an_editor_still_reaches_the_connect_door_and_a_viewer_the_catalog(roles) -> None:
    _creds, editor = await roles.authenticate(
        _conn({"Authorization": "Bearer ed"}, path="/api/connectors/connections/start", method="POST")
    )
    assert editor.is_authenticated
    _creds, viewer = await roles.authenticate(
        _conn({"Authorization": "Bearer vw"}, path="/api/connectors/providers", method="GET")
    )
    assert viewer.is_authenticated
