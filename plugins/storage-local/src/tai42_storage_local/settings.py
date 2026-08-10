"""Environment-configured settings for the local-filesystem storage backend."""

from __future__ import annotations

from pydantic_settings import SettingsConfigDict
from tai42_kit.settings import TaiBaseSettings, settings_cache


class LocalStorageSettings(TaiBaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="STORAGE_LOCAL_",
    )

    root_path: str = "./templates"
    create_dirs: bool = True


@settings_cache
def storage_settings() -> LocalStorageSettings:
    return LocalStorageSettings()
