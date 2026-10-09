"""Settings machinery: the base class, the cache-reset registry, and the owned-prefix audit.

Leaf settings live next to the impl they configure (``tai42_kit.llm``, ``tai42_kit.clients``,
``tai42_kit.logging``).
"""

from tai42_kit.settings.base import ENV_IGNORE_EMPTY, KeyMaterial, ReloadClass, TaiBaseSettings, present_env_value
from tai42_kit.settings.cache_registry import (
    StaleHolder,
    keyed_settings_cache,
    register_settings_reset,
    reset_all_settings,
    restamp_settings_born_after,
    settings_birth_mark,
    settings_cache,
    sweep_stale_settings,
)
from tai42_kit.settings.default_namespace import (
    TAI_DEFAULT_ENV_PREFIX,
    DefaultNamespaceMixin,
)
from tai42_kit.settings.env_file import DEFAULT_ENV_FILE, env_file_identity
from tai42_kit.settings.owned_prefix import (
    KUBERNETES_SERVICE_LINK_SUFFIX_RE,
    UnknownOwnedSettingError,
    is_kubernetes_service_link,
    kubernetes_service_link_names,
    owned_env_prefixes,
    refuse_unknown_owned_env,
    unknown_owned_env_keys,
)
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
    "ENV_IGNORE_EMPTY",
    "KUBERNETES_SERVICE_LINK_SUFFIX_RE",
    "TAI_DEFAULT_ENV_PREFIX",
    "DefaultNamespaceMixin",
    "KeyMaterial",
    "ReloadClass",
    "SettingsClassInfo",
    "SettingsFieldInfo",
    "StaleHolder",
    "TaiBaseSettings",
    "UnknownOwnedSettingError",
    "env_file_identity",
    "is_kubernetes_service_link",
    "keyed_settings_cache",
    "kubernetes_service_link_names",
    "not_configured_message",
    "owned_env_prefixes",
    "present_env_value",
    "refuse_unknown_owned_env",
    "register_settings_reset",
    "registered_settings",
    "require",
    "require_secret",
    "reset_all_settings",
    "restamp_settings_born_after",
    "settings_birth_mark",
    "settings_cache",
    "sweep_stale_settings",
    "unknown_owned_env_keys",
]
