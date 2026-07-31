"""Settings machinery: the base class + the cache-reset registry. Leaf settings
live next to the impl they configure (``tai42_kit.llm``, ``tai42_kit.clients``,
``tai42_kit.logging``)."""

from tai42_kit.settings.base import TaiBaseSettings
from tai42_kit.settings.cache_registry import (
    register_settings_reset,
    reset_all_settings,
    settings_cache,
)
from tai42_kit.settings.registry import (
    SettingsClassInfo,
    SettingsFieldInfo,
    registered_settings,
)

__all__ = [
    "SettingsClassInfo",
    "SettingsFieldInfo",
    "TaiBaseSettings",
    "register_settings_reset",
    "registered_settings",
    "reset_all_settings",
    "settings_cache",
]
