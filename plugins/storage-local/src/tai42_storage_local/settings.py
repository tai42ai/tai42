"""Environment-configured settings for the local-filesystem storage backend."""

from __future__ import annotations

from pydantic_settings import SettingsConfigDict
from tai42_kit.settings import TaiBaseSettings, settings_cache


class LocalStorageSettings(TaiBaseSettings):
    """``STORAGE_LOCAL_*`` settings for the local-filesystem storage backend."""

    model_config = SettingsConfigDict(
        env_prefix="STORAGE_LOCAL_",
    )

    # The storage root; unset (or empty) leaves the backend unconfigured and every call refuses.
    root_path: str | None = None
    create_dirs: bool = True


@settings_cache
def storage_settings() -> LocalStorageSettings:
    """The cached local-storage settings, re-read on a settings reload."""
    return LocalStorageSettings()
