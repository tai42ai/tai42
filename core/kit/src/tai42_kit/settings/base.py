"""Kit base for env-sourced settings groups and their reload disposition."""

import json
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from tai42_kit.settings.env_file import DEFAULT_ENV_FILE, TaiDotEnvSettingsSource

# Reload disposition of a settings group (or a single field) across a settings
# epoch flip: ``hot`` re-reads live, ``recycle`` needs its pooled resource torn
# down and rebuilt, ``excluded`` must not change without a full process restart.
ReloadClass = Literal["hot", "recycle", "excluded"]

# Field type for env-sourced key material (KEKs, HMAC/signing keys). A
# ``SecretStr`` so masking applies everywhere a secret does; the ``key_material``
# flag is what the registry surfaces so downstream policy can refuse to expose it.
KeyMaterial = Annotated[SecretStr, Field(json_schema_extra={"key_material": True})]


class TaiBaseSettings(BaseSettings):
    """Base for env-configurable settings groups; concrete subclasses self-register.

    A settings class reads the env file named by its ``tai_env_file`` class attribute
    (default ``.env``); the file is parsed once per file identity. A subclass that sets
    ``model_config['env_file']`` is refused at import.
    """

    # The env file is named by ``tai_env_file`` and read by the kit's dotenv source,
    # which serves one parse per file identity; the framework's own source reads
    # nothing (``env_file=None``). ``only_existing`` resolves the fields only: the
    # unmatched dotenv keys it skips are what ``extra="ignore"`` discards.
    # ``model_config`` merges down the MRO, so subclasses inherit this.
    model_config = SettingsConfigDict(
        env_file=None,
        dotenv_filtering="only_existing",
        validate_default=True,
        env_ignore_empty=True,
        extra="ignore",
    )

    # The env file this group reads, relative to the working directory; ``None``
    # reads none. Read only if it exists (absent under K8s, where the cluster
    # injects env vars).
    tai_env_file: ClassVar[str | Path | None] = DEFAULT_ENV_FILE

    # Group-level reload disposition, read with inheriting ``getattr`` semantics
    # so a subclass inherits its base's declaration. Kit ships only the ``hot``
    # default; core-owned classes declare ``recycle``/``excluded`` downstream.
    reload_class: ClassVar[ReloadClass] = "hot"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Put the kit's cached dotenv source in the dotenv slot.

        Every other option of the source is read from ``settings_cls.model_config``,
        as the framework's own source reads it.
        """
        dotenv = TaiDotEnvSettingsSource(
            settings_cls,
            env_file=cls.tai_env_file,
            _init_state=dotenv_settings._init_state,
        )
        return (init_settings, env_settings, dotenv, file_secret_settings)

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        """Self-register each concrete subclass with fields as an env-configurable group."""
        # Runs after pydantic has built the model (``model_fields`` populated) —
        # unlike ``__init_subclass__``, which fires before. Every concrete
        # subclass self-registers as an env-configurable group.
        super().__pydantic_init_subclass__(**kwargs)
        # The kit's dotenv source reads ``tai_env_file``; a ``model_config`` env file
        # would be read by nothing, so declaring one is refused at import.
        if cls.model_config.get("env_file") is not None:
            raise TypeError(
                f"{cls.__qualname__}: tai42 settings declare their env file with the 'tai_env_file' "
                "class attribute; model_config['env_file'] is not read"
            )
        # Own-attribute check (not ``getattr``): a concrete subclass of an
        # excluded abstract base still registers, since the flag is not inherited
        # into the subclass ``__dict__``. A ``ClassVar`` is the pydantic-safe way
        # to carry this — a leading-underscore attr would be swallowed as a
        # private attribute and inherited via ``__private_attributes__``.
        if cls.__dict__.get("registry_exclude") is True:
            return
        # No fields → no group to register.
        if not cls.model_fields:
            return
        # Import at registration time, not module import: the base module takes
        # no import-time dependency on the registry (only reached once a concrete
        # subclass with fields is defined).
        from tai42_kit.settings.registry import _register

        _register(cls)

    def with_fallbacks(self, user_config: dict) -> dict:
        """Return a dict with defaults added for missing keys (never override user config)."""
        defaults = self.model_dump(exclude_none=True)
        return {**defaults, **(user_config or {})}  # user_config always wins on conflict

    def load_with_fallbacks(self, user_config: str) -> dict:
        """Parse ``user_config`` JSON (or empty) and merge it over the defaults."""
        return self.with_fallbacks(json.loads(user_config) if user_config else {})
