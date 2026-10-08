"""Settings machinery: the base class + the cache-reset registry.

Leaf settings live next to the impl they configure (``tai42_kit.llm``, ``tai42_kit.clients``,
``tai42_kit.logging``).
"""

from tai42_kit.settings.base import KeyMaterial, ReloadClass, TaiBaseSettings
from tai42_kit.settings.cache_registry import (
    StaleHolder,
    keyed_settings_cache,
    register_settings_reset,
    reset_all_settings,
    settings_cache,
    sweep_stale_settings,
)
from tai42_kit.settings.default_namespace import (
    TAI_DEFAULT_ENV_PREFIX,
    DefaultNamespaceMixin,
)
from tai42_kit.settings.env_file import DEFAULT_ENV_FILE, env_file_identity
from tai42_kit.settings.registry import (
    SettingsClassInfo,
    SettingsFieldInfo,
    registered_settings,
)
from tai42_kit.settings.require import (
    not_configured_message,
    require,
    require_secret,
)

__all__ = [
    "DEFAULT_ENV_FILE",
    "TAI_DEFAULT_ENV_PREFIX",
    "DefaultNamespaceMixin",
    "KeyMaterial",
    "ReloadClass",
    "SettingsClassInfo",
    "SettingsFieldInfo",
    "StaleHolder",
    "TaiBaseSettings",
    "env_file_identity",
    "keyed_settings_cache",
    "not_configured_message",
    "register_settings_reset",
    "registered_settings",
    "require",
    "require_secret",
    "reset_all_settings",
    "settings_cache",
    "sweep_stale_settings",
]
