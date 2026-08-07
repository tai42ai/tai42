"""The settings-profile body model — the typed JSONB ``body`` a profile stores
under ``kind="settings_profile"`` in the generic versioned-document store.

This is the SHAPE only. The concrete view that persists and versions it lives in
the skeleton, mirroring the preset view — a contract holds models, never logic.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SettingsProfileBody(BaseModel):
    """The persisted body of a versioned settings profile.

    ``env`` is the whole profile-managed env band (values verbatim, secrets
    included — the store rides the ``secret=True`` versioned-documents backup
    section); applying the profile REPLACES the stored env with it (a key the
    profile does not name is deleted, save the carried X-band). ``secret_keys`` are
    the per-profile secret marks (which ``env`` keys are secret) folded into the
    display mask union. ``description`` is the profile's human description.
    """

    description: str = ""
    env: dict[str, str] = Field(default_factory=dict)
    secret_keys: list[str] = Field(default_factory=list)
