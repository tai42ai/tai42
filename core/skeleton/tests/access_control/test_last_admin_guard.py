"""One last-admin guard on every door that can strand the admins.

"Admin" is every enabled principal whose own policy is admin-shaped and free of the
``disabled`` marker enforcement denies on. The guard refuses (``LastAdminError``, nothing
written) a change that leaves no enabled admin principal: a role demotion through
``apply_role``, a disable, a delete, and a key/policy-door write (edit, scope edit,
rollback) that rewrites a principal's own row. Promotions, a demotion while another enabled
admin exists, a change to a disabled admin and a write to a key row (no principal) pass.

The ``disabled`` marker in a policy is the server-owned projection of the principal's
``disabled`` state: a key/policy-door edit carries the stored marker forward and refuses
(``ValueError``, nothing written) a value that is not exactly the stored boolean; a rollback
keeps the live one; the key mint refuses any supplied ``disabled`` before it writes anything.
"""

from __future__ import annotations

import pytest
from tai42_contract.access_control.models import AccessPolicy
from tai42_contract.accounts.errors import LastAdminError

import tai42_skeleton.versioning as versioning_module
from tai42_skeleton.access_control import management, roles
from tai42_skeleton.access_control.roles import RESERVED_ADMIN_ROLE, ROLE_POINTER_KEY, seed_default_roles
from tai42_skeleton.operations import api_keys as api_keys_ops
from tai42_skeleton.operations._authority import Caller
from tai42_skeleton.operations.errors import BadRequestError, ConflictError

from .conftest import FakeAccessControlPg, FakeRedis, make_client_ctx
from .test_policy_store import _MemStore
from .test_principals import _SpyProvider

_ADMIN_BODY = {"scopes": ["*"], "policy_data": {}, "condition": None}


@pytest.fixture
def mem() -> _MemStore:
    return _MemStore()


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch, mem: _MemStore) -> None:
    monkeypatch.setattr(versioning_module, "versioned_store", lambda: mem)
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "secret")


@pytest.fixture(autouse=True)
def redis_mgmt(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    fake = FakeRedis(strings={})
    monkeypatch.setattr(management, "client_ctx", make_client_ctx(fake))
    return fake


@pytest.fixture(autouse=True)
def identity_provider() -> _SpyProvider:
    """The key-minting provider a delete's owned-key revoke enumerates."""
    from tai42_kit.access_control import registry

    spy = _SpyProvider()
    registry._PROVIDERS._generation.committed()["redis"] = lambda _settings: spy
    return spy


def _admin(pg: FakeAccessControlPg, user_id: str, *, disabled: bool = False) -> None:
    pg.add_principal(user_id, kind="service", display_name=user_id, disabled=disabled)
    pg.add_policy(user_id, scopes=["*"], policy_data={"disabled": True} if disabled else {})


# -- apply_role ----------------------------------------------------------------


async def test_demoting_the_last_admin_through_apply_role_is_refused(pg: FakeAccessControlPg) -> None:
    await seed_default_roles()
    _admin(pg, "a")
    with pytest.raises(LastAdminError, match="last enabled admin"):
        await roles.apply_role("a", "viewer")
    assert pg.policy_body("a") == _ADMIN_BODY


async def test_demoting_an_admin_while_another_remains_is_allowed(pg: FakeAccessControlPg) -> None:
    await seed_default_roles()
    _admin(pg, "a")
    _admin(pg, "b")
    await roles.apply_role("a", "viewer")
    assert pg.policy_body("a")["policy_data"][ROLE_POINTER_KEY] == "viewer"


async def test_reapplying_the_admin_role_to_the_last_admin_is_allowed(pg: FakeAccessControlPg) -> None:
    await seed_default_roles()
    _admin(pg, "a")
    await roles.apply_role("a", RESERVED_ADMIN_ROLE)
    assert pg.policy_body("a")["scopes"] == ["*"]


async def test_promoting_a_principal_needs_no_count(pg: FakeAccessControlPg) -> None:
    await seed_default_roles()
    pg.add_principal("v", kind="service", display_name="v")
    pg.add_policy(
        "v",
        scopes=["*"],
        policy_data={ROLE_POINTER_KEY: "viewer"},
        condition={"content": "true", "id": None, "kwargs": {}},
    )
    await roles.apply_role("v", RESERVED_ADMIN_ROLE)
    assert ROLE_POINTER_KEY not in pg.policy_body("v")["policy_data"]


async def test_demoting_a_disabled_admin_is_allowed(pg: FakeAccessControlPg) -> None:
    await seed_default_roles()
    _admin(pg, "a", disabled=True)
    await roles.apply_role("a", "viewer")
    assert pg.policy_body("a")["policy_data"] == {"disabled": True, ROLE_POINTER_KEY: "viewer"}


async def test_apply_role_creates_the_policy_row_when_absent(pg: FakeAccessControlPg) -> None:
    await seed_default_roles()
    pg.add_principal("fresh", kind="human", display_name="fresh")
    await roles.apply_role("fresh", "editor")
    assert pg.policy_body("fresh")["policy_data"] == {ROLE_POINTER_KEY: "editor"}


async def test_apply_role_unknown_role_is_a_key_error(pg: FakeAccessControlPg) -> None:
    await seed_default_roles()
    _admin(pg, "a")
    with pytest.raises(KeyError, match="unknown role"):
        await roles.apply_role("a", "no-such-role")


# -- disable and delete ----------------------------------------------------------


async def test_disabling_the_last_admin_is_refused(pg: FakeAccessControlPg) -> None:
    _admin(pg, "a")
    with pytest.raises(LastAdminError, match="last enabled admin"):
        await roles.set_principal_disabled("a", True)
    assert pg.principal("a")["disabled"] is False


async def test_disabling_an_admin_while_another_remains_is_allowed(pg: FakeAccessControlPg) -> None:
    _admin(pg, "a")
    _admin(pg, "b")
    await roles.set_principal_disabled("a", True)
    assert pg.principal("a")["disabled"] is True


async def test_re_enabling_needs_no_count(pg: FakeAccessControlPg) -> None:
    _admin(pg, "a", disabled=True)
    await roles.set_principal_disabled("a", False)
    assert pg.principal("a")["disabled"] is False


async def test_disabling_an_unknown_principal_is_a_key_error(pg: FakeAccessControlPg) -> None:
    with pytest.raises(KeyError):
        await roles.set_principal_disabled("ghost", True)


async def test_deleting_the_last_admin_is_refused(pg: FakeAccessControlPg) -> None:
    _admin(pg, "a")
    with pytest.raises(LastAdminError, match="last enabled admin"):
        await roles.delete_principal("a")
    assert pg.principal("a") is not None
    assert pg.policy("a") is not None


async def test_deleting_an_admin_while_another_remains_is_allowed(pg: FakeAccessControlPg) -> None:
    _admin(pg, "a")
    _admin(pg, "b")
    await roles.delete_principal("a")
    assert pg.principal("a") is None


async def test_a_keys_only_admin_counts(pg: FakeAccessControlPg) -> None:
    # Any enabled principal whose own policy is admin-shaped counts, whatever its kind.
    _admin(pg, "a")
    pg.add_principal("owner", kind="human", display_name="owner")
    pg.add_policy("owner", scopes=["*"])
    await roles.set_principal_disabled("a", True)
    assert pg.principal("a")["disabled"] is True


# -- the key/policy doors ---------------------------------------------------------


async def test_key_door_edit_that_demotes_the_last_admin_principal_is_refused(pg: FakeAccessControlPg) -> None:
    _admin(pg, "a")
    with pytest.raises(LastAdminError, match="last enabled admin"):
        await management.edit_user_payload("a", scopes=[])
    assert pg.policy_body("a") == _ADMIN_BODY


async def test_key_door_edit_keeping_the_admin_shape_is_allowed(pg: FakeAccessControlPg) -> None:
    _admin(pg, "a")
    committed = await management.edit_user_payload("a", policy_data={"note": "kept"})
    assert committed == {"scopes": ["*"], "policy_data": {"note": "kept"}, "condition": None}


async def test_key_door_edit_with_another_admin_is_allowed(pg: FakeAccessControlPg) -> None:
    _admin(pg, "a")
    _admin(pg, "b")
    committed = await management.edit_user_payload("a", scopes=[])
    assert committed is not None
    assert committed["scopes"] == []


async def test_key_door_edit_of_a_key_row_writes_as_before(pg: FakeAccessControlPg) -> None:
    # A key row has no principal: it never counts as an admin principal, so no count runs.
    pg.add_policy("key-1", scopes=["*"])
    committed = await management.edit_user_payload("key-1", scopes=[])
    assert committed is not None
    assert committed["scopes"] == []
    assert not any(s.startswith("SELECT pg_advisory_xact_lock") for s in pg.executed)


async def test_key_door_edit_of_an_unknown_user_is_none(pg: FakeAccessControlPg) -> None:
    assert await management.edit_user_payload("ghost", scopes=[]) is None


async def test_rollback_that_demotes_the_last_admin_principal_is_refused(pg: FakeAccessControlPg) -> None:
    _admin(pg, "a")
    with pytest.raises(LastAdminError, match="last enabled admin"):
        await management.restore_policy_body("a", {"scopes": ["read"], "policy_data": {}, "condition": None})
    assert pg.policy_body("a") == _ADMIN_BODY


async def test_rollback_with_another_admin_is_allowed(pg: FakeAccessControlPg) -> None:
    _admin(pg, "a")
    _admin(pg, "b")
    restored = await management.restore_policy_body("a", {"scopes": ["read"], "policy_data": {}, "condition": None})
    assert restored is not None
    assert pg.policy_body("a")["scopes"] == ["read"]


# -- the disabled marker is the principal's server-owned projection --------------------

_NOT_POLICY_CONTENT = "'disabled' is not policy content"


@pytest.mark.parametrize("cleared", [{}, None])
async def test_key_door_edit_clearing_policy_data_keeps_a_disabled_principal_disabled(
    pg: FakeAccessControlPg, cleared: dict[str, object] | None
) -> None:
    _admin(pg, "a")
    _admin(pg, "b")
    await roles.set_principal_disabled("b", True)
    committed = await management.edit_user_payload("b", policy_data=cleared)
    assert committed == {"scopes": ["*"], "policy_data": {"disabled": True}, "condition": None}
    assert pg.policy_body("b")["policy_data"] == {"disabled": True}
    assert pg.principal("b")["disabled"] is True


@pytest.mark.parametrize("other_admin", [False, True])
async def test_key_door_edit_writing_the_disabled_marker_onto_an_enabled_principal_is_refused(
    pg: FakeAccessControlPg, other_admin: bool
) -> None:
    # Not a last-admin refusal: the marker is never policy content, whatever the admin count.
    _admin(pg, "a")
    if other_admin:
        _admin(pg, "b")
    with pytest.raises(ValueError, match=_NOT_POLICY_CONTENT):
        await management.edit_user_payload("a", policy_data={"disabled": True})
    assert pg.policy_body("a") == _ADMIN_BODY
    assert pg.principal("a")["disabled"] is False


async def test_key_door_edit_clearing_the_marker_of_a_disabled_principal_is_refused(pg: FakeAccessControlPg) -> None:
    _admin(pg, "a")
    _admin(pg, "b", disabled=True)
    with pytest.raises(ValueError, match=_NOT_POLICY_CONTENT):
        await management.edit_user_payload("b", policy_data={"disabled": False})
    assert pg.policy_body("b")["policy_data"] == {"disabled": True}
    assert pg.principal("b")["disabled"] is True


async def test_key_door_edit_round_tripping_the_stored_marker_is_accepted(pg: FakeAccessControlPg) -> None:
    _admin(pg, "a")
    _admin(pg, "b", disabled=True)
    committed = await management.edit_user_payload("b", policy_data={"disabled": True, "note": "kept"})
    assert committed is not None
    assert committed["policy_data"] == {"disabled": True, "note": "kept"}
    assert pg.principal("b")["disabled"] is True


async def test_key_door_edit_round_tripping_an_enabled_state_writes_no_marker(pg: FakeAccessControlPg) -> None:
    _admin(pg, "a")
    committed = await management.edit_user_payload("a", policy_data={"disabled": False, "note": "kept"})
    assert committed == {"scopes": ["*"], "policy_data": {"note": "kept"}, "condition": None}
    assert pg.principal("a")["disabled"] is False


async def test_key_door_edit_writing_the_disabled_marker_onto_a_key_row_is_refused(pg: FakeAccessControlPg) -> None:
    pg.add_policy("key-1", scopes=["*"], policy_data={"note": "kept"})
    with pytest.raises(ValueError, match=_NOT_POLICY_CONTENT):
        await management.edit_user_payload("key-1", policy_data={"disabled": True})
    assert pg.policy_body("key-1")["policy_data"] == {"note": "kept"}


@pytest.mark.parametrize("value", [1, "true", "yes", None])
@pytest.mark.parametrize("disabled", [False, True])
async def test_key_door_edit_with_a_non_boolean_disabled_is_refused(
    pg: FakeAccessControlPg, value: object, disabled: bool
) -> None:
    # A non-boolean value is never the stored state (the claim is ``True`` or absent).
    _admin(pg, "a")
    _admin(pg, "b", disabled=disabled)
    stored = {"disabled": True} if disabled else {}
    with pytest.raises(ValueError, match=_NOT_POLICY_CONTENT):
        await management.edit_user_payload("b", policy_data={"disabled": value})
    assert pg.policy_body("b")["policy_data"] == stored
    assert pg.principal("b")["disabled"] is disabled


async def test_rollback_to_a_body_carrying_the_disabled_marker_leaves_the_principal_enabled(
    pg: FakeAccessControlPg,
) -> None:
    # "a" is the only admin: a rollback never changes its enabled state, so no count refuses it.
    _admin(pg, "a")
    restored = await management.restore_policy_body(
        "a", {"scopes": ["*"], "policy_data": {"disabled": True, "note": "v1"}, "condition": None}
    )
    assert restored == {"scopes": ["*"], "policy_data": {"note": "v1"}, "condition": None}
    assert pg.policy_body("a") == restored
    assert pg.principal("a")["disabled"] is False


async def test_rollback_to_a_body_without_the_marker_keeps_a_disabled_principal_disabled(
    pg: FakeAccessControlPg,
) -> None:
    _admin(pg, "a")
    _admin(pg, "b", disabled=True)
    restored = await management.restore_policy_body("b", {"scopes": ["*"], "policy_data": {}, "condition": None})
    assert restored == {"scopes": ["*"], "policy_data": {"disabled": True}, "condition": None}
    assert pg.policy_body("b") == restored
    assert pg.principal("b")["disabled"] is True


async def test_an_admin_whose_policy_carries_the_disabled_marker_is_not_counted(pg: FakeAccessControlPg) -> None:
    _admin(pg, "a")
    _admin(pg, "b")
    await roles.set_principal_disabled("b", True)
    with pytest.raises(LastAdminError, match="last enabled admin"):
        await roles.set_principal_disabled("a", True)
    with pytest.raises(LastAdminError, match="last enabled admin"):
        await roles.delete_principal("a")
    with pytest.raises(LastAdminError, match="last enabled admin"):
        await management.edit_user_payload("a", scopes=[])
    assert pg.principal("a")["disabled"] is False
    assert pg.policy_body("a") == _ADMIN_BODY


async def test_changing_an_admin_whose_policy_carries_the_disabled_marker_needs_no_count(
    pg: FakeAccessControlPg,
) -> None:
    _admin(pg, "a")
    _admin(pg, "b")
    await roles.set_principal_disabled("b", True)
    await roles.delete_principal("b")
    assert pg.principal("b") is None


# -- the mint never takes a ``disabled`` claim from its caller -------------------------


@pytest.mark.parametrize("value", [True, False, "true"])
async def test_mint_refuses_a_disabled_claim_and_writes_nothing(
    pg: FakeAccessControlPg, identity_provider: _SpyProvider, value: object
) -> None:
    _admin(pg, "a")
    with pytest.raises(ValueError, match=_NOT_POLICY_CONTENT):
        await management.add_user_api_key("k1", "d", [], {"disabled": value, "note": "n"}, owner_user_id="a")
    assert pg.policy("k1") is None
    assert "k1" not in identity_provider.identities


async def test_mint_without_a_disabled_claim_writes_an_enabled_key(pg: FakeAccessControlPg) -> None:
    _admin(pg, "a")
    _raw, body, _fingerprint = await management.add_user_api_key("k1", "d", [], {"note": "n"}, owner_user_id="a")
    assert "disabled" not in body["policy_data"]
    assert "disabled" not in pg.policy_body("k1")["policy_data"]


async def test_a_principal_created_over_a_minted_key_id_reads_as_enforced(pg: FakeAccessControlPg) -> None:
    # The mint plants no marker, so a principal that takes over a key's row shows the
    # enabled state enforcement acts on.
    await seed_default_roles()
    _admin(pg, "a")
    with pytest.raises(ValueError, match=_NOT_POLICY_CONTENT):
        await management.add_user_api_key("k5", "d", [], {"disabled": True}, owner_user_id="a")
    await management.add_user_api_key("k5", "d", [], None, owner_user_id="a")
    await roles.create_principal("k5", kind="service", display_name="K5", created_by="a", role="admin")
    assert pg.principal("k5")["disabled"] is False
    assert "disabled" not in pg.policy_body("k5")["policy_data"]


# -- the doors map the refusal to 409 ------------------------------------------------


def _admin_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    caller = Caller(caller_id="root", policy=AccessPolicy(scopes=["*"]), is_admin=True, owner_claim=None)

    async def _resolve() -> Caller:
        return caller

    monkeypatch.setattr(api_keys_ops, "resolve_caller", _resolve)


async def test_edit_api_key_door_maps_the_refusal_to_409(
    pg: FakeAccessControlPg, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tai42_skeleton.access_control.settings import AccessControlSettings

    monkeypatch.setattr(api_keys_ops, "access_control_settings", lambda: AccessControlSettings(enable=True))
    _admin_caller(monkeypatch)
    _admin(pg, "a")
    with pytest.raises(ConflictError, match="last enabled admin"):
        await api_keys_ops.edit_api_key("a", {"scopes": []})
    assert pg.policy_body("a") == _ADMIN_BODY


@pytest.mark.parametrize("other_admin", [False, True])
async def test_edit_api_key_door_refuses_the_disabled_marker_with_400(
    pg: FakeAccessControlPg, monkeypatch: pytest.MonkeyPatch, other_admin: bool
) -> None:
    from tai42_skeleton.access_control.settings import AccessControlSettings

    monkeypatch.setattr(api_keys_ops, "access_control_settings", lambda: AccessControlSettings(enable=True))
    _admin_caller(monkeypatch)
    _admin(pg, "a")
    if other_admin:
        _admin(pg, "b")
    with pytest.raises(BadRequestError, match=_NOT_POLICY_CONTENT):
        await api_keys_ops.edit_api_key("a", {"policy_data": {"disabled": True}})
    assert pg.policy_body("a") == _ADMIN_BODY
    assert pg.principal("a")["disabled"] is False


async def test_create_api_key_door_refuses_a_disabled_claim_with_400(
    pg: FakeAccessControlPg, monkeypatch: pytest.MonkeyPatch, identity_provider: _SpyProvider
) -> None:
    from tai42_skeleton.access_control.settings import AccessControlSettings

    monkeypatch.setattr(api_keys_ops, "access_control_settings", lambda: AccessControlSettings(enable=True))
    _admin_caller(monkeypatch)
    _admin(pg, "root")
    with pytest.raises(BadRequestError, match=_NOT_POLICY_CONTENT):
        await api_keys_ops.create_api_key("k2", "d", [], {"disabled": True}, None, None)
    assert pg.policy("k2") is None
    assert "k2" not in identity_provider.identities


async def test_modify_scopes_door_maps_the_refusal_to_409(
    pg: FakeAccessControlPg, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tai42_skeleton.access_control.settings import AccessControlSettings

    monkeypatch.setattr(api_keys_ops, "access_control_settings", lambda: AccessControlSettings(enable=True))
    _admin_caller(monkeypatch)
    _admin(pg, "a")
    with pytest.raises(ConflictError, match="last enabled admin"):
        await api_keys_ops.modify_api_key_scopes("a", remove=["*"])
    assert pg.policy_body("a") == _ADMIN_BODY


async def test_rollback_door_maps_the_refusal_to_409(pg: FakeAccessControlPg, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from tai42_skeleton.access_control.settings import AccessControlSettings

    monkeypatch.setattr(api_keys_ops, "access_control_settings", lambda: AccessControlSettings(enable=True))
    _admin_caller(monkeypatch)

    class _History:
        async def get_version(self, _user_id: str, _version: int) -> SimpleNamespace:
            return SimpleNamespace(body={"scopes": ["read"], "policy_data": {}, "condition": None})

        async def rollback(self, _user_id: str, _version: int) -> None:  # pragma: no cover - refused first
            raise AssertionError("the history pointer must not move on a refusal")

    monkeypatch.setattr(api_keys_ops, "ac_policy_store", lambda: _History())
    _admin(pg, "a")
    with pytest.raises(ConflictError, match="last enabled admin"):
        await api_keys_ops.rollback_policy("a", 1)
    assert pg.policy_body("a") == _ADMIN_BODY


# -- principal_roles ----------------------------------------------------------------


async def test_principal_roles_reports_admin_pointer_none_and_absent(pg: FakeAccessControlPg) -> None:
    _admin(pg, "a")
    pg.add_principal("e", kind="human", display_name="e")
    pg.add_policy(
        "e",
        scopes=["*"],
        policy_data={ROLE_POINTER_KEY: "editor"},
        condition={"content": "true", "id": None, "kwargs": {}},
    )
    pg.add_principal("raw", kind="service", display_name="raw")
    pg.add_policy("raw", scopes=["read"])
    roles_by_id = await roles.principal_roles(["a", "e", "raw", "ghost"])
    assert roles_by_id == {"a": RESERVED_ADMIN_ROLE, "e": "editor", "raw": None}


async def test_principal_roles_of_no_ids_is_empty(pg: FakeAccessControlPg) -> None:
    assert await roles.principal_roles([]) == {}
