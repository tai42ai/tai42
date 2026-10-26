"""Fixture defining a plugin-style settings group ON IMPORT.

Loaded via a manifest ``lifecycle_modules`` entry, so the group registers inside
``app_context``, after the boot env bridge applied the stored env. Its one field is read
under two env names.
"""

from pydantic import AliasChoices, Field
from pydantic_settings import SettingsConfigDict
from tai42_kit.settings import TaiBaseSettings


class LateAliasedSettings(TaiBaseSettings):
    """A settings group with one field read under two env names, the first one preferred."""

    model_config = SettingsConfigDict(env_prefix="LATE_ALIASED_")

    endpoint: str | None = Field(
        default=None, validation_alias=AliasChoices("LATE_ALIASED_ENDPOINT", "LATE_ALIASED_URL")
    )
