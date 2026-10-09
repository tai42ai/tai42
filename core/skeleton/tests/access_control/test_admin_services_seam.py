"""The ``AccountsAdminServices`` seam: a stand-in accounts provider reaches the guarded methods.

The stand-in receives the services as ``settings.admin`` exactly as any accounts-provider
factory does (installed by ``AuthAdapter``), never by importing the application, and
drives the three guarded methods and ``principal_roles``. The last enabled admin
principal is refused with the contract's ``LastAdminError``; nothing is written.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from tai42_contract.accounts import AccountsAdminServices
from tai42_contract.accounts.errors import LastAdminError

import tai42_skeleton.versioning as versioning_module
from tai42_skeleton.access_control import management
from tai42_skeleton.access_control.adapter import AuthAdapter
from tai42_skeleton.access_control.roles import ROLE_POINTER_KEY, seed_default_roles
from tai42_skeleton.access_control.settings import AccessControlSettings

from .conftest import FakeAccessControlPg, FakeRedis, make_client_ctx
from .test_policy_store import _MemStore
from .test_principals import _SpyProvider


class _StandInDirectory:
    """A neutral accounts provider stand-in: it keeps its people by id and delegates every
    principal change to the services it was handed."""

    def __init__(self, settings: Any) -> None:
        self._admin: AccountsAdminServices = settings.admin

    async def change_role(self, person: str, role: str) -> None:
        await self._admin.apply_role(person, role)

    async def suspend(self, person: str) -> None:
        await self._admin.set_user_disabled(person, True)

    async def forget(self, person: str) -> None:
        await self._admin.remove_policy(person)

    async def roles_of(self, people: list[str]) -> Mapping[str, str | None]:
        return await self._admin.principal_roles(people)


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch) -> None:
    mem = _MemStore()
    monkeypatch.setattr(versioning_module, "versioned_store", lambda: mem)
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "secret")
    monkeypatch.setattr(management, "client_ctx", make_client_ctx(FakeRedis(strings={})))
    from tai42_kit.access_control import registry

    spy = _SpyProvider()
    registry._PROVIDERS._generation.committed()["redis"] = lambda _settings: spy


@pytest.fixture
def directory() -> _StandInDirectory:
    settings = AccessControlSettings()
    AuthAdapter(settings)
    assert isinstance(settings.admin, AccountsAdminServices)
    return _StandInDirectory(settings)


def _admin_principal(pg: FakeAccessControlPg, user_id: str) -> None:
    pg.add_principal(user_id, kind="human", display_name=user_id)
    pg.add_policy(user_id, scopes=["*"])


async def test_the_last_admin_is_refused_on_every_guarded_method(
    pg: FakeAccessControlPg, directory: _StandInDirectory
) -> None:
    await seed_default_roles()
    _admin_principal(pg, "person-1")
    with pytest.raises(LastAdminError):
        await directory.change_role("person-1", "viewer")
    with pytest.raises(LastAdminError):
        await directory.suspend("person-1")
    with pytest.raises(LastAdminError):
        await directory.forget("person-1")
    assert pg.policy_body("person-1") == {"scopes": ["*"], "policy_data": {}, "condition": None}
    assert pg.principal("person-1")["disabled"] is False


async def test_with_another_admin_every_guarded_method_passes(
    pg: FakeAccessControlPg, directory: _StandInDirectory
) -> None:
    await seed_default_roles()
    for person in ("person-1", "person-2", "person-3", "keeper"):
        _admin_principal(pg, person)
    await directory.change_role("person-1", "viewer")
    await directory.suspend("person-2")
    await directory.forget("person-3")
    assert pg.policy_body("person-1")["policy_data"] == {ROLE_POINTER_KEY: "viewer"}
    assert pg.principal("person-2")["disabled"] is True
    assert pg.principal("person-3") is None


async def test_roles_are_read_through_the_seam(pg: FakeAccessControlPg, directory: _StandInDirectory) -> None:
    await seed_default_roles()
    _admin_principal(pg, "person-1")
    _admin_principal(pg, "person-2")
    await directory.change_role("person-2", "editor")
    assert await directory.roles_of(["person-1", "person-2", "nobody"]) == {"person-1": "admin", "person-2": "editor"}
