"""Registry of every concrete ``TaiBaseSettings`` subclass, captured as plain data.

A UI can list the env-configurable groups from this data without importing live model classes.

A subclass self-registers through ``TaiBaseSettings.__pydantic_init_subclass__``
(base.py). This module holds the storage and the extraction that turns a model's
``model_fields`` into JSON-safe field metadata.

It also keeps the env a respawn-class field (``recycle`` / ``excluded``) is bound under: while
a lifespan has bound it (:func:`bind_respawn_class_env`), each such field is bound under the
process env as it was when its group first registered in this lifespan, or at the bind for a
group registered before it; nothing but a bind or the process end re-binds it.
"""

import enum
import inspect
import json
import os
import types
from collections.abc import Callable, Mapping
from datetime import timedelta
from pathlib import PurePath
from typing import TYPE_CHECKING, Any, NamedTuple, Union, cast, get_args, get_origin

from pydantic import AliasChoices, BaseModel, PrivateAttr, SecretBytes, SecretStr, TypeAdapter
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings

from tai42_kit.settings.base import ReloadClass
from tai42_kit.settings.default_namespace import TAI_DEFAULT_ENV_PREFIX

if TYPE_CHECKING:
    from tai42_kit.settings.reader_descriptions import EnvReader


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
    # The ``TAI_DEFAULT_*`` env var this field falls back to when its own
    # namespace leaves it unset, or ``None`` for a field with no default-namespace
    # mapping (every behavior knob and every nested-group reference).
    default_namespace_var: str | None = None
    # This field's reload disposition: its own ``json_schema_extra={"reload": ...}``
    # override, else its class's ``reload_class``.
    reload_class: ReloadClass = "hot"
    # True when the field carries key material (the ``KeyMaterial`` type).
    key_material: bool = False
    # Every env name the settings layer reads for this field: the primary env var
    # plus every other string choice of an ``AliasChoices`` (a nested-group
    # reference reads ``env_prefix + name`` as JSON).
    accepted_env_vars: list[str]


class SettingsClassInfo(BaseModel):
    """A registered settings class, identified by its qualified name."""

    name: str
    module: str
    qualname: str
    fields: list[SettingsFieldInfo]
    # The group's declared reload disposition (default ``hot``).
    reload_class: ReloadClass = "hot"
    env_prefix: str
    # Whether the group declares its ``env_prefix`` owned (``TaiBaseSettings.env_prefix_owned``).
    env_prefix_owned: bool
    # The group's env-reader description, made at the extraction; never part of the plain data.
    _env_reader: "EnvReader | None" = PrivateAttr(default=None)


# Keyed by qualified name (``module.__qualname__``) so a module re-import
# replaces its registration instead of duplicating the group — the same keying
# rationale as ``cache_registry.py``.
_REGISTRY: dict[str, SettingsClassInfo] = {}


class _BoundField(NamedTuple):
    """A respawn-class field and the process env it was bound under."""

    group: str
    field: SettingsFieldInfo
    env: Mapping[str, str]


# Each respawn-class field of a registered group bound in this lifespan, keyed by its env var;
# ``None`` while no lifespan has bound it.
_BOUND_ENV: dict[str, _BoundField] | None = None

# Called with each group's registration, in subscription order (see :func:`on_settings_registered`).
_REGISTRATION_LISTENERS: list[Callable[[SettingsClassInfo], None]] = []


def _qualname(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


# A ``timedelta`` default is emitted in pydantic's JSON form (an ISO 8601 duration).
_TIMEDELTA_ADAPTER: TypeAdapter[timedelta] = TypeAdapter(timedelta)


def _json_default(value: Any) -> Any | None:
    """The JSON form of a display default, or ``None`` when it has none (no default emitted).

    A value whose type the settings layer reads from a plain JSON form is emitted in
    that form: an Enum member as its ``.value``, a path as its string, a ``timedelta``
    as its ISO 8601 duration, and a set as a list sorted by each item's JSON text, so
    the emitted order is stable across processes. Any other value is emitted as it
    is when it JSON-serializes, else ``None`` (a value with no JSON representation,
    such as bytes).
    """
    if isinstance(value, enum.Enum):
        value = value.value
    elif isinstance(value, PurePath):
        value = str(value)
    elif isinstance(value, timedelta):
        value = _TIMEDELTA_ADAPTER.dump_python(value, mode="json")
    try:
        if isinstance(value, set | frozenset):
            value = sorted(value, key=json.dumps)
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


def _strip_annotated(annotation: Any) -> Any:
    """The underlying type of an ``Annotated[...]``, else the annotation itself.

    A nullable classified type (e.g. ``KeyMaterial | None``) leaves its non-null
    branch as ``Annotated[SecretStr, FieldInfo(...)]``; unwrap it so the subclass
    check that identifies a secret reaches the ``SecretStr`` underneath.
    """
    if getattr(annotation, "__metadata__", None) is not None:
        return annotation.__origin__
    return annotation


def _extra_dicts(field_info: FieldInfo) -> list[dict[str, Any]]:
    """Every ``json_schema_extra`` mapping reachable from a field's classification.

    Covers the field's own ``json_schema_extra`` plus any carried on ``Annotated``
    metadata nested inside an ``Optional``/``Union`` annotation. Pydantic tucks a
    classified type's flag-bearing ``FieldInfo`` into the union member's metadata
    rather than onto the outer field, so ``KeyMaterial | None`` would otherwise
    lose its ``key_material``/``secret``/``reload`` flags.
    """
    dicts: list[dict[str, Any]] = []
    own = field_info.json_schema_extra
    if isinstance(own, dict):
        dicts.append(own)
    for member in _union_members(field_info.annotation):
        dicts.extend(
            meta.json_schema_extra
            for meta in getattr(member, "__metadata__", ())
            if isinstance(meta, FieldInfo) and isinstance(meta.json_schema_extra, dict)
        )
    return dicts


def _settings_member(annotation: Any) -> type[BaseSettings] | None:
    """The nested ``BaseSettings`` class in the annotation (unwrapping unions), if any."""
    for member in _union_members(annotation):
        if isinstance(member, type) and issubclass(member, BaseSettings):
            return member
    return None


def _is_secret(field_info: FieldInfo) -> bool:
    """Whether a field holds a secret value.

    True if its annotation is/contains ``SecretStr``/``SecretBytes``, or it is explicitly flagged
    via ``json_schema_extra={"secret": True}``.

    Union members are unwrapped through ``Annotated`` so ``KeyMaterial | None``
    (whose non-null branch is ``Annotated[SecretStr, ...]``) is still recognized.
    """
    for member in _union_members(field_info.annotation):
        stripped = _strip_annotated(member)
        if isinstance(stripped, type) and issubclass(stripped, (SecretStr, SecretBytes)):
            return True
    return any(extra.get("secret") is True for extra in _extra_dicts(field_info))


def _is_key_material(field_info: FieldInfo) -> bool:
    """Whether a field holds key material.

    True when flagged via ``json_schema_extra={"key_material": True}`` (the ``KeyMaterial`` type).
    """
    return any(extra.get("key_material") is True for extra in _extra_dicts(field_info))


def _as_reload_class(value: Any, source: str) -> ReloadClass:
    """Narrow an untyped declaration to a ``ReloadClass``.

    Raises with ``source`` on any value outside the literal set — never a silent coercion to a
    default.
    """
    if value in get_args(ReloadClass):
        return cast("ReloadClass", value)
    raise ValueError(f"{source} reload class {value!r} is not one of {get_args(ReloadClass)}")


def _reload_override(field_info: FieldInfo) -> Any | None:
    """The field's own ``json_schema_extra={"reload": ...}`` value, or ``None`` when it has none.

    A field with no override takes its class's ``reload_class``.

    Reads through ``Annotated`` metadata nested in an ``Optional``/``Union`` so a
    per-field override composes with a nullable classified type.
    """
    for extra in _extra_dicts(field_info):
        if "reload" in extra:
            return extra["reload"]
    return None


def _resolve_ref(ref: str, defs: dict[str, Any]) -> dict[str, Any]:
    """Resolve a local ``#/$defs/Name`` reference against a schema's ``$defs``.

    Raises on any reference that cannot be resolved — an unresolvable ``$ref`` is
    a bug to surface, never a silent fallback.
    """
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
    raises rather than recursing without bound.
    """
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
    resolvable type is a bug to surface, not to paper over with a default.
    """
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
    unless ``case_sensitive`` is set on the model config.
    """
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


def _accepted_env_vars(name: str, field_info: FieldInfo, env_prefix: str, case_sensitive: bool) -> list[str]:
    """Every env name the settings layer reads for a scalar field.

    The primary env var (``_env_var``) first, then every other string choice of an
    ``AliasChoices``, each upper-cased unless ``case_sensitive``.
    """
    primary = _env_var(name, field_info, env_prefix, case_sensitive)
    names = [primary if case_sensitive else primary.upper()]
    alias = field_info.validation_alias or field_info.alias
    if isinstance(alias, AliasChoices):
        for choice in alias.choices:
            if isinstance(choice, str):
                candidate = choice if case_sensitive else choice.upper()
                if candidate not in names:
                    names.append(candidate)
    return names


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
    below rather than the ``except``; either way the result is the same.
    """
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
    plain (non-factory) default return that value.
    """
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
    # ``{field_name: default_key}`` map for classes carrying the default-namespace
    # fallback (empty for every other settings class).
    default_fields: Mapping[str, str] = getattr(cls, "tai_default_fields", {})
    # Inheriting read (a subclass inherits its base's declaration); the field-level
    # value falls back to this when a field declares no ``reload`` override.
    class_reload = _as_reload_class(getattr(cls, "reload_class", "hot"), f"class {cls.__name__!r}")
    prefix_owned = getattr(cls, "env_prefix_owned", False) is True
    if prefix_owned and not env_prefix:
        raise ValueError(f"settings group {cls.__name__!r}: env_prefix_owned requires a non-empty env_prefix")
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
                    reload_class=class_reload,
                    accepted_env_vars=[env_prefix + name if case_sensitive else (env_prefix + name).upper()],
                )
            )
            continue

        secret = _is_secret(field_info)
        required = field_info.is_required()
        if required or secret:
            default: Any | None = None
        else:
            default = _json_default(_resolve_default(field_info))
        default_key = default_fields.get(name)
        reload_override = _reload_override(field_info)
        field_reload = (
            _as_reload_class(reload_override, f"field {name!r}") if reload_override is not None else class_reload
        )
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
                default_namespace_var=f"{TAI_DEFAULT_ENV_PREFIX}{default_key.upper()}"
                if default_key is not None
                else None,
                reload_class=field_reload,
                key_material=_is_key_material(field_info),
                accepted_env_vars=_accepted_env_vars(name, field_info, env_prefix, case_sensitive),
            )
        )
    info = SettingsClassInfo(
        name=cls.__name__,
        module=cls.__module__,
        qualname=_qualname(cls),
        fields=fields,
        reload_class=class_reload,
        env_prefix=env_prefix,
        env_prefix_owned=prefix_owned,
    )
    # Imported here: the env-reader module reads this module's helpers at load.
    from tai42_kit.settings.reader_descriptions import _describe

    types_by_name = {field.name: field.type for field in fields if field.nested_group is None}
    info._env_reader = _describe(cls, lambda name, _field_info: types_by_name[name])
    return info


def _env_reader_of(info: SettingsClassInfo) -> "EnvReader":
    """The env-reader description of a registered group; raises for an info no extraction made."""
    if info._env_reader is None:
        raise RuntimeError(f"settings group {info.qualname} carries no env-reader description")
    return info._env_reader


def _register(cls: type[BaseSettings]) -> None:
    """Register (or replace) a settings class in the registry by qualified name.

    Every :func:`on_settings_registered` listener is then called with the registration, and every
    :func:`~tai42_kit.settings.on_env_reader_described` listener with the group's env-reader
    description, before the respawn-class bind below reads the env, so a listener that edits the
    process env edits what the bind reads. A listener's exception propagates out of the class
    definition.
    While the respawn-class env is bound, each respawn-class field of ``cls`` whose env var is not
    yet bound is bound under the process env as it is now; a var already bound (a re-import's
    re-registration, a second class naming the same var) keeps its first binding.
    """
    info = _extract(cls)
    _REGISTRY[_qualname(cls)] = info
    for listener in _REGISTRATION_LISTENERS:
        listener(info.model_copy(deep=True))
    from tai42_kit.settings.reader_descriptions import _notify

    _notify(_env_reader_of(info))
    if _BOUND_ENV is not None:
        env = dict(os.environ)
        for field in _respawn_fields(info):
            if field.env_var not in _BOUND_ENV:
                _BOUND_ENV[field.env_var] = _BoundField(info.qualname, field, env)


def on_settings_registered(listener: Callable[[SettingsClassInfo], None]) -> Callable[[SettingsClassInfo], None]:
    """Subscribe ``listener`` to every settings group registered from now on; returns ``listener``.

    The listener is called with a copy of each registration (a re-import's re-registration
    included), once the group is in :func:`registered_settings` and before any value of it is
    read. Subscribing the same listener again does nothing.
    """
    if listener not in _REGISTRATION_LISTENERS:
        _REGISTRATION_LISTENERS.append(listener)
    return listener


def _clear_registry() -> None:
    """Drop every registration and the bound respawn-class env — for tests only."""
    global _BOUND_ENV
    _REGISTRY.clear()
    _BOUND_ENV = None


def _registered_infos() -> list[SettingsClassInfo]:
    """The live registrations, uncopied — for kit-internal read-only scans."""
    return list(_REGISTRY.values())


def _registered_readers() -> list["EnvReader"]:
    """The env-reader description of every registered group, in registration order."""
    return [_env_reader_of(info) for info in _REGISTRY.values()]


def registered_settings() -> list[SettingsClassInfo]:
    """Snapshot of every registered settings group, in registration order.

    Returns model copies so callers cannot mutate the registry's internals.
    """
    return [info.model_copy(deep=True) for info in _REGISTRY.values()]


_RESPAWN_CLASSES: frozenset[ReloadClass] = frozenset({"recycle", "excluded"})


def _changes_only_by_respawn(cls: type[BaseSettings], seen: frozenset[str]) -> bool:
    key = _qualname(cls)
    info = _REGISTRY.get(key)
    if info is None or key in seen:
        return False
    for field in info.fields:
        if field.nested_group is None:
            if field.reload_class not in _RESPAWN_CLASSES:
                return False
            continue
        field_info = cls.model_fields.get(field.name)
        nested = None if field_info is None else _settings_member(field_info.annotation)
        if nested is None or not _changes_only_by_respawn(nested, seen | {key}):
            return False
    return True


def changes_only_by_respawn(cls: type[BaseSettings]) -> bool:
    """Whether every value of the registered settings group ``cls`` changes only by a process respawn or restart.

    True when ``cls`` is registered and every field's reload class is ``recycle`` or
    ``excluded``; a field that references a nested group is judged by that group's own
    registered fields, recursively. False for an unregistered class (a plain
    ``BaseSettings``, a ``registry_exclude`` base, a class with no fields), for a nested
    group that is unregistered or references back into the groups being judged, and
    for any ``hot`` field.
    """
    return _changes_only_by_respawn(cls, frozenset())


def _respawn_fields(info: SettingsClassInfo) -> list[SettingsFieldInfo]:
    """``info``'s respawn-class fields with an env var (a nested-group reference carries none)."""
    return [
        field
        for field in info.fields
        if field.nested_group is None and field.env_var and field.reload_class in _RESPAWN_CLASSES
    ]


def bind_respawn_class_env() -> None:
    """Bind every registered respawn-class field under the process env as it is now.

    Called once a lifespan applied its env, before it builds the resources behind those values:
    a group registered later binds its own fields at its registration. Overwrites every binding
    an earlier bind recorded.
    """
    global _BOUND_ENV
    env = dict(os.environ)
    _BOUND_ENV = {
        field.env_var: _BoundField(info.qualname, field, env)
        for info in _REGISTRY.values()
        for field in _respawn_fields(info)
    }


def unbind_respawn_class_env() -> None:
    """Drop the bound respawn-class env (a lifespan's end); nothing is bound until the next bind."""
    global _BOUND_ENV
    _BOUND_ENV = None


def respawn_class_env_drift() -> frozenset[str]:
    """The env vars of the bound respawn-class fields whose value now differs from the value they were bound under.

    Each value is read as the settings layer reads it, under every name that sets the field
    (:func:`~tai42_kit.settings.settings_field_env_value`): the field's own names, its nested
    groups' JSON names and its delimited names, from the process env now and from the env the
    field was bound under. Such a value takes effect only in a respawned process. Empty while
    nothing is bound.
    """
    from tai42_kit.settings.env_names import settings_field_env_value

    if _BOUND_ENV is None:
        return frozenset()
    return frozenset(
        var
        for var, bound in _BOUND_ENV.items()
        if settings_field_env_value(os.environ, bound.group, bound.field)
        != settings_field_env_value(bound.env, bound.group, bound.field)
    )
