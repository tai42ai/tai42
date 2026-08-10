"""Opt-in ``TAI_DEFAULT_*`` fallback for connection-identity settings.

A settings class mixes in :class:`DefaultNamespaceMixin` and declares a
``tai_default_fields`` map (``{field_name: default_key}``). Each mapped field
that its own specific namespace leaves unset falls back, per field, to the
shared ``TAI_DEFAULT_ + default_key.upper()`` env var — so one ``TAI_DEFAULT_``
namespace can supply the connection identity for every store at once, while any
store-specific value still wins. The mechanism is built entirely on the
documented pydantic-settings customization API (``PydanticBaseSettingsSource``
subclasses composed through ``settings_customise_sources``); no source internals
are overridden, so it holds across the declared ``pydantic-settings>=2.11`` floor.
"""

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

from dotenv import dotenv_values
from pydantic.fields import FieldInfo
from pydantic_settings import PydanticBaseSettingsSource

TAI_DEFAULT_ENV_PREFIX = "TAI_DEFAULT_"

logger = logging.getLogger("tai42_kit.settings.default_namespace")


class _DefaultNamespaceSource(PydanticBaseSettingsSource):
    """Shared ``TAI_DEFAULT_*`` lookup for one settings class.

    Maps each field in ``tai_default_fields`` to the env var
    ``TAI_DEFAULT_ + default_key.upper()``, matched case-insensitively (mirroring
    pydantic-settings' default ``case_sensitive=False``), and skips empty-string
    values (mirroring the model config's ``env_ignore_empty=True``). Subclasses
    supply the name→value mapping to read from. Raw strings are returned;
    pydantic coerces types (``pg_port`` int, ``pg_password`` SecretStr) during
    validation.
    """

    def __init__(
        self,
        settings_cls: type,
        tai_default_fields: Mapping[str, str],
        recorded: list[frozenset[str]],
    ) -> None:
        super().__init__(settings_cls)
        self._tai_default_fields = tai_default_fields
        self._recorded = recorded

    def _values(self) -> Mapping[str, str | None]:
        """The raw ``{env_var: value}`` mapping this source reads from."""
        raise NotImplementedError

    def _resolve_one(self, values: Mapping[str, str], default_key: str) -> str | None:
        """The non-empty value for ``TAI_DEFAULT_ + default_key.upper()``, or None."""
        env_var = f"{TAI_DEFAULT_ENV_PREFIX}{default_key.upper()}"
        raw = values.get(env_var.lower())
        if raw is None or raw == "":
            return None
        return raw

    def _lowered(self) -> dict[str, str]:
        """A lower-cased view of the source's mapping (case-insensitive matching)."""
        return {key.lower(): value for key, value in self._values().items() if value is not None}

    def __call__(self) -> dict[str, Any]:
        values = self._lowered()
        resolved: dict[str, Any] = {}
        for field_name, default_key in self._tai_default_fields.items():
            raw = self._resolve_one(values, default_key)
            if raw is not None:
                resolved[field_name] = raw
        self._recorded.append(frozenset(resolved))
        return resolved

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        # Per-field form of the same lookup; ``__call__`` is the authority the
        # framework invokes for this source.
        default_key = self._tai_default_fields.get(field_name)
        if default_key is None:
            return None, field_name, False
        return self._resolve_one(self._lowered(), default_key), field_name, False


class DefaultNamespaceEnvSource(_DefaultNamespaceSource):
    """Reads ``os.environ`` for the ``TAI_DEFAULT_*`` fallbacks."""

    def _values(self) -> Mapping[str, str | None]:
        return os.environ


class DefaultNamespaceDotEnvSource(_DefaultNamespaceSource):
    """Reads the model's ``env_file`` (``.env``) for the ``TAI_DEFAULT_*`` fallbacks.

    Process env is NOT consulted here — :class:`DefaultNamespaceEnvSource` covers
    it and sits above this source. The configured ``env_file`` is normalized to a
    sequence and read in order with later files overriding earlier ones (matching
    pydantic-settings' own dotenv order); an absent file contributes nothing.
    """

    def _values(self) -> Mapping[str, str | None]:
        env_file = self.config.get("env_file")
        if env_file is None:
            return {}
        if isinstance(env_file, (str, os.PathLike)):
            env_file = [env_file]
        encoding = self.config.get("env_file_encoding")
        merged: dict[str, str | None] = {}
        for entry in env_file:
            path = Path(entry).expanduser()
            if path.is_file() or path.is_fifo():
                merged.update(dotenv_values(path, encoding=encoding or "utf8"))
        return merged


class _RecordingSource(PydanticBaseSettingsSource):
    """Wraps a framework source, recording the keys it emits for provenance.

    Used only for the four framework sources (init, env, dotenv, file-secret);
    the two default-namespace sources record their own emissions natively. The
    wrapper keeps the inner source's identity and data intact:

    - ``__name__`` is set to the inner source's class name so the framework keys
      its per-source states dict by that exact name rather than collapsing every
      wrapped source onto one ``_RecordingSource`` key.
    - ``_set_current_state`` / ``_set_settings_sources_data`` are forwarded to the
      inner source (the framework calls both before ``__call__``), so the inner
      source sees byte-identical data to the unwrapped path.
    """

    def __init__(self, inner: PydanticBaseSettingsSource, recorded: list[frozenset[str]]) -> None:
        super().__init__(inner.settings_cls)
        self._inner = inner
        self._recorded = recorded
        self.__name__ = type(inner).__name__

    def __call__(self) -> dict[str, Any]:
        result = self._inner()
        self._recorded.append(frozenset(result))
        return result

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self._inner.get_field_value(field, field_name)

    def _set_current_state(self, state: dict[str, Any]) -> None:
        forward = getattr(self._inner, "_set_current_state", None)
        if forward is not None:
            forward(state)

    def _set_settings_sources_data(self, states: dict[str, dict[str, Any]]) -> None:
        forward = getattr(self._inner, "_set_settings_sources_data", None)
        if forward is not None:
            forward(states)


class _DefaultNamespaceLogSource(PydanticBaseSettingsSource):
    """Terminal source that logs which fields the default namespace actually won.

    A field is WON when a default source supplied it and no higher-priority source
    did. Emits exactly one INFO line per store when the set is non-empty — field
    names only, never values — and contributes nothing to the resolved settings.
    """

    def __init__(
        self,
        settings_cls: type,
        tai_default_fields: Mapping[str, str],
        recorded: list[frozenset[str]],
        default_start: int,
    ) -> None:
        super().__init__(settings_cls)
        self._tai_default_fields = tai_default_fields
        self._recorded = recorded
        self._default_start = default_start

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        higher = frozenset[str]().union(*self._recorded[: self._default_start])
        default_emitted = frozenset[str]().union(*self._recorded[self._default_start :])
        won = (default_emitted & frozenset(self._tai_default_fields)) - higher
        if won:
            env_prefix = self.config.get("env_prefix", "")
            label = env_prefix if env_prefix else self.settings_cls.__name__
            logger.info("%s: %s ← TAI_DEFAULT_*", label, ", ".join(sorted(won)))
        return {}


class DefaultNamespaceMixin:
    """Adds the ``TAI_DEFAULT_*`` per-field fallback to a settings class.

    Declare ``tai_default_fields`` (``{field_name: default_key}``) on the subclass
    to enable it. An empty map (the default) leaves the source pipeline untouched,
    so a class that mixes this in without a map behaves as one that does not.
    """

    tai_default_fields: ClassVar[Mapping[str, str]] = {}

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type,
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        tai_default_fields = cls.tai_default_fields
        if not tai_default_fields:
            return (init_settings, env_settings, dotenv_settings, file_secret_settings)
        # The four framework sources all carry a specific namespace and sit above
        # the default sources — specific always beats default (file secrets
        # included: it is a specific-namespace source, inert until ``secrets_dir``
        # is ever configured). pydantic-settings merges per top-level key with the
        # earlier source winning, which is exactly the per-field fallback: the
        # specific namespace fills what it sets, the default namespace the rest.
        recorded: list[frozenset[str]] = []
        specific = (init_settings, env_settings, dotenv_settings, file_secret_settings)
        default_start = len(specific)
        return (
            *(_RecordingSource(source, recorded) for source in specific),
            DefaultNamespaceEnvSource(settings_cls, tai_default_fields, recorded),
            DefaultNamespaceDotEnvSource(settings_cls, tai_default_fields, recorded),
            _DefaultNamespaceLogSource(settings_cls, tai_default_fields, recorded, default_start),
        )
