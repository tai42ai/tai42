"""The acting caller's READ reach over feature tags, resolved through the caller's stored standing."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from tai42_contract.access_control.context import reset_request_user_id, set_request_user_id
from tai42_contract.versioning.errors import DocumentNotFoundError

import tai42_skeleton.access_control.feature_reach as feature_reach
import tai42_skeleton.access_control.policy as policy_module
import tai42_skeleton.access_control.role_grants as role_grants_module
import tai42_skeleton.access_control.store as store_module
from tai42_skeleton.access_control.feature_reach import readable_feature_tags
from tai42_skeleton.access_control.request_scopes import (
    reset_request_identity_claims,
    reset_request_is_admin,
    set_request_identity_claims,
    set_request_is_admin,
)
from tai42_skeleton.access_control.roles import ROLE_POINTER_KEY, grantable_feature_tags
from tai42_skeleton.access_control.settings import AccessControlSettings
from tai42_skeleton.operations.errors import PermissionDeniedError

from .conftest import FakeAccessControlPg, FakeRedis, make_client_ctx, make_pg_ctx

# Feature tags a role's level opens, one the base tier keeps for an admin, and one no route carries.
_ASKED = frozenset({"presets", "hooks", "schedules", "states", "access-control", "no-such-feature"})


@pytest.fixture(autouse=True)
def policies(monkeypatch: pytest.MonkeyPatch) -> FakeAccessControlPg:
    """A stored policy per principal: one under a role, one with no role pointer."""
    pg = FakeAccessControlPg()
    pg.add_policy("member", scopes=["presets"], policy_data={ROLE_POINTER_KEY: "member-role"})
    pg.add_policy("unroled", scopes=["presets"])
    monkeypatch.setattr(store_module, "client_ctx", make_pg_ctx(pg))
    monkeypatch.setattr(policy_module, "client_ctx", make_client_ctx(FakeRedis()))
    return pg


@pytest.fixture
def role_grants(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, str]]:
    """The stored grant map of each role a test defines; a role absent here is a deleted role."""
    roles: dict[str, dict[str, str]] = {}

    async def _resolve(role_name: str, _version: int) -> dict[str, str]:
        if role_name not in roles:
            raise DocumentNotFoundError("role", role_name)
        return roles[role_name]

    monkeypatch.setattr(role_grants_module, "resolve_role_grants", _resolve)
    return roles


@contextmanager
def _acting(user_id: str | None, *, admin: bool = False) -> Iterator[None]:
    user = set_request_user_id(user_id)
    claims = set_request_identity_claims(None if user_id is None else {"sub": user_id})
    is_admin = set_request_is_admin(admin)
    try:
        yield
    finally:
        reset_request_is_admin(is_admin)
        reset_request_identity_claims(claims)
        reset_request_user_id(user)


def test_the_asked_tags_hold_both_a_tag_a_level_opens_and_a_registered_admin_only_tag() -> None:
    grantable = grantable_feature_tags()
    assert {"presets", "hooks", "schedules", "states"} <= grantable
    assert "access-control" not in grantable
    assert "no-such-feature" not in grantable


async def test_a_role_member_reads_the_tags_its_role_grants_read_or_write_on(
    role_grants: dict[str, dict[str, str]],
) -> None:
    role_grants["member-role"] = {"presets": "read", "schedules": "write", "hooks": "none"}
    with _acting("member"):
        assert await readable_feature_tags(_ASKED) == {"presets", "schedules"}


async def test_a_principal_no_role_governs_reads_every_tag_a_level_opens(
    role_grants: dict[str, dict[str, str]],
) -> None:
    with _acting("unroled"):
        assert await readable_feature_tags(_ASKED) == {"presets", "hooks", "schedules", "states"}


async def test_a_role_pointer_naming_a_deleted_role_reads_no_tag(role_grants: dict[str, dict[str, str]]) -> None:
    with _acting("member"):
        assert await readable_feature_tags(_ASKED) == frozenset()


async def test_an_admin_reads_every_asked_tag_registered_or_not() -> None:
    with _acting("member", admin=True):
        assert await readable_feature_tags(_ASKED) == _ASKED


async def test_no_caller_bound_reads_no_tag() -> None:
    with _acting(None):
        assert await readable_feature_tags(_ASKED) == frozenset()


async def test_access_control_off_reads_every_asked_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feature_reach, "access_control_settings", lambda: AccessControlSettings(enable=False))
    with _acting(None):
        assert await readable_feature_tags(_ASKED) == _ASKED


async def test_a_principal_with_no_policy_is_refused_with_the_standing_reason() -> None:
    with _acting("stranger"), pytest.raises(PermissionDeniedError, match=r"^access denied: principal has no policy$"):
        await readable_feature_tags(_ASKED)


async def test_no_asked_tag_reads_none_without_resolving_the_caller() -> None:
    with _acting("stranger"):
        assert await readable_feature_tags(()) == frozenset()
