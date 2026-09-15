"""Logging settings read from the ``TAI_LOG_LEVEL`` environment variable."""

import logging

from pydantic import field_validator
from pydantic_settings import SettingsConfigDict

from tai42_kit.settings import TaiBaseSettings, settings_cache


class LoggingSettings(TaiBaseSettings):
    """Process logging configuration sourced from ``TAI_``-prefixed environment variables."""

    # ``env_prefix`` merges down the MRO on top of ``TaiBaseSettings`` config, so
    # the single ``log_level`` field reads ``TAI_LOG_LEVEL``.
    model_config = SettingsConfigDict(env_prefix="TAI_")

    log_level: str = "INFO"

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Normalise ``log_level`` to upper case, raising when it is not a known level name."""
        mapping = logging.getLevelNamesMapping()
        upper_v = v.upper()
        if upper_v not in mapping:
            raise ValueError(f"Invalid log level: {v}. Must be one of {list(mapping)}")
        return upper_v

    def _level_num(self) -> int:
        return logging.getLevelNamesMapping()[self.log_level.upper()]

    def is_enabled_for(self, level_name: str) -> bool:
        """Whether the configured level would emit a record logged at ``level_name``."""
        mapping = logging.getLevelNamesMapping()
        return mapping[self.log_level.upper()] <= mapping[level_name.upper()]


@settings_cache
def logging_settings() -> LoggingSettings:
    """Return the cached :class:`LoggingSettings` instance."""
    return LoggingSettings()
