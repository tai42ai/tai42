import json
from typing import Annotated, Any, ClassVar, Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Reload disposition of a settings group (or a single field) across a settings
# epoch flip: ``hot`` re-reads live, ``recycle`` needs its pooled resource torn
# down and rebuilt, ``excluded`` must not change without a full process restart.
ReloadClass = Literal["hot", "recycle", "excluded"]

# Field type for env-sourced key material (KEKs, HMAC/signing keys). A
# ``SecretStr`` so masking applies everywhere a secret does; the ``key_material``
# flag is what the registry surfaces so downstream policy can refuse to expose it.
KeyMaterial = Annotated[SecretStr, Field(json_schema_extra={"key_material": True})]


class TaiBaseSettings(BaseSettings):
    # ``env_file=".env"`` is read only if the file exists (absent under K8s,
    # where the cluster injects env vars) — a side-effect-free replacement for
    # an import-time ``load_dotenv()``. ``model_config`` merges down the MRO, so
    # subclasses inherit this without restating it.
    model_config = SettingsConfigDict(
        env_file=".env",
        validate_default=True,
        env_ignore_empty=True,
        extra="ignore",
    )

    # Group-level reload disposition, read with inheriting ``getattr`` semantics
    # so a subclass inherits its base's declaration. Kit ships only the ``hot``
    # default; core-owned classes declare ``recycle``/``excluded`` downstream.
    reload_class: ClassVar[ReloadClass] = "hot"

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        # Runs after pydantic has built the model (``model_fields`` populated) —
        # unlike ``__init_subclass__``, which fires before. Every concrete
        # subclass self-registers as an env-configurable group.
        super().__pydantic_init_subclass__(**kwargs)
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
        return self.with_fallbacks(json.loads(user_config) if user_config else {})
