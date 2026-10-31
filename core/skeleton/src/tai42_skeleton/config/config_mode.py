"""Configuration mode — the provider-selection seam.

Defines the ``ConfigMode`` built-in modes and the ``ConfigModeSettings`` boot
setting that names which :class:`~tai42_contract.config.manager.ConfigManager`
provider backs the deployment. The factory (:mod:`tai42_skeleton.config.factory`)
reads ``config_mode()`` and resolves it to a provider module.

Environment variables
---------------------
- ``TAI_CONFIG_MODE`` -- ``file`` (default, built in). Any other value names an
  external provider plugin, resolved by convention to ``tai42-config-<mode>``.

The mode string is used verbatim to build the provider module name, so it is
validated but never normalized — an out-of-shape value fails loudly rather than
being silently coerced. Provider-specific settings live with their provider, not
here — this module names only the seam.
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from pydantic import field_validator
from pydantic_settings import SettingsConfigDict
from tai42_contract.config.manager import CONFIG_MODE_ENV, CONFIG_MODE_PATTERN, DEFAULT_CONFIG_DIR, FILE_CONFIG_MODE
from tai42_kit.settings import ReloadClass, TaiBaseSettings, settings_cache


class ConfigMode(StrEnum):
    """Config modes the skeleton ships built in.

    Only ``file`` is built in. Any other mode is an open string resolved by the
    factory's naming convention to a separately-installed ``tai42-config-<mode>``
    provider plugin, so it is deliberately not enumerated here.
    """

    file = FILE_CONFIG_MODE


class ConfigModeSettings(TaiBaseSettings):
    """Reads ``TAI_CONFIG_MODE`` (+ ``TAI_CONFIG_DIR_PATH``) and validates the mode."""

    model_config = SettingsConfigDict(
        env_prefix="TAI_",
    )

    # Config-location seam: the mode selects where profiles live and the dir path
    # roots the .env/manifest a profile would be read from — neither can be carried
    # by a profile, so the whole group is excluded from the reload boundary.
    reload_class: ClassVar[ReloadClass] = "excluded"

    config_mode: str = ConfigMode.file

    # ``TAI_CONFIG_DIR_PATH`` — the bootstrap config root. The file provider reads
    # it directly (``file_manager.FileConfigManager.__init__``); this field exists
    # only to register the key as an excluded boundary member, never to serve the read.
    # Its default is the provider's own fallback dir, so the registered field states it.
    config_dir_path: str = DEFAULT_CONFIG_DIR

    @field_validator("config_mode", mode="before")
    @classmethod
    def validate_config_mode(cls, v: object) -> str:
        """Validate ``config_mode`` is ``file`` or a well-formed provider mode name, returning it."""
        if not isinstance(v, str) or CONFIG_MODE_PATTERN.fullmatch(v) is None:
            raise ValueError(
                f"Invalid {CONFIG_MODE_ENV}={v!r}: expected {FILE_CONFIG_MODE!r} (built-in) or a config-provider "
                f"mode name matching {CONFIG_MODE_PATTERN.pattern}, resolved to the tai42-config-<mode> plugin."
            )
        return v


@settings_cache
def config_mode() -> str:
    """Return the active config mode as a plain string.

    ``'file'`` selects the built-in provider; any other value resolves by
    convention to the ``tai42-config-<mode>`` plugin.
    """
    return str(ConfigModeSettings().config_mode)
