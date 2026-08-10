"""The settings-profiles contract: the :class:`SettingsProfileBody` persisted
shape, the profile-specific errors, and the :class:`SettingsProfileStore` Protocol.

A *settings profile* is a named, versioned snapshot of the profile-managed env
band. It is a typed VIEW over the generic versioned-document store
(``kind="settings_profile"``): the store holds the opaque body, this Protocol is
the typed interface over it. tai42-contract owns only the Protocol + model +
errors; the concrete view that delegates to the store, validates/reshapes the
body, and maps the generic errors to these profile errors lives in the skeleton
(no versioning code in the view).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tai42_contract.settings_profiles.errors import (
    SettingsProfileError,
    SettingsProfileExistsError,
    SettingsProfileNotFoundError,
    SettingsProfileVersionNotFoundError,
)
from tai42_contract.settings_profiles.models import SettingsProfileBody
from tai42_contract.versioning.models import DocumentRecord, DocumentVersion


@runtime_checkable
class SettingsProfileStore(Protocol):
    """The typed interface over the versioned-document store with
    ``kind="settings_profile"``.

    Delegation, body validation/reshaping, and error mapping are the concrete
    (skeleton) view's job — this Protocol pins only the surface. A profile body is
    always the full :class:`SettingsProfileBody` (``{description, env,
    secret_keys}``); a version save persists the whole body (whole-map, never a
    per-field merge — the env band is self-contained).
    """

    async def create_profile(self, name: str, body: SettingsProfileBody) -> DocumentRecord:
        """Create a versioned settings profile from the full ``body``. Raise
        :class:`SettingsProfileExistsError` on a duplicate name."""
        ...

    async def save_version(self, name: str, body: SettingsProfileBody) -> DocumentVersion:
        """Append a new version carrying the full ``body`` (whole-body replace, not a
        per-field merge). Raise :class:`SettingsProfileNotFoundError` if the profile
        is absent."""
        ...

    async def list_profiles(self) -> list[DocumentRecord]:
        """List the active versioned settings profiles."""
        ...

    async def get_profile(self, name: str) -> DocumentRecord:
        """Fetch a profile's active record. Raise :class:`SettingsProfileNotFoundError`
        if absent."""
        ...

    async def get_active_body(self, name: str) -> SettingsProfileBody:
        """Return the FULL active-version body (``{description, env, secret_keys}``).
        Raise :class:`SettingsProfileNotFoundError` if absent."""
        ...

    async def list_versions(self, name: str) -> list[DocumentVersion]:
        """List every version of the profile, each carrying its ``is_current`` signal.
        Raise :class:`SettingsProfileNotFoundError` if the profile is absent."""
        ...

    async def get_version(self, name: str, version: int) -> DocumentVersion:
        """Fetch one version. Raise :class:`SettingsProfileVersionNotFoundError` if
        that version does not exist."""
        ...

    async def rollback(self, name: str, version: int) -> DocumentRecord:
        """Re-point the active version to ``version`` (no data copy). Raise
        :class:`SettingsProfileVersionNotFoundError` if that version does not exist."""
        ...

    async def soft_delete(self, name: str) -> None:
        """Soft-delete the profile, keeping its version history (audit). Raise
        :class:`SettingsProfileNotFoundError` if absent."""
        ...


__all__ = [
    "SettingsProfileBody",
    "SettingsProfileError",
    "SettingsProfileExistsError",
    "SettingsProfileNotFoundError",
    "SettingsProfileStore",
    "SettingsProfileVersionNotFoundError",
]
