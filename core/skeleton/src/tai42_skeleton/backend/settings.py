"""Backend dispatch settings — the manifest key, task timeout and tool-name arg, from ``BACKEND_*``."""

from pydantic_settings import SettingsConfigDict
from tai42_kit.backend import BackendDispatchSettings
from tai42_kit.settings import TaiBaseSettings, settings_cache


class BackendSettings(BackendDispatchSettings, TaiBaseSettings):
    """The host side of the backend dispatch wiring, read from ``BACKEND_*`` env.

    The manifest key, task timeout and tool-name arg are the shared dispatch group's, so the host
    and every execution backend name them identically.
    """

    model_config = SettingsConfigDict(env_prefix="BACKEND_")


@settings_cache
def base_backend_settings() -> BackendSettings:
    """The process-cached :class:`BackendSettings`."""
    return BackendSettings()
