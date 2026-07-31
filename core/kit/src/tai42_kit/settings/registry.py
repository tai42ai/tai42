"""Registry of every concrete ``TaiBaseSettings`` subclass, captured as plain
data so a UI can list the env-configurable groups without importing live model
classes.

A subclass self-registers through ``TaiBaseSettings.__pydantic_init_subclass__``
(base.py). This module holds the storage and the extraction that turns a model's
``model_fields`` into JSON-safe field metadata."""

import enum
import inspect
import json
import types
from collections.abc import Callable
from typing import Any, Union, cast, get_args, get_origin

from pydantic import AliasChoices, BaseModel, SecretBytes, SecretStr, TypeAdapter
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings


class SettingsFieldInfo(BaseModel):
    """One env-configurable field on a settings class, as plain data."""

    name: str
    env_var: str
    type: str
    default: Any | None = None
    required: bool
    secret: bool
    description: str | None = None
    nested_group: str | None = None


class SettingsClassInfo(BaseModel):
    """A registered settings class, identified by its qualified name."""

    name: str
    module: str
    qualname: str
    fields: list[SettingsFieldInfo]


# Keyed by qualified name (``module.__qualname__``) so a module re-import
# replaces its registration instead of duplicating the group — the same keying
# rationale as ``cache_registry.py``.
_REGISTRY: dict[str, SettingsClassInfo] = {}


def _qualname(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


def _json_safe(value: Any) -> Any | None:
    """Return ``value`` if it JSON-serializes, else ``None`` (no default emitted)."""
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return None
    return value


def _union_members(annotation: Any) -> list[Any]:
    """Members of a union annotation, or the annotation itself if not a union."""
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        return list(get_args(annotation))
    return [annotation]


def _settings_member(annotation: Any) -> type[BaseSettings] | None:
    """The nested ``BaseSettings`` class in the annotation (unwrapping unions), if any."""
    for member in _union_members(annotation):
        if isinstance(member, type) and issubclass(member, BaseSettings):
            return member
    return None


def _is_secret(field_info: FieldInfo) -> bool:
    """A field is secret if its annotation is/contains ``SecretStr``/``SecretBytes``,
    or it is explicitly flagged via ``json_schema_extra={"secret": True}``."""
    for member in _union_members(field_info.annotation):
        if isinstance(member, type) and issubclass(member, (SecretStr, SecretBytes)):
            return True
    extra = field_info.json_schema_extra
    return isinstance(extra, dict) and extra.get("secret") is True


def _resolve_ref(ref: str, defs: dict[str, Any]) -> dict[str, Any]:
    """Resolve a local ``#/$defs/Name`` reference against a schema's ``$defs``.

    Raises on any reference that cannot be resolved — an unresolvable ``$ref`` is
    a bug to surface, never a silent fallback."""
    prefix = "#/$defs/"
    if not ref.startswith(prefix):
        raise ValueError(f"unsupported schema reference {ref!r}")
    key = ref[len(prefix) :]
    if key not in defs:
        raise ValueError(f"unresolvable schema reference {ref!r}")
    return defs[key]


def _resolve_type(schema: dict[str, Any], defs: dict[str, Any], visited: frozenset[str] = frozenset()) -> str:
    """Normalize a pydantic JSON schema to a single JSON-schema type string.

    A ``$ref`` (e.g. the non-null branch of an ``Optional[enum]``) is resolved
    against ``defs`` so an enum reports its underlying scalar type rather than a
    bare ``"object"``. An enum carrying no explicit type maps to ``"string"``.

    ``visited`` tracks the ``$ref`` names on the current resolution stack; a
    ``$ref`` re-encountered while already resolving is a cyclic definition and
    raises rather than recursing without bound."""
    if "$ref" in schema:
        ref = schema["$ref"]
        if ref in visited:
            raise ValueError(f"cyclic schema reference {ref!r}")
        return _resolve_type(_resolve_ref(ref, defs), defs, visited | {ref})
    if "type" in schema:
        return schema["type"]
    for key in ("anyOf", "oneOf"):
        branches = schema.get(key)
        if branches:
            for branch in branches:
                if branch.get("type") == "null":
                    continue
                return _resolve_type(branch, defs, visited)
            return "string"
    if "enum" in schema:
        return "string"
    return "string"


def _field_type(name: str, field_info: FieldInfo) -> str:
    """JSON-schema type string for a scalar field annotation.

    Raises with the field name on an unexpected schema — a scalar with no
    resolvable type is a bug to surface, not to paper over with a default."""
    try:
        schema = TypeAdapter(field_info.annotation).json_schema()
        return _resolve_type(schema, schema.get("$defs", {}))
    except Exception as exc:
        # Re-raise loudly with the field name — an unresolvable scalar type is a
        # bug to surface, never a silent fallback to a default.
        raise ValueError(f"cannot resolve JSON-schema type for field {name!r}") from exc


def _env_var(name: str, field_info: FieldInfo, env_prefix: str, case_sensitive: bool) -> str:
    """Env var a field reads.

    A string alias wins verbatim. An ``AliasChoices`` reports its first string
    choice — the primary env var pydantic-settings reads, which overrides
    ``env_prefix``. With no alias, the name is ``env_prefix + name``, upper-cased
    unless ``case_sensitive`` is set on the model config."""
    alias = field_info.validation_alias or field_info.alias
    if isinstance(alias, str):
        return alias
    if isinstance(alias, AliasChoices):
        for choice in alias.choices:
            if isinstance(choice, str):
                return choice
        raise ValueError(f"field {name!r} has an AliasChoices with no string choice; cannot determine its env var")
    env_var = env_prefix + name
    return env_var if case_sensitive else env_var.upper()


def _factory_needs_validated_data(factory: Any) -> bool:
    """Whether a ``default_factory`` is the pydantic 2.10+ validated-data form.

    Mirrors pydantic's own ``takes_validated_data_argument`` rule: a factory
    needs the already-validated field data — and so cannot be evaluated at
    registration time — iff it has exactly ONE parameter, that parameter can be
    passed positionally (POSITIONAL_ONLY or POSITIONAL_OR_KEYWORD), and it has
    NO default. Every other shape is the plain form pydantic calls with no
    arguments (including a lone positional parameter that carries a default).

    When the signature cannot be introspected (e.g. ``dict``, and some C
    builtins whose signature ``inspect.signature`` rejects with ``ValueError``),
    pydantic treats the factory as the plain no-argument form and calls it — so
    this returns ``False`` via the ``except`` to do the same. Builtins like
    ``list`` and ``set`` DO expose a signature (a lone positional parameter that
    carries a default), so they reach ``False`` through the no-default clause
    below rather than the ``except``; either way the result is the same."""
    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):
        return False
    positional = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    parameters = list(signature.parameters.values())
    return (
        len(parameters) == 1 and parameters[0].kind in positional and parameters[0].default is inspect.Parameter.empty
    )


def _resolve_default(field_info: FieldInfo) -> Any | None:
    """The display default for a non-required, non-secret field.

    A ``default_factory`` that needs validated data cannot be evaluated here, so
    it yields no determinable default (``None``). A plain 0-arg factory is called
    and ANY error it raises propagates loudly — a broken factory is a real bug to
    surface, never something to hide behind a display default. Fields with a
    plain (non-factory) default return that value."""
    factory = field_info.default_factory
    if factory is not None:
        if _factory_needs_validated_data(factory):
            return None
        # Confirmed above not to need validated data, so this is the plain
        # no-argument form pydantic calls with no args.
        return cast("Callable[[], Any]", factory)()
    return field_info.get_default(call_default_factory=False)


def _extract(cls: type[BaseSettings]) -> SettingsClassInfo:
    env_prefix = cls.model_config.get("env_prefix", "")
    case_sensitive = bool(cls.model_config.get("case_sensitive"))
    fields: list[SettingsFieldInfo] = []
    for name, field_info in cls.model_fields.items():
        nested = _settings_member(field_info.annotation)
        if nested is not None:
            # The sub-model self-registers as its own top-level group; this field
            # is a non-editable reference to it, never a duplicate group.
            fields.append(
                SettingsFieldInfo(
                    name=name,
                    env_var="",
                    type="object",
                    default=None,
                    required=False,
                    secret=False,
                    description=field_info.description,
                    nested_group=nested.__name__,
                )
            )
            continue

        secret = _is_secret(field_info)
        required = field_info.is_required()
        if required or secret:
            default: Any | None = None
        else:
            raw_default = _resolve_default(field_info)
            if isinstance(raw_default, enum.Enum):
                # An Enum member is not JSON-serializable, but its ``.value`` (a
                # str/int for the enums used here) is — emit that.
                raw_default = raw_default.value
            default = _json_safe(raw_default)
        fields.append(
            SettingsFieldInfo(
                name=name,
                env_var=_env_var(name, field_info, env_prefix, case_sensitive),
                type=_field_type(name, field_info),
                default=default,
                required=required,
                secret=secret,
                description=field_info.description,
                nested_group=None,
            )
        )
    return SettingsClassInfo(
        name=cls.__name__,
        module=cls.__module__,
        qualname=_qualname(cls),
        fields=fields,
    )


def _register(cls: type[BaseSettings]) -> None:
    """Register (or replace) a settings class in the registry by qualified name."""
    _REGISTRY[_qualname(cls)] = _extract(cls)


def _clear_registry() -> None:
    """Drop every registration — for tests only."""
    _REGISTRY.clear()


def registered_settings() -> list[SettingsClassInfo]:
    """Snapshot of every registered settings group, in registration order.

    Returns model copies so callers cannot mutate the registry's internals."""
    return [info.model_copy(deep=True) for info in _REGISTRY.values()]
