"""The concrete ``SettingsProfileStoreView`` over an in-memory generic store.

Pins the view's two jobs: delegation to ``kind="settings_profile"`` and the
generic→profile error mapping. The generic store's own Postgres semantics are
covered in ``tests/versioning``; here the store is a faithful in-memory stand-in
so the view logic is exercised in isolation.
"""

from __future__ import annotations

from typing import Any

import pytest
from tai42_contract.settings_profiles import SettingsProfileBody
from tai42_contract.settings_profiles.errors import (
    SettingsProfileExistsError,
    SettingsProfileNotFoundError,
    SettingsProfileVersionNotFoundError,
)
from tai42_contract.versioning import VersionedStore
from tai42_contract.versioning.errors import (
    DocumentExistsError,
    DocumentNotFoundError,
    DocumentVersionNotFoundError,
)
from tai42_contract.versioning.models import DocumentRecord, DocumentVersion

from tai42_skeleton.settings_profiles.store import SettingsProfileStoreView


class _MemStore(VersionedStore):
    """Faithful in-memory ``VersionedStore`` for view tests (one active row per key)."""

    def __init__(self) -> None:
        self.docs: dict[tuple[str, str], dict[str, Any]] = {}

    async def create(self, kind, name, body, tags=None, *, tx=None) -> DocumentRecord:
        key = (kind, name)
        if key in self.docs:
            raise DocumentExistsError(kind, name)
        self.docs[key] = {"active": 1, "versions": {1: (dict(body), list(tags or []))}}
        return DocumentRecord(kind=kind, name=name, active_version=1, is_active=True, created_at="t")

    async def save_version(self, kind, name, body, tags=None, *, tx=None) -> DocumentVersion:
        doc = self.docs.get((kind, name))
        if doc is None:
            raise DocumentNotFoundError(kind, name)
        new_version = max(doc["versions"]) + 1
        doc["versions"][new_version] = (dict(body), list(tags or []))
        doc["active"] = new_version
        return DocumentVersion(version=new_version, body=body, tags=list(tags or []), created_at="t", is_current=True)

    async def list(self, kind) -> list[DocumentRecord]:
        return [
            DocumentRecord(kind=k, name=n, active_version=d["active"], is_active=True, created_at="t")
            for (k, n), d in self.docs.items()
            if k == kind
        ]

    async def get(self, kind, name) -> DocumentRecord:
        doc = self._require(kind, name)
        return DocumentRecord(kind=kind, name=name, active_version=doc["active"], is_active=True, created_at="t")

    async def get_active_body(self, kind, name, *, tx=None, for_update=False) -> dict[str, Any]:
        doc = self._require(kind, name)
        return dict(doc["versions"][doc["active"]][0])

    async def list_versions(self, kind, name) -> list[DocumentVersion]:
        doc = self._require(kind, name)
        return [
            DocumentVersion(version=v, body=body, tags=tags, created_at="t", is_current=v == doc["active"])
            for v, (body, tags) in sorted(doc["versions"].items())
        ]

    async def get_version(self, kind, name, version, *, tx=None) -> DocumentVersion:
        doc = self.docs.get((kind, name))
        if doc is None or version not in doc["versions"]:
            raise DocumentVersionNotFoundError(kind, name, version)
        body, tags = doc["versions"][version]
        return DocumentVersion(
            version=version, body=body, tags=tags, created_at="t", is_current=version == doc["active"]
        )

    async def rollback(self, kind, name, version, *, tx=None) -> DocumentRecord:
        doc = self.docs.get((kind, name))
        if doc is None or version not in doc["versions"]:
            raise DocumentVersionNotFoundError(kind, name, version)
        doc["active"] = version
        return DocumentRecord(kind=kind, name=name, active_version=version, is_active=True, created_at="t")

    async def soft_delete(self, kind, name) -> None:
        self._require(kind, name)
        del self.docs[(kind, name)]

    async def delete(self, kind, name, *, tx=None) -> None:
        self._require(kind, name)
        del self.docs[(kind, name)]

    async def rename(self, kind, name, new_name) -> DocumentRecord:
        if (kind, new_name) in self.docs:
            raise DocumentExistsError(kind, new_name)
        doc = self._require(kind, name)
        self.docs[(kind, new_name)] = doc
        del self.docs[(kind, name)]
        return DocumentRecord(kind=kind, name=new_name, active_version=doc["active"], is_active=True, created_at="t")

    def transaction(self):  # pragma: no cover - unused by the profile view
        raise NotImplementedError

    def _require(self, kind, name) -> dict[str, Any]:
        doc = self.docs.get((kind, name))
        if doc is None:
            raise DocumentNotFoundError(kind, name)
        return doc


def _body(*, description: str = "d", env=None, secret_keys=None) -> SettingsProfileBody:
    return SettingsProfileBody(
        description=description, env=env or {"API_KEY": "s"}, secret_keys=secret_keys or ["API_KEY"]
    )


@pytest.fixture
def store() -> _MemStore:
    return _MemStore()


@pytest.fixture
def view(store: _MemStore) -> SettingsProfileStoreView:
    return SettingsProfileStoreView(store)


# -- delegation + body round-trip -------------------------------------------


async def test_create_persists_full_body(view, store):
    await view.create_profile("p", _body(env={"A": "1"}, secret_keys=["A"]))
    assert ("settings_profile", "p") in store.docs
    body = await view.get_active_body("p")
    assert body.description == "d"
    assert body.env == {"A": "1"}
    assert body.secret_keys == ["A"]


async def test_save_version_appends_full_body(view):
    await view.create_profile("p", _body(env={"A": "1"}))
    await view.save_version("p", SettingsProfileBody(description="v2", env={"B": "2"}, secret_keys=[]))
    # A profile version is a whole-body replace — no per-field carry-forward.
    body = await view.get_active_body("p")
    assert body.description == "v2"
    assert body.env == {"B": "2"}
    assert body.secret_keys == []
    assert (await view.get_version("p", 1)).body["env"] == {"A": "1"}


async def test_list_profiles_delegates(view):
    await view.create_profile("a", _body())
    await view.create_profile("b", _body())
    assert sorted(r.name for r in await view.list_profiles()) == ["a", "b"]


async def test_list_versions_marks_current(view):
    await view.create_profile("p", _body())
    await view.save_version("p", _body(description="v2"))
    versions = await view.list_versions("p")
    assert [(v.version, v.is_current) for v in versions] == [(1, False), (2, True)]


async def test_rollback_delegates(view):
    await view.create_profile("p", _body(env={"A": "1"}))
    await view.save_version("p", _body(env={"A": "2"}))
    rec = await view.rollback("p", 1)
    assert rec.active_version == 1
    assert (await view.get_active_body("p")).env == {"A": "1"}


async def test_soft_delete_delegates(view, store):
    await view.create_profile("p", _body())
    await view.soft_delete("p")
    assert ("settings_profile", "p") not in store.docs


# -- error mapping ----------------------------------------------------------


async def test_duplicate_maps_to_profile_exists(view):
    await view.create_profile("p", _body())
    with pytest.raises(SettingsProfileExistsError):
        await view.create_profile("p", _body())


async def test_missing_maps_to_profile_not_found(view):
    with pytest.raises(SettingsProfileNotFoundError):
        await view.get_profile("nope")
    with pytest.raises(SettingsProfileNotFoundError):
        await view.get_active_body("nope")
    with pytest.raises(SettingsProfileNotFoundError):
        await view.list_versions("nope")
    with pytest.raises(SettingsProfileNotFoundError):
        await view.save_version("nope", _body())
    with pytest.raises(SettingsProfileNotFoundError):
        await view.soft_delete("nope")


async def test_missing_version_maps_to_profile_version_not_found(view):
    await view.create_profile("p", _body())
    with pytest.raises(SettingsProfileVersionNotFoundError):
        await view.get_version("p", 99)
    with pytest.raises(SettingsProfileVersionNotFoundError):
        await view.rollback("p", 99)
