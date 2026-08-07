"""Settings profiles: the concrete typed store view over the generic
versioned-document store (``kind="settings_profile"``).

A *settings profile* is a named, versioned snapshot of the profile-managed env
band. tai42-contract owns the :class:`~tai42_contract.settings_profiles.SettingsProfileStore`
Protocol + :class:`~tai42_contract.settings_profiles.SettingsProfileBody` model +
the profile errors; this package holds the concrete view that persists and
versions it.
"""

from __future__ import annotations

from tai42_skeleton.settings_profiles.store import SettingsProfileStoreView, settings_profile_store

__all__ = ["SettingsProfileStoreView", "settings_profile_store"]
