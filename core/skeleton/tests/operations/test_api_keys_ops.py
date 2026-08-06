"""Op-level characterization for the api-keys/scopes operations.

The route oracles (``tests/routers/test_api_keys*``) drive these ops end to end
through the adapter; this pins the operation-level edge branches directly — the
``_check_scope_subset`` branches, the ops' own ownership rejections, and the
``ValueError -> BadRequestError`` mappings — so each declared error class is
exercised at the operation itself, independent of the route surface. The acting-principal
resolution and the shared ownership predicate those ops call are pinned in
``test_authority``.
"""

from __future__ import annotations

import pytest
from tai42_contract.access_control import OWNER_USER_ID_CLAIM
from tai42_contract.access_control.context import reset_request_user_id, set_request_user_id
from tai42_contract.access_control.models import AccessPolicy

from tai42_skeleton.access_control import management
from tai42_skeleton.access_control.settings import AccessControlSettings
from tai42_skeleton.operations import _authority as authority
from tai42_skeleton.operations import api_keys as ops
from tai42_skeleton.operations.errors import BadRequestError, ForbiddenError, NotFoundError, NotSupportedError


def _caller(*, caller_id="c", scopes=None, is_admin=False, owner_claim=None) -> authority.Caller:
    return authority.Caller(
        caller_id=caller_id,
        policy=AccessPolicy(scopes=scopes or []),
        is_admin=is_admin,
        owner_claim=owner_claim,
    )


def test_check_scope_subset_wildcard_caller_grants_anything():
    # A ``"*"`` caller may grant any scope — the early return, no excess computed.
    ops._check_scope_subset(_caller(scopes=["*"]), ["anything", "at-all"])


# -- ownership rejections ----------------------------------------------------


async def test_edit_api_key_non_admin_unknown_key_is_not_found(monkeypatch):
    monkeypatch.setattr(ops, "resolve_caller", lambda: _make(_caller(caller_id="alice")))

    async def _no_body(_user_id):
        return None

    monkeypatch.setattr(management, "get_policy_body", _no_body)
    with pytest.raises(NotFoundError, match="user not found"):
        await ops.edit_api_key("ghost", {"description": "d"})


async def test_edit_api_key_non_admin_not_owned_is_forbidden(monkeypatch):
    monkeypatch.setattr(ops, "resolve_caller", lambda: _make(_caller(caller_id="alice")))

    async def _bob_body(_user_id):
        return {"policy_data": {OWNER_USER_ID_CLAIM: "bob"}}

    monkeypatch.setattr(management, "get_policy_body", _bob_body)
    with pytest.raises(ForbiddenError, match="only edit API keys you own"):
        await ops.edit_api_key("k1", {"description": "d"})


async def test_edit_api_key_non_admin_scope_superset_is_bad_request(monkeypatch):
    monkeypatch.setattr(ops, "resolve_caller", lambda: _make(_caller(caller_id="alice", scopes=["read"])))

    async def _own_body(_user_id):
        return {"policy_data": {OWNER_USER_ID_CLAIM: "alice"}}

    monkeypatch.setattr(management, "get_policy_body", _own_body)
    with pytest.raises(BadRequestError, match="exceed your own"):
        await ops.edit_api_key("k1", {"scopes": ["read", "write"]})


# -- ValueError -> BadRequestError mappings ----------------------------------


async def test_add_scope_url_value_error_maps_to_bad_request(monkeypatch):
    monkeypatch.setattr(ops, "access_control_settings", lambda: AccessControlSettings(enable=True))

    async def _boom(_scope_id, _url, _pattern):
        raise ValueError("bad scope mapping")

    monkeypatch.setattr(management, "add_url_to_scope", _boom)
    with pytest.raises(BadRequestError, match="bad scope mapping"):
        await ops.add_scope_url("s", "/u", None)


async def test_delete_scope_value_error_maps_to_bad_request(monkeypatch):
    async def _boom(_scope_id):
        raise ValueError("cannot delete")

    monkeypatch.setattr(management, "remove_scope", _boom)
    with pytest.raises(BadRequestError, match="cannot delete"):
        await ops.delete_scope("s")


async def test_revoke_api_key_value_error_maps_to_bad_request(monkeypatch):
    monkeypatch.setattr(ops, "resolve_caller", lambda: _make(_caller(is_admin=True)))

    async def _boom(_user_id):
        raise ValueError("revoke failed")

    monkeypatch.setattr(management, "revoke_api_key", _boom)
    with pytest.raises(BadRequestError, match="revoke failed"):
        await ops.revoke_api_key("k1")


async def test_rollback_policy_restore_value_error_maps_to_bad_request(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(ops, "resolve_caller", lambda: _make(_caller(is_admin=True)))
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "secret")

    class _Store:
        async def get_version(self, _user_id, _version):
            return SimpleNamespace(body={"scopes": []})

    monkeypatch.setattr(ops, "ac_policy_store", lambda: _Store())

    async def _boom(_user_id, _body):
        raise ValueError("restore rejected")

    monkeypatch.setattr(management, "restore_policy_body", _boom)
    with pytest.raises(BadRequestError, match="restore rejected"):
        await ops.rollback_policy("k1", 1)


# -- OFF state: access control disabled --------------------------------


async def test_list_scopes_disabled_answers_empty(monkeypatch):
    # Gate disabled → the honest empty mapping, no store touched.
    monkeypatch.setattr(ops, "access_control_settings", lambda: AccessControlSettings(enable=False))
    assert await ops.list_scopes() == {}


async def test_create_api_key_disabled_refuses(monkeypatch):
    # Gate disabled → refuse the mint with a named, machine-readable reason. The OFF
    # gate fires before resolve_caller, so no acting principal is needed.
    monkeypatch.setattr(ops, "access_control_settings", lambda: AccessControlSettings(enable=False))
    with pytest.raises(NotSupportedError) as exc_info:
        await ops.create_api_key("u", "d", [], None, None, None, None, None)
    assert exc_info.value.extra["code"] == "access-control-disabled"


async def test_list_reads_disabled_answer_empty(monkeypatch):
    # Every disabled READ door answers its honest empty shape, never a store read
    # under the synthetic admin.
    monkeypatch.setattr(ops, "access_control_settings", lambda: AccessControlSettings(enable=False))
    assert await ops.list_routes([]) == []
    assert await ops.list_public_routes() == []
    assert await ops.list_tokens_payload() == []


async def test_edit_and_revoke_disabled_refuse(monkeypatch):
    # Both key mutations refuse with the -disabled code when access control is off.
    monkeypatch.setattr(ops, "access_control_settings", lambda: AccessControlSettings(enable=False))
    with pytest.raises(NotSupportedError) as edit_exc:
        await ops.edit_api_key("u", {"description": "x"})
    assert edit_exc.value.extra["code"] == "access-control-disabled"
    with pytest.raises(NotSupportedError) as revoke_exc:
        await ops.revoke_api_key("u")
    assert revoke_exc.value.extra["code"] == "access-control-disabled"


async def test_residual_mutations_disabled_refuse(monkeypatch):
    # Every remaining scope/public-route/claim/policy mutation refuses with the
    # -disabled code, gated BEFORE any store access — an anonymous caller (synthetic
    # admin with the gate off) can never write into the disabled feature's store.
    monkeypatch.setattr(ops, "access_control_settings", lambda: AccessControlSettings(enable=False))

    # Any store touch would be a bug: fail loudly if the gate lets execution fall through.
    def _boom(*_a, **_k):
        raise AssertionError("store touched with access control disabled")

    for attr in (
        "add_url_to_scope",
        "remove_url_from_scope",
        "remove_scope",
        "pin_route_public",
        "unpin_public_route",
        "restore_policy_body",
    ):
        monkeypatch.setattr(management, attr, _boom)
    monkeypatch.setattr(ops, "resolve_caller", lambda: _make(_caller(is_admin=True)))

    async def _call(coro):
        with pytest.raises(NotSupportedError) as exc:
            await coro
        assert exc.value.extra["code"] == "access-control-disabled"

    await _call(ops.add_scope_url("s", "/u", None))
    await _call(ops.remove_scope_url("/u"))
    await _call(ops.delete_scope("s"))
    await _call(ops.pin_public_route("/u", None))
    await _call(ops.unpin_public_route("/u"))
    await _call(ops.create_claim_link("sk-x", None))
    await _call(ops.rollback_policy("u", 1))


async def test_role_and_version_reads_disabled_answer_empty(monkeypatch):
    # The two admin-only reads answer the honest empty list when access control is off,
    # never a store read under the synthetic admin.
    monkeypatch.setattr(ops, "access_control_settings", lambda: AccessControlSettings(enable=False))
    assert await ops.list_roles() == []
    assert await ops.list_policy_versions("u") == []


# -- helper ------------------------------------------------------------------


async def _make(caller: authority.Caller) -> authority.Caller:
    return caller


@pytest.fixture(autouse=True)
def _no_bound_caller():
    # Ensure the request-user contextvar is clean around each op-level test.
    token = set_request_user_id(None)
    try:
        yield
    finally:
        reset_request_user_id(token)
