"""The concrete :class:`~tai42_contract.settings_profiles.SettingsProfileStore` view.

A thin typed wrapper over the generic
:class:`~tai42_contract.versioning.VersionedStore` with ``kind="settings_profile"``
and body :class:`~tai42_contract.settings_profiles.SettingsProfileBody`
(``{description, env, secret_keys}``). It holds NO SQL of its own — all persistence
is the generic store. Its jobs are:

* **body typing** — a version body is always the FULL :class:`SettingsProfileBody`,
  persisted whole-body (the env band is self-contained; there is no per-field
  carry-forward — unlike presets, a profile save carries the whole map);
* **error mapping** — the generic store's errors become the profile error types
  (:class:`SettingsProfileNotFoundError` / :class:`SettingsProfileExistsError` /
  :class:`SettingsProfileVersionNotFoundError`).
"""

from __future__ import annotations

from tai42_contract.settings_profiles import (
    SettingsProfileBody,
    SettingsProfileStore,
)
from tai42_contract.settings_profiles.errors import (
    SettingsProfileExistsError,
    SettingsProfileNotFoundError,
    SettingsProfileVersionNotFoundError,
)
from tai42_contract.versioning import VersionedStore
from tai42_contract.versioning.errors import DocumentExistsError, DocumentNotFoundError, DocumentVersionNotFoundError
from tai42_contract.versioning.models import DocumentRecord, DocumentVersion

_KIND = "settings_profile"


class SettingsProfileStoreView(SettingsProfileStore):
    """Typed settings-profile view delegating to a generic :class:`VersionedStore`."""

    def __init__(self, store: VersionedStore) -> None:
        self._store = store

    async def create_profile(self, name: str, body: SettingsProfileBody) -> DocumentRecord:
        try:
            return await self._store.create(_KIND, name, body.model_dump())
        except DocumentExistsError as exc:
            raise SettingsProfileExistsError(name) from exc

    async def save_version(self, name: str, body: SettingsProfileBody) -> DocumentVersion:
        try:
            return await self._store.save_version(_KIND, name, body.model_dump())
        except DocumentNotFoundError as exc:
            raise SettingsProfileNotFoundError(name) from exc

    async def list_profiles(self) -> list[DocumentRecord]:
        return await self._store.list(_KIND)

    async def get_profile(self, name: str) -> DocumentRecord:
        try:
            return await self._store.get(_KIND, name)
        except DocumentNotFoundError as exc:
            raise SettingsProfileNotFoundError(name) from exc

    async def get_active_body(self, name: str) -> SettingsProfileBody:
        try:
            raw = await self._store.get_active_body(_KIND, name)
        except DocumentNotFoundError as exc:
            raise SettingsProfileNotFoundError(name) from exc
        return SettingsProfileBody.model_validate(raw)

    async def list_versions(self, name: str) -> list[DocumentVersion]:
        try:
            return await self._store.list_versions(_KIND, name)
        except DocumentNotFoundError as exc:
            raise SettingsProfileNotFoundError(name) from exc

    async def get_version(self, name: str, version: int) -> DocumentVersion:
        try:
            return await self._store.get_version(_KIND, name, version)
        except DocumentVersionNotFoundError as exc:
            raise SettingsProfileVersionNotFoundError(name, version) from exc

    async def rollback(self, name: str, version: int) -> DocumentRecord:
        try:
            return await self._store.rollback(_KIND, name, version)
        except DocumentVersionNotFoundError as exc:
            raise SettingsProfileVersionNotFoundError(name, version) from exc

    async def soft_delete(self, name: str) -> None:
        try:
            await self._store.soft_delete(_KIND, name)
        except DocumentNotFoundError as exc:
            raise SettingsProfileNotFoundError(name) from exc


def settings_profile_store() -> SettingsProfileStoreView:
    """Build the active settings-profile view over the generic versioned-document store."""
    from tai42_skeleton.versioning import versioned_store

    return SettingsProfileStoreView(versioned_store())
