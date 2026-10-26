"""Fixture defining a plugin-style settings group with one ``excluded`` field ON IMPORT.

Imported after the boot env bridge applied the stored env, so the group's registration is what
makes the field's env name part of the X band.
"""

from pydantic import Field
from pydantic_settings import SettingsConfigDict
from tai42_kit.settings import TaiBaseSettings


class LateExcludedSettings(TaiBaseSettings):
    """A settings group whose one field only a full restart changes."""

    model_config = SettingsConfigDict(env_prefix="LATE_EXCLUDED_")

    identity: str | None = Field(default=None, json_schema_extra={"reload": "excluded"})
