"""Which settings fields an env name sets, as the settings layer reads env names.

An :class:`EnvNameIndex` is built from env-reader descriptions
(:class:`~tai42_kit.settings.EnvReader`). A field is set by more names than its accepted env
names:

* a nested group's JSON name — the accepted name of the field that references it, e.g.
  ``HOOKS_REDIS`` — sets every field of that group, and of the groups it nests in turn;
* in a reader that declares an ``env_nested_delimiter``, a delimited name — an accepted name of
  one of its fields, the delimiter, then the path below it, e.g.
  ``TAI_RATE_LIMIT_REDIS__REDIS_URL`` — sets the field the path names: a field of a nested
  group, or a field whose value is an object or an array, under any path below it.

Every name is compared folded (:func:`~tai42_kit.settings.fold_env_name`), as the settings
layer reads env names. The fields are the scalar fields: a nested-group reference is not one;
the fields of the group it references are. A ``TAI_DEFAULT_*`` fallback is not a name of the
field it falls back for. Two descriptions of one reader (two versions of a group) count as one
reader that sets, under a name, every field either sets.

:func:`settings_fields_set_by` and :func:`settings_field_env_value` answer over the settings
groups registered at the call, through the index of their descriptions; it is rebuilt whenever
the registrations change.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, NamedTuple

from tai42_kit.settings.base import fold_env_name, present_env_value
from tai42_kit.settings.reader_descriptions import EnvReader
from tai42_kit.settings.registry import SettingsFieldInfo, _registered_infos, _registered_readers

# The JSON-schema types whose values pydantic-settings reads as complex (decoded from JSON,
# and filled from delimited names below the field).
_COMPLEX_TYPES = frozenset({"object", "array"})


class SettingsFieldRef(NamedTuple):
    """One scalar settings field: its reader's qualified name and the field's name."""

    group: str
    field: str


@dataclass(frozen=True)
class _Field:
    """One field of a reader, over every description of the reader the index holds."""

    name: str
    env_names: tuple[str, ...]
    aliases: tuple[str, ...]
    # The nested readers the field holds, among the readers the index holds.
    children: tuple[str, ...]
    # Whether a description reads the field as a value of its own (not a nested group).
    scalar: bool


@dataclass(frozen=True)
class _Link:
    """A nested-group reference: field ``field`` of ``parent`` holds the group ``child``."""

    parent: str
    field: str
    child: str


@dataclass(frozen=True)
class _Delimited:
    """The delimited names below one accepted name of a field of a reader with a nested delimiter."""

    # ``prefix`` is the accepted name and the delimiter, folded; ``delimiter`` is as the reader declares it.
    prefix: str
    delimiter: str
    maxsplit: int
    # Whether the reader reads env names case-sensitively: its delimited names' path keys keep their case.
    case_sensitive: bool
    group: str
    field: str
    # The group a nested-group reference holds; ``None`` for a complex scalar field.
    child: str | None


def _unique(names: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(names))


def _merged_groups(readers: list[EnvReader]) -> dict[str, dict[str, _Field]]:
    """Each reader's fields, the descriptions of one qualified name merged field by field."""
    held = {reader.qualname for reader in readers}
    parts: dict[str, dict[str, list[Any]]] = {}
    for reader in readers:
        group = parts.setdefault(reader.qualname, {})
        for field in reader.fields:
            env_names, aliases, children, scalar = group.setdefault(field.name, [[], [], [], [False]])
            env_names.extend(field.env_names)
            aliases.extend(field.aliases)
            if field.nested is None:
                scalar[0] = True
            elif field.nested in held:
                children.append(field.nested)
    return {
        qualname: {
            name: _Field(name, _unique(env_names), _unique(aliases), _unique(children), scalar[0])
            for name, (env_names, aliases, children, scalar) in fields.items()
        }
        for qualname, fields in parts.items()
    }


def _links_of(groups: Mapping[str, Mapping[str, _Field]]) -> dict[str, tuple[_Link, ...]]:
    """Every parent field that holds each nested group, by the nested group's qualified name."""
    links: dict[str, list[_Link]] = {}
    for qualname, fields in groups.items():
        for field in fields.values():
            for child in field.children:
                links.setdefault(child, []).append(_Link(parent=qualname, field=field.name, child=child))
    return {child: tuple(found) for child, found in links.items()}


def _delimited_rules(readers: list[EnvReader], groups: Mapping[str, Mapping[str, _Field]]) -> tuple[_Delimited, ...]:
    """The delimited-name rules of every reader that reads nested names, each rule once.

    A field takes delimited names when it holds a nested group among ``groups`` or is a complex
    (object or array) field of its own.
    """
    delimited: dict[_Delimited, None] = {}
    for reader in readers:
        if not reader.nested_delimiter:
            continue
        for field in reader.fields:
            child = field.nested if field.nested in groups else None
            if child is None and (field.nested is not None or not field.complex):
                continue
            for name in field.env_names:
                rule = _Delimited(
                    prefix=fold_env_name(name + reader.nested_delimiter),
                    delimiter=reader.nested_delimiter,
                    maxsplit=(reader.nested_max_split or 0) - 1,
                    case_sensitive=reader.case_sensitive,
                    group=reader.qualname,
                    field=field.name,
                    child=child,
                )
                delimited[rule] = None
    return tuple(delimited)


class EnvNameIndex:
    """The env names a set of env readers reads, resolved to the scalar fields they set.

    Built with :meth:`of`; immutable.
    """

    def __init__(
        self,
        groups: Mapping[str, Mapping[str, _Field]],
        exact: Mapping[str, frozenset[SettingsFieldRef]],
        delimited: tuple[_Delimited, ...],
        links: Mapping[str, tuple[_Link, ...]],
    ) -> None:
        """Hold the merged groups, the exact names, the delimited rules and the parent links; see :meth:`of`."""
        self._groups = groups
        self._exact = exact
        self._delimited = delimited
        self._links = links

    @classmethod
    def of(cls, readers: Iterable[EnvReader]) -> EnvNameIndex:
        """The index of ``readers``; a nested reader that is not among them sets nothing."""
        readers = list(readers)
        groups = _merged_groups(readers)
        links = _links_of(groups)
        structure = cls(groups, {}, (), links)
        return cls(groups, structure._exact_names(), _delimited_rules(readers, groups), links)

    def _exact_names(self) -> dict[str, frozenset[SettingsFieldRef]]:
        """Every folded env name of every field, mapped to the scalar fields it sets."""
        exact: dict[str, set[SettingsFieldRef]] = {}
        for qualname, fields in self._groups.items():
            for field in fields.values():
                reached = self._field_leaves(qualname, field, frozenset())
                for name in field.env_names:
                    exact.setdefault(fold_env_name(name), set()).update(reached)
        return {name: frozenset(refs) for name, refs in exact.items()}

    def _field_leaves(self, group: str, field: _Field, seen: frozenset[str]) -> frozenset[SettingsFieldRef]:
        """The scalar fields ``field`` of ``group`` sets: itself, or every field of the groups it holds."""
        found: set[SettingsFieldRef] = {SettingsFieldRef(group, field.name)} if field.scalar else set()
        for child in field.children:
            found |= self._leaves(child, seen | {group})
        return frozenset(found)

    def _leaves(self, group: str, seen: frozenset[str]) -> frozenset[SettingsFieldRef]:
        """Every scalar field of ``group`` and of the groups it nests, a group already on the way skipped."""
        if group in seen:
            return frozenset()
        found: set[SettingsFieldRef] = set()
        for field in self._groups[group].values():
            found |= self._field_leaves(group, field, seen)
        return frozenset(found)

    def _sub_field(self, group: str, segment: str) -> _Field | None:
        """The field of ``group`` a path segment names: by its field name or a name its alias declares, folded."""
        for field in self._groups[group].values():
            if segment in {fold_env_name(field.name), *(fold_env_name(alias) for alias in field.aliases)}:
                return field
        return None

    def _below(self, group: str, segments: list[str], seen: frozenset[str]) -> frozenset[SettingsFieldRef]:
        """The scalar fields a path of segments below a nested group sets."""
        field = self._sub_field(group, segments[0])
        if field is None or group in seen:
            return frozenset()
        found: set[SettingsFieldRef] = {SettingsFieldRef(group, field.name)} if field.scalar else set()
        for child in field.children:
            if len(segments) == 1:
                found |= self._leaves(child, frozenset())
            else:
                found |= self._below(child, segments[1:], seen | {group})
        return frozenset(found)

    def fields_set_by(self, name: str) -> frozenset[SettingsFieldRef]:
        """Every scalar field of the index's readers the env name ``name`` can set; empty when it sets none."""
        folded = fold_env_name(name)
        found = set(self._exact.get(folded, frozenset()))
        for rule in self._delimited:
            if not folded.startswith(rule.prefix) or len(folded) == len(rule.prefix):
                continue
            if rule.child is None:
                found.add(SettingsFieldRef(rule.group, rule.field))
                continue
            segments = folded[len(rule.prefix) :].split(fold_env_name(rule.delimiter), rule.maxsplit)
            found |= self._below(rule.child, segments, frozenset())
        return frozenset(found)


# The registered groups' index and the registrations it was built from, by identity.
_cache: tuple[tuple[int, ...], EnvNameIndex] | None = None


def _index() -> EnvNameIndex:
    global _cache
    key = tuple(id(info) for info in _registered_infos())
    if _cache is None or _cache[0] != key:
        _cache = (key, EnvNameIndex.of(_registered_readers()))
    return _cache[1]


def settings_fields_set_by(name: str) -> frozenset[SettingsFieldRef]:
    """Every registered scalar settings field the env name ``name`` can set; empty when it sets none."""
    return _index().fields_set_by(name)


_MISSING = object()


def _decoded(name: str, raw: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"env var {name} is not valid JSON for the settings field it sets") from exc


def _merged(low: Any, high: Any) -> Any:
    """``high`` over ``low`` as the settings layer combines its sources: objects merge key by key, deeply."""
    if isinstance(low, dict) and isinstance(high, dict):
        out = dict(low)
        for key, value in high.items():
            out[key] = _merged(out[key], value) if key in out else value
        return out
    return high


def _nest(keys: list[str], value: Any) -> Any:
    for key in reversed(keys):
        value = {key: value}
    return value


def _names_path(index: EnvNameIndex, segments: list[str], path: list[SettingsFieldRef]) -> bool:
    """Whether the first ``len(path)`` folded segments name the fields of ``path``, one group at a time."""
    if len(segments) < len(path):
        return False
    for segment, ref in zip(segments, path, strict=False):
        field = index._sub_field(ref.group, segment)
        if field is None or field.name != ref.field:
            return False
    return True


def _delimited_value(
    index: EnvNameIndex, env: Mapping[str, str], rule: _Delimited, path: list[SettingsFieldRef], complex_leaf: bool
) -> Any:
    """What the delimited names below ``rule`` give the field at the end of ``path``, or ``_MISSING``.

    ``path`` runs from the group ``rule`` holds down to the field; it is empty when ``rule`` is
    the field's own. A name that ends at the field gives its value; a name below a complex field
    gives a member of it; a later spelling of one name wins.
    """
    found: Any = _MISSING
    for key, value in env.items():
        folded = fold_env_name(key)
        if value == "" or not folded.startswith(rule.prefix) or len(folded) == len(rule.prefix):
            continue
        rest = key[len(rule.prefix) :]
        if not rule.case_sensitive:
            rest = rest.lower()
        segments = rest.split(rule.delimiter if rule.case_sensitive else rule.delimiter.lower(), rule.maxsplit)
        if not _names_path(index, [fold_env_name(segment) for segment in segments], path):
            continue
        below = segments[len(path) :]
        if not below:
            found = value
        elif complex_leaf:
            found = _merged({} if found is _MISSING or not isinstance(found, dict) else found, _nest(below, value))
    return found


def _parent_layers(
    index: EnvNameIndex, env: Mapping[str, str], path: list[SettingsFieldRef], complex_leaf: bool, seen: frozenset[str]
) -> list[Any]:
    """The values the groups that nest ``path[0].group`` give the field ``path`` ends at, lowest precedence first.

    A parent passes the nested group its JSON name's value with its delimited names merged over
    it, and that beats the nested group's own env names; the parent's own parents beat it in
    turn. When the group is nested by more than one group, the first one that gives a value
    is read.
    """
    group = path[0].group
    if group in seen:
        return []
    for link in index._links.get(group, ()):
        holder = index._groups[link.parent][link.field]
        layer: Any = _MISSING
        for name in holder.env_names:
            raw = present_env_value(env, name)
            if raw is not None:
                layer = _json_member(index, _decoded(name, raw), path)
                break
        for rule in index._delimited:
            if rule.group == link.parent and rule.field == link.field:
                given = _delimited_value(index, env, rule, path, complex_leaf)
                if given is not _MISSING:
                    layer = given if layer is _MISSING else _merged(layer, given)
        outer = _parent_layers(
            index, env, [SettingsFieldRef(link.parent, link.field), *path], complex_leaf, seen | {group}
        )
        layers = ([] if layer is _MISSING else [layer]) + outer
        if layers:
            return layers
    return []


def _json_member(index: EnvNameIndex, value: Any, path: list[SettingsFieldRef]) -> Any:
    """The member of a decoded JSON value ``path`` names, or ``_MISSING``: each step by field name or alias."""
    for ref in path:
        if not isinstance(value, dict):
            return _MISSING
        for key in (ref.field, *index._groups[ref.group][ref.field].aliases):
            if key in value:
                value = value[key]
                break
        else:
            return _MISSING
    return value


def _own_layers(
    index: EnvNameIndex, env: Mapping[str, str], own: SettingsFieldInfo, ref: SettingsFieldRef
) -> list[Any]:
    """The values the field's own names give it, lowest precedence first.

    The first accepted name ``env`` sets, as its env string; for an object or array field in a
    group with a nested delimiter, its delimited names merged over it, as the settings layer
    merges them only over an object or over no value.
    """
    layers: list[Any] = []
    for name in own.accepted_env_vars:
        raw = present_env_value(env, name)
        if raw is not None:
            layers.append(raw)
            break
    rules = [rule for rule in index._delimited if rule.group == ref.group and rule.field == ref.field]
    if not rules or (layers and not isinstance(_decoded(own.accepted_env_vars[0], layers[0]), dict)):
        return layers
    for rule in rules:
        given = _delimited_value(index, env, rule, [], complex_leaf=True)
        if given is not _MISSING:
            layers.append(given)
    return layers


def settings_field_env_value(env: Mapping[str, str], group: str, field: SettingsFieldInfo) -> str | None:
    """The value ``env`` gives the scalar field ``field`` of the settings group ``group``; ``None`` when none.

    ``group`` is the group's qualified name. Read as the settings layer reads it. In precedence
    order, highest first: the registered groups that nest ``group`` (outermost first), each
    through its JSON name with its delimited names merged over it; then the field's own names
    (:func:`_own_layers`). Objects from several of these merge key by key, as the settings layer
    combines them. Empty values are absent. The value is in env form: a string read from one
    name as it is, anything else JSON-encoded. A name whose value must be JSON and is not raises,
    and so does a nested-group reference, which holds no value of its own.
    """
    if field.nested_group is not None:
        raise ValueError(f"settings field {group}.{field.name} references a nested group; it holds no value of its own")
    index = _index()
    ref = SettingsFieldRef(group, field.name)
    complex_leaf = field.type in _COMPLEX_TYPES
    layers = _own_layers(index, env, field, ref) + _parent_layers(index, env, [ref], complex_leaf, frozenset())
    if not layers:
        return None
    value = layers[0]
    for layer in layers[1:]:
        if complex_leaf:
            low = _decoded(field.name, value) if isinstance(value, str) else value
            value = _merged(low, _decoded(field.name, layer) if isinstance(layer, str) else layer)
        else:
            value = layer
    return value if isinstance(value, str) else json.dumps(value)
