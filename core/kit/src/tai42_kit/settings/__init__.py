"""Settings machinery: the base class, the cache-reset registry, and the owned-prefix audit.

Leaf settings live next to the impl they configure (``tai42_kit.llm``, ``tai42_kit.clients``,
``tai42_kit.logging``).
"""

from tai42_kit.settings.base import (
    ENV_IGNORE_EMPTY,
    KeyMaterial,
    ReloadClass,
    TaiBaseSettings,
    fold_env_name,
    present_env_value,
)
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
from tai42_kit.settings.env_file import (
    DEFAULT_ENV_FILE,
    env_file_identity,
    env_file_in_effect,
    env_file_reads_suspended,
)
from tai42_kit.settings.env_names import (
    EnvNameIndex,
    SettingsFieldRef,
    settings_field_env_value,
    settings_fields_set_by,
)
from tai42_kit.settings.owned_prefix import (
    KUBERNETES_SERVICE_LINK_SUFFIX_RE,
    UnknownOwnedSettingError,
    is_kubernetes_service_link,
    kubernetes_service_link_names,
    owned_env_prefixes,
    refuse_unknown_owned_env,
    unknown_owned_env_keys,
)
from tai42_kit.settings.reader_descriptions import (
    EnvReader,
    EnvReaderField,
    declare_env_reader,
    decode_env_readers,
    describe_env_reader,
    encode_env_readers,
    env_readers,
    on_env_reader_described,
)
from tai42_kit.settings.registry import (
    SettingsClassInfo,
    SettingsFieldInfo,
    bind_respawn_class_env,
    changes_only_by_respawn,
    on_settings_registered,
    registered_settings,
    respawn_class_env_drift,
    unbind_respawn_class_env,
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
    "EnvNameIndex",
    "EnvReader",
    "EnvReaderField",
    "KeyMaterial",
    "ReloadClass",
    "SettingsClassInfo",
    "SettingsFieldInfo",
    "SettingsFieldRef",
    "StaleHolder",
    "TaiBaseSettings",
    "UnknownOwnedSettingError",
    "bind_respawn_class_env",
    "changes_only_by_respawn",
    "declare_env_reader",
    "decode_env_readers",
    "describe_env_reader",
    "encode_env_readers",
    "env_file_identity",
    "env_file_in_effect",
    "env_file_reads_suspended",
    "env_readers",
    "fold_env_name",
    "is_kubernetes_service_link",
    "keyed_settings_cache",
    "kubernetes_service_link_names",
    "not_configured_message",
    "on_env_reader_described",
    "on_settings_registered",
    "owned_env_prefixes",
    "present_env_value",
    "refuse_unknown_owned_env",
    "register_settings_reset",
    "registered_settings",
    "require",
    "require_secret",
    "reset_all_settings",
    "respawn_class_env_drift",
    "restamp_settings_born_after",
    "settings_birth_mark",
    "settings_cache",
    "settings_field_env_value",
    "settings_fields_set_by",
    "sweep_stale_settings",
    "unbind_respawn_class_env",
    "unknown_owned_env_keys",
]
