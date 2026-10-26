"""Env readers as plain data: every env name a pydantic-settings class reads, and how it nests.

An :class:`EnvReader` describes one pydantic-settings class the way the settings layer reads it
from the env: its nested delimiter, whether it reads names case-sensitively, and per field the
env names it accepts, the alias names a delimited path may use for it, and the reader of a
nested settings group it holds. A description carries names and shapes, never a value, so a
process can send it to another, and the other can judge which fields a name sets for a reader it
never imported (:class:`~tai42_kit.settings.EnvNameIndex`).

The env readers of a process are every registered settings group, described at its
registration, and every reader declared with :func:`declare_env_reader`: a pydantic-settings
class that reads the applied env without being a settings group (no schema entry, no reload
class, no ``TAI_DEFAULT_*`` fallback). Each description reaches the :func:`on_env_reader_described`
listeners.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from pydantic import AliasChoices, BaseModel, ConfigDict, TypeAdapter
from pydantic_settings import BaseSettings

from tai42_kit.settings.registry import (
    _accepted_env_vars,
    _field_type,
    _qualname,
    _registered_readers,
    _settings_member,
)

if TYPE_CHECKING:
    from pydantic.fields import FieldInfo

# The JSON-schema types whose values pydantic-settings reads as complex (decoded from JSON,
# and filled from delimited names below the field).
_COMPLEX_TYPES = frozenset({"object", "array"})


class EnvReaderField(BaseModel):
    """One field of an env reader: the env names that set it and how a delimited path names it."""

    model_config = ConfigDict(frozen=True)

    name: str
    # Every env name the reader accepts for the field, as the settings layer reads it.
    env_names: tuple[str, ...]
    # The string names the field's alias declares; a delimited path segment may name the field by one.
    aliases: tuple[str, ...]
    # The qualified name of the settings class the field holds, for a nested settings group.
    nested: str | None = None
    # Whether the field's value is an object or an array, read from JSON and from delimited names below it.
    complex: bool = False


class EnvReader(BaseModel):
    """One pydantic-settings class as the settings layer reads it from the env; names and shapes, never values."""

    model_config = ConfigDict(frozen=True)

    qualname: str
    nested_delimiter: str | None
    nested_max_split: int | None
    case_sensitive: bool
    fields: tuple[EnvReaderField, ...]


_READERS = TypeAdapter(tuple[EnvReader, ...])

# The declared readers, keyed by qualified name: a re-declaration replaces the description.
_DECLARED: dict[str, EnvReader] = {}

# Called with each description, in subscription order (see :func:`on_env_reader_described`).
_DESCRIBED_LISTENERS: list[Callable[[EnvReader], None]] = []


def _alias_strings(field_info: FieldInfo) -> tuple[str, ...]:
    """The string names a field's alias declares: a plain alias, or every string choice of an ``AliasChoices``."""
    alias = field_info.validation_alias or field_info.alias
    if isinstance(alias, str):
        return (alias,)
    if isinstance(alias, AliasChoices):
        return tuple(choice for choice in alias.choices if isinstance(choice, str))
    return ()


def _describe(cls: type[BaseSettings], field_type: Callable[[str, FieldInfo], str]) -> EnvReader:
    """The description of ``cls``, each scalar field's JSON-schema type given by ``field_type``."""
    config = cls.model_config
    env_prefix = config.get("env_prefix", "")
    case_sensitive = bool(config.get("case_sensitive"))
    fields: list[EnvReaderField] = []
    for name, field_info in cls.model_fields.items():
        member = _settings_member(field_info.annotation)
        if member is not None:
            # A nested group is read as JSON under ``env_prefix + name``, its alias aside.
            env_name = env_prefix + name
            fields.append(
                EnvReaderField(
                    name=name,
                    env_names=(env_name if case_sensitive else env_name.upper(),),
                    aliases=_alias_strings(field_info),
                    nested=_qualname(member),
                )
            )
            continue
        fields.append(
            EnvReaderField(
                name=name,
                env_names=tuple(_accepted_env_vars(name, field_info, env_prefix, case_sensitive)),
                aliases=_alias_strings(field_info),
                complex=field_type(name, field_info) in _COMPLEX_TYPES,
            )
        )
    return EnvReader(
        qualname=_qualname(cls),
        nested_delimiter=config.get("env_nested_delimiter") or None,
        nested_max_split=config.get("env_nested_max_split") or None,
        case_sensitive=case_sensitive,
        fields=tuple(fields),
    )


def describe_env_reader(cls: type[BaseSettings]) -> EnvReader:
    """The description of the pydantic-settings class ``cls``: every env name it reads and how it nests.

    A field whose type has no resolvable JSON-schema type raises, naming the field.
    """
    return _describe(cls, _field_type)


def _notify(reader: EnvReader) -> None:
    for listener in _DESCRIBED_LISTENERS:
        listener(reader)


def declare_env_reader(cls: type[BaseSettings]) -> None:
    """Declare ``cls`` a reader of the applied env, with every nested settings class it reaches.

    Each class is described (:func:`describe_env_reader`) and enters :func:`env_readers`, not the
    settings registry; each description reaches the :func:`on_env_reader_described` listeners,
    ``cls`` first. A listener's exception propagates.
    """
    pending: list[type[BaseSettings]] = [cls]
    seen: set[str] = set()
    while pending:
        current = pending.pop(0)
        qualname = _qualname(current)
        if qualname in seen:
            continue
        seen.add(qualname)
        reader = describe_env_reader(current)
        _DECLARED[qualname] = reader
        _notify(reader)
        for field_info in current.model_fields.values():
            member = _settings_member(field_info.annotation)
            if member is not None:
                pending.append(member)


def on_env_reader_described(listener: Callable[[EnvReader], None]) -> Callable[[EnvReader], None]:
    """Subscribe ``listener`` to every env reader described from now on; returns ``listener``.

    Called with the description of each settings group registered (after the
    :func:`~tai42_kit.settings.on_settings_registered` listeners, before any value of it is read)
    and of each declared reader. Subscribing the same listener again does nothing.
    """
    if listener not in _DESCRIBED_LISTENERS:
        _DESCRIBED_LISTENERS.append(listener)
    return listener


def _sort_key(reader: EnvReader) -> tuple[str, str]:
    return reader.qualname, reader.model_dump_json()


def _sorted_unique(readers: Iterable[EnvReader]) -> tuple[EnvReader, ...]:
    """``readers`` sorted, each equal description once."""
    unique = {_sort_key(reader): reader for reader in readers}
    return tuple(unique[key] for key in sorted(unique))


def env_readers() -> tuple[EnvReader, ...]:
    """Every env reader of this process: each registered settings group and each declared reader, sorted."""
    return _sorted_unique([*_registered_readers(), *_DECLARED.values()])


def encode_env_readers(readers: Iterable[EnvReader]) -> str:
    """The canonical JSON of a set of env readers: the same set gives the same text in any order."""
    unique = _sorted_unique(readers)
    return json.dumps([reader.model_dump(mode="json") for reader in unique], sort_keys=True, separators=(",", ":"))


def decode_env_readers(body: str | bytes) -> tuple[EnvReader, ...]:
    """The env readers :func:`encode_env_readers` encoded in ``body``; a body of another shape raises."""
    return _READERS.validate_json(body)
