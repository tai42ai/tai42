"""The JSON Schema rules a schema holds before any instance is checked against it.

``jsonschema`` checks an instance against a schema only after checking the schema itself against
its dialect's meta-schema. A schema that fails that meta-check (a bound that is not a number, a
negative ``minLength``, a ``pattern`` that does not compile) refuses every instance, so a caller
refuses it up front instead, naming the property it breaks under. :func:`check_schema_rules`
runs that same meta-check with the same validator class the instance check uses, and it refuses
what that meta-check lets through yet no instance check can use: a number that is not finite
(an infinity or a NaN of any number type) anywhere in the schema, which every JSON Schema
dialect admits and every comparison with an instance gets wrong; a schema holding itself, which
no instance check ever finishes walking; and, in every subschema an instance check evaluates, a
fault the meta-schema never sees: a ``$schema`` that is not a string or does not parse as a URI
(one naming a dialect ``jsonschema`` does not know is read as the dialect of the schema holding
it, as the instance check reads it), a subschema breaking the meta-schema of the dialect it names,
a regular expression the dialect's meta-schema never checks (a ``patternProperties`` key, and
those keys joined into the one expression ``additionalProperties`` matches against), and a
``$ref``/``$dynamicRef``/``$recursiveRef`` the instance check cannot follow, or whose pointer
indexes an array with a segment that is no RFC 6901 array index.

A schema it accepts is one resource: only its root may carry an identifier (``$id``, or ``id`` in
draft-04 and draft-03), and no anchor name is declared twice. Every validator built for an
instance check resolves references through :data:`SCHEMA_REFS`, which holds no schema and fetches
none (``jsonschema`` adds the JSON Schema meta-schemas to it), so a reference resolves inside the
schema itself (``#``, a ``#/…`` pointer, an anchor, an absolute reference under the root's ``$id``)
or into a meta-schema. The
instance check registers the schema under its root identifier, so a root identifier naming a
meta-schema would put the schema in that meta-schema's place, and it is refused. Under a root
``$id`` that names none, a reference into a meta-schema keeps the schema's own subschemas out of
that meta-schema's evaluation: no ``$dynamicAnchor`` below the root carries a name the subschema the
reference reaches, or one reachable from it by reference, binds dynamically, or a name the reference
itself names as a ``$dynamicAnchor`` of that meta-schema. So a reference resolves to the same
subschema however the instance check reaches it.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterator, Mapping
from functools import cache
from typing import TYPE_CHECKING, Any, Literal, NamedTuple
from urllib.parse import urldefrag, urljoin

import jsonschema
import referencing.exceptions
from jsonschema import FormatChecker
from jsonschema.protocols import Validator
from jsonschema.validators import validator_for
from jsonschema_specifications import REGISTRY as SPECIFICATIONS
from referencing import Registry, Specification
from referencing.jsonschema import (
    DRAFT3,
    DRAFT4,
    DRAFT6,
    DRAFT7,
    DRAFT201909,
    DRAFT202012,
    DynamicAnchor,
    lookup_recursive_ref,
)

from tai42_kit.utils.data.json_schema_util.dynamic_bindings import DynamicBindings, names_a_dynamic_anchor
from tai42_kit.utils.data.json_schema_util.json_pointer import check_array_indexes
from tai42_kit.utils.data.json_schema_util.plain_json import (
    _entries,
    _is_container,
    _owner_and_location,
    _Place,
    _steps,
    check_plain_json,
)
from tai42_kit.utils.data.json_schema_util.reference_cycles import _Edge, _NodeKey, _refuse_in_place_cycles

if TYPE_CHECKING:
    # ``referencing`` publishes its resolver types from its core module only.
    from referencing._core import Resolved, Resolver

# The registry a validator resolves ``$ref``/``$dynamicRef`` through: it holds no resource and
# retrieves nothing, so a reference resolves only inside the schema itself (``#``, a ``#/…``
# pointer, an anchor, an absolute reference under the root's ``$id``) or into the JSON Schema
# meta-schemas ``jsonschema`` adds to it. A reference naming any other schema does not resolve.
SCHEMA_REFS: Registry[Any] = Registry()


# The registry every reference resolves through, and per subschema of its resources, the anchor
# names it or a subschema reachable from it binds dynamically: a schema whose root carries an
# identifier, and whose reference reaches that subschema, is re-entered at a ``$dynamicAnchor`` of
# such a name.
_REFERENCE_REGISTRY: Registry[Any] = SPECIFICATIONS.combine(SCHEMA_REFS)
_DYNAMIC_BINDINGS = DynamicBindings(_REFERENCE_REGISTRY)

# Every way compiling a ``pattern`` fails on the pattern itself: a syntax fault (``re.error``), a
# repetition count past the engine's limit (``OverflowError``), nesting past the compiler's
# recursion limit (``RecursionError``). The meta-schemas' own ``regex`` format check declares only
# the first, so the others would escape the meta-check instead of naming the property.
_PATTERN_COMPILE_FAULTS = (re.error, OverflowError, RecursionError)


def _rules_message(exc: jsonschema.SchemaError, subject: str) -> str:
    # The refusal for one meta-schema fault: the top-level property it sits under when it has one,
    # else its location in the schema (none for a fault on the schema's root).
    owner, location = _owner_and_location([str(step) for step in exc.absolute_path], subject)
    if owner != subject:
        return f"{owner} breaks the JSON Schema rules: {exc.message}"
    where = f" at {'/'.join(location)}" if location else ""
    return f"{subject} breaks the JSON Schema rules{where}: {exc.message}"


def _under(place: _Place, *keys: str) -> _Place:
    # The place ``keys`` lead to from ``place``.
    for key in keys:
        place = (place, key)
    return place


def _pattern_properties_fault(subschema: dict[str, Any]) -> tuple[str, Exception] | None:
    # The first regular expression an instance check builds from one subschema's
    # ``patternProperties`` that fails to compile: a key (draft-03 and draft-04 meta-schemas never
    # check one; later drafts check it through ``propertyNames``), or, when
    # ``additionalProperties`` is present, every key joined with ``|`` into the one expression
    # each other instance key is matched against (no meta-schema checks that).
    patterns = subschema.get("patternProperties")
    if not isinstance(patterns, dict) or not patterns:
        return None
    for key in patterns:
        try:
            re.compile(key)
        except _PATTERN_COMPILE_FAULTS as exc:
            return f"{key!r} is not a 'regex'", exc
    if "additionalProperties" in subschema:
        try:
            re.compile("|".join(patterns))
        except _PATTERN_COMPILE_FAULTS as exc:
            joined = "the keys joined with '|', which additionalProperties matches every other key against,"
            return f"{joined} do not compile: {exc}", exc
    return None


# How a keyword holds the subschemas an instance check evaluates: ``schema`` one subschema,
# ``schema_or_list`` one or a list of them, ``list`` a list of them, ``map`` an object whose
# values are subschemas, ``if`` the ``if`` subschema with its sibling ``then`` and ``else``.
_Shape = Literal["schema", "schema_or_list", "list", "map", "if"]


class _Applicator(NamedTuple):
    # A keyword holding subschemas an instance check evaluates.
    shape: _Shape
    # The subschema is evaluated against the instance itself, not against a part of it.
    same_instance: bool
    # The subschema is evaluated by the holder's validator evolved onto it (``not``, ``if``,
    # ``contains``), and its own class decides whether a ``$ref`` hides its siblings. Otherwise the
    # validator descends into it, and the holder's class decides that.
    evolved: bool


_ONE = _Applicator("schema", same_instance=False, evolved=False)
_ONE_IN_PLACE = _Applicator("schema", same_instance=True, evolved=False)
_LIST = _Applicator("list", same_instance=False, evolved=False)
_LIST_IN_PLACE = _Applicator("list", same_instance=True, evolved=False)
_MAP = _Applicator("map", same_instance=False, evolved=False)
_MAP_IN_PLACE = _Applicator("map", same_instance=True, evolved=False)
_ONE_OR_LIST = _Applicator("schema_or_list", same_instance=False, evolved=False)
_ONE_OR_LIST_IN_PLACE = _Applicator("schema_or_list", same_instance=True, evolved=False)
_NOT = _Applicator("schema", same_instance=True, evolved=True)
_CONTAINS = _Applicator("schema", same_instance=False, evolved=True)
_IF = _Applicator("if", same_instance=True, evolved=True)

_DRAFT4_APPLICATORS: dict[str, _Applicator] = {
    "additionalItems": _ONE,
    "additionalProperties": _ONE,
    "allOf": _LIST_IN_PLACE,
    "anyOf": _LIST_IN_PLACE,
    "dependencies": _MAP_IN_PLACE,
    "items": _ONE_OR_LIST,
    "not": _NOT,
    "oneOf": _LIST_IN_PLACE,
    "patternProperties": _MAP,
    "properties": _MAP,
}
_DRAFT6_APPLICATORS = {**_DRAFT4_APPLICATORS, "contains": _CONTAINS, "propertyNames": _ONE}
_DRAFT2019_APPLICATORS = {
    **{key: value for key, value in _DRAFT6_APPLICATORS.items() if key != "dependencies"},
    "dependentSchemas": _MAP_IN_PLACE,
    "if": _IF,
    "unevaluatedItems": _ONE,
    "unevaluatedProperties": _ONE,
}

# Per validator class, every keyword it evaluates whose value holds a subschema, as its
# ``VALIDATORS`` name them (references aside, in :data:`_REFERENCES`).
_APPLICATORS: dict[type[Validator], dict[str, _Applicator]] = {
    jsonschema.Draft3Validator: {
        "additionalItems": _ONE,
        "additionalProperties": _ONE,
        "dependencies": _MAP_IN_PLACE,
        "disallow": _ONE_OR_LIST_IN_PLACE,
        "extends": _ONE_OR_LIST_IN_PLACE,
        "items": _ONE_OR_LIST,
        "patternProperties": _MAP,
        "properties": _MAP,
        "type": _ONE_OR_LIST_IN_PLACE,
    },
    jsonschema.Draft4Validator: _DRAFT4_APPLICATORS,
    jsonschema.Draft6Validator: _DRAFT6_APPLICATORS,
    jsonschema.Draft7Validator: {**_DRAFT6_APPLICATORS, "if": _IF},
    jsonschema.Draft201909Validator: _DRAFT2019_APPLICATORS,
    jsonschema.Draft202012Validator: {
        **{key: value for key, value in _DRAFT2019_APPLICATORS.items() if key != "additionalItems"},
        "items": _ONE,
        "prefixItems": _LIST,
    },
}

# Per validator class, the keywords that evaluate the subschema a reference resolves to.
_REFERENCES: dict[type[Validator], frozenset[str]] = {
    jsonschema.Draft3Validator: frozenset({"$ref"}),
    jsonschema.Draft4Validator: frozenset({"$ref"}),
    jsonschema.Draft6Validator: frozenset({"$ref"}),
    jsonschema.Draft7Validator: frozenset({"$ref"}),
    jsonschema.Draft201909Validator: frozenset({"$ref", "$recursiveRef"}),
    jsonschema.Draft202012Validator: frozenset({"$ref", "$dynamicRef"}),
}

# The classes whose subschema holding ``$ref`` is evaluated by that reference alone.
_IGNORES_REF_SIBLINGS: frozenset[type[Validator]] = frozenset(
    {jsonschema.Draft3Validator, jsonschema.Draft4Validator, jsonschema.Draft6Validator, jsonschema.Draft7Validator}
)

# Per validator class, the keywords whose values are named subschemas its meta-schema holds to
# itself.
_DEFINITIONS: dict[type[Validator], tuple[str, ...]] = {
    jsonschema.Draft3Validator: (),
    jsonschema.Draft4Validator: ("definitions",),
    jsonschema.Draft6Validator: ("definitions",),
    jsonschema.Draft7Validator: ("definitions",),
    jsonschema.Draft201909Validator: ("$defs", "definitions"),
    jsonschema.Draft202012Validator: ("$defs", "definitions"),
}

# Per validator class, the ``referencing`` specification its resources are built with (the one
# its meta-schema's dialect names), and per specification name, that class.
_SPECIFICATIONS: dict[type[Validator], Specification[Any]] = {
    jsonschema.Draft3Validator: DRAFT3,
    jsonschema.Draft4Validator: DRAFT4,
    jsonschema.Draft6Validator: DRAFT6,
    jsonschema.Draft7Validator: DRAFT7,
    jsonschema.Draft201909Validator: DRAFT201909,
    jsonschema.Draft202012Validator: DRAFT202012,
}
_VALIDATORS: dict[str, type[Validator]] = {specification.name: cls for cls, specification in _SPECIFICATIONS.items()}


def _applicators(validator: type[Validator]) -> dict[str, _Applicator]:
    # ``validator``'s applicator keywords; a class with none listed is one this check cannot judge.
    try:
        return _APPLICATORS[validator]
    except KeyError:
        raise LookupError(f"no JSON Schema rules check is defined for {validator.__name__}") from None


def _children(
    subschema: dict[str, Any], validator: type[Validator]
) -> Iterator[tuple[tuple[str, ...], dict[str, Any], _Applicator]]:
    # Every object subschema ``validator`` evaluates directly under ``subschema``, with the keys
    # leading to it. A boolean subschema holds nothing to check; a value of any other kind where a
    # subschema belongs is a fault the meta-schema check has already refused.
    for keyword, applicator in _applicators(validator).items():
        if keyword not in subschema:
            continue
        value = subschema[keyword]
        if applicator.shape == "if":
            for key in ("if", "then", "else"):
                if isinstance(subschema.get(key), dict):
                    evolved = key == "if"
                    yield (key,), subschema[key], applicator._replace(evolved=evolved)
        elif applicator.shape == "map" and isinstance(value, dict):
            yield from (((keyword, str(key)), sub, applicator) for key, sub in value.items() if isinstance(sub, dict))
        elif applicator.shape in ("list", "schema_or_list") and isinstance(value, list):
            yield from (
                ((keyword, str(index)), sub, applicator) for index, sub in enumerate(value) if isinstance(sub, dict)
            )
        elif applicator.shape in ("schema", "schema_or_list") and isinstance(value, dict):
            yield (keyword,), value, applicator


def _specification(validator: type[Validator]) -> Specification[Any]:
    # The ``referencing`` specification ``validator`` builds its resources with.
    try:
        return _SPECIFICATIONS[validator]
    except KeyError:
        raise LookupError(f"no JSON Schema rules check is defined for {validator.__name__}") from None


@cache
def _meta_validator(validator: type[Validator]) -> tuple[type[Validator], FormatChecker]:
    # The class ``validator.check_schema`` validates a schema with (the one its meta-schema
    # names) and its default format checker, with the ``regex`` format check declaring every
    # pattern compile fault.
    meta = validator_for(validator.META_SCHEMA, default=validator)
    checker = FormatChecker(formats=())
    checker.checkers = dict(meta.FORMAT_CHECKER.checkers)
    if "regex" in checker.checkers:
        compiles, _ = checker.checkers["regex"]
        checker.checkers["regex"] = (compiles, _PATTERN_COMPILE_FAULTS)
    return meta, checker


def _meta_check(subschema: Any, validator: type[Validator], place: _Place) -> None:
    # ``validator.check_schema``'s own steps (its meta-schema, raising the first fault as a
    # ``SchemaError``) with that format checker, so each pattern compile fault comes back as a
    # meta-schema fault naming where the pattern sits, at ``place`` in the schema.
    meta, checker = _meta_validator(validator)
    for error in meta(validator.META_SCHEMA, format_checker=checker).iter_errors(subschema):
        if place is None:
            raise jsonschema.SchemaError.create_from(error)
        steps = [*_steps(place), *(str(step) for step in error.absolute_path)]
        raise jsonschema.SchemaError(error.message, path=steps, cause=error.cause)


def _dialect(subschema: Any, default: type[Validator], place: _Place) -> type[Validator]:
    # The class an instance check evaluates ``subschema`` with: the one its ``$schema`` names, else
    # ``default`` (the class evaluating the schema holding it), also for a ``$schema`` naming a
    # dialect ``jsonschema`` does not know. A ``$schema`` the instance check
    # cannot read is a fault at ``place``.
    if not isinstance(subschema, dict) or "$schema" not in subschema:
        return default
    dialect = subschema["$schema"]
    path = [*_steps(place), "$schema"]
    if not isinstance(dialect, str):
        raise jsonschema.SchemaError(f"{dialect!r} is not a string", path=path)
    try:
        return validator_for(subschema, default=default)
    except ValueError as exc:
        raise jsonschema.SchemaError(f"{dialect!r} does not parse as a URI: {exc}", path=path, cause=exc) from exc


def _resolve(resolver: Resolver[Any], keyword: str, ref: Any, place: _Place) -> Resolved[Any]:
    # What the reference ``ref`` under ``keyword`` resolves to, as the instance check resolves it;
    # one that cannot be resolved is a fault at ``place``.
    path = [*_steps(place), keyword]
    if not isinstance(ref, str):
        raise jsonschema.SchemaError(f"{ref!r} is not a string", path=path)
    opening = f"{keyword} {ref!r} does not resolve:"
    try:
        if keyword == "$recursiveRef":
            return lookup_recursive_ref(resolver)
        return resolver.lookup(ref)
    except referencing.exceptions.PointerToNowhere as exc:
        reason = f"nothing at {exc.ref!r}"
        raise jsonschema.SchemaError(f"{opening} {reason}", path=path, cause=exc) from exc
    except referencing.exceptions.NoSuchAnchor as exc:
        reason = f"no $anchor or $dynamicAnchor {exc.anchor!r}"
        raise jsonschema.SchemaError(f"{opening} {reason}", path=path, cause=exc) from exc
    except referencing.exceptions.InvalidAnchor as exc:
        reason = f"{exc.anchor!r} is not an anchor name"
        raise jsonschema.SchemaError(f"{opening} {reason}", path=path, cause=exc) from exc
    except referencing.exceptions.Unresolvable as exc:
        reason = "it names a schema outside this one, and no schema is fetched — inline it or move it under $defs"
        raise jsonschema.SchemaError(f"{opening} {reason}", path=path, cause=exc) from exc
    except (LookupError, TypeError, ValueError, AttributeError) as exc:
        # ``referencing`` reading a pointer through a value that is not a container, or an
        # anchor through a subschema it cannot walk, fails the same way at instance-check time.
        reason = f"nothing at {ref.partition('#')[2]!r}" if "#/" in ref else f"{type(exc).__name__}: {exc}"
        raise jsonschema.SchemaError(f"{opening} {reason}", path=path, cause=exc) from exc


def _places(schema: Any) -> dict[int, _Place]:
    # Where each container in ``schema`` sits, by identity (the first place for a value sitting at
    # two), in document order: a reference landing on a container elsewhere lands in a meta-schema.
    places: dict[int, _Place] = {}
    pending: list[tuple[_Place, Any]] = [(None, schema)]
    while pending:
        place, value = pending.pop()
        if not _is_container(value) or id(value) in places:
            continue
        places[id(value)] = place
        pending.extend(((place, str(key)), item) for key, item in reversed(_entries(value)))
    return places


# Per specification (by name), every keyword it reads an identifier or an anchor from, the one it
# reads an identifier from first.
_NAMING_KEYWORDS: dict[str, tuple[str, ...]] = {
    DRAFT3.name: ("id",),
    DRAFT4.name: ("id",),
    DRAFT6.name: ("$id",),
    DRAFT7.name: ("$id",),
    DRAFT201909.name: ("$id", "$anchor"),
    DRAFT202012.name: ("$id", "$anchor", "$dynamicAnchor"),
}


def _unreadable(
    subschema: Mapping[str, Any], specifications: Collection[Specification[Any]], place: _Place
) -> jsonschema.SchemaError | None:
    # The fault of a keyword any of ``specifications`` reads an identifier or an anchor from that
    # holds no string, which reading it needs; ``None`` when each one present holds a string.
    for specification in specifications:
        for keyword in _NAMING_KEYWORDS[specification.name]:
            value = subschema.get(keyword)
            if value is not None and not isinstance(value, str):
                return jsonschema.SchemaError(f"{value!r} is not a string", path=[*_steps(place), keyword])
    return None


def _identified(
    subschema: Mapping[str, Any], specifications: Collection[Specification[Any]], place: _Place
) -> jsonschema.SchemaError | None:
    # The fault of an identifier any of ``specifications`` reads in ``subschema``, a subschema
    # below the root: it would make ``subschema`` a resource of its own, which the references
    # inside it resolve against. ``None`` when none reads one.
    for specification in specifications:
        if specification.id_of(subschema) is not None:
            keyword = _NAMING_KEYWORDS[specification.name][0]
            message = (
                f"{keyword} {subschema[keyword]!r} below the root is not allowed: the schema is one resource, so every "
                f"reference resolves inside it — drop the {keyword} and point references at '#/…' or at an anchor"
            )
            return jsonschema.SchemaError(message, path=[*_steps(place), keyword])
    return None


def _taken(schema: Mapping[str, Any], specification: Specification[Any]) -> jsonschema.SchemaError | None:
    # The fault of a root identifier, as ``specification`` reads it and the empty fragment dropped,
    # naming a resource of the registry references resolve through: the instance check registers
    # the schema under it, in that meta-schema's place. ``None`` when it names none.
    identifier = specification.create_resource(schema).id()
    if identifier is None or identifier not in _REFERENCE_REGISTRY:
        return None
    keyword = _NAMING_KEYWORDS[specification.name][0]
    message = (
        f"{keyword} {schema[keyword]!r} names a JSON Schema meta-schema: a schema cannot take a meta-schema's "
        f"identifier — give it an identifier of its own, or drop the {keyword}"
    )
    return jsonschema.SchemaError(message, path=[keyword])


def _anchor_keyword(anchor: Any, specification: Specification[Any]) -> str:
    # The keyword ``specification`` read ``anchor`` from.
    if isinstance(anchor, DynamicAnchor):
        return "$dynamicAnchor"
    if specification.name in (DRAFT201909.name, DRAFT202012.name):
        return "$anchor"
    return _NAMING_KEYWORDS[specification.name][0]


def _check_one_resource(
    schema: dict[str, Any],
    validator: type[Validator],
    places: dict[int, _Place],
    meta_checked: set[tuple[int, type[Validator]]],
) -> dict[str, _Place]:
    # Every subresource of ``schema`` as the resolver's registry crawls it, in document order: under
    # the keywords its specification lists, each read with the specification its own ``$schema``
    # names, else its holder's. Each holds that specification's meta-schema before it is read
    # (``meta_checked`` holds the pairs already checked, and gains the ones checked here), the
    # root's identifier names no resource of the reference registry (the instance check would
    # register the schema in that meta-schema's place), none below the root carries an identifier
    # (as the root's specification or its own reads it), and no anchor name is declared twice, since
    # the registry would keep one of the two without a word: the second declaration is the fault.
    # Returns the place of each ``$dynamicAnchor`` below the root by its name, in document order,
    # which the walk holds against a reference entering a meta-schema.
    root = _specification(validator)
    order = {key: index for index, key in enumerate(places)}
    names: set[str] = set()
    dynamic_below_root: dict[str, _Place] = {}
    seen: set[int] = set()
    pending: list[tuple[Specification[Any], Mapping[str, Any]]] = [(root, schema)]
    while pending:
        specification, contents = pending.pop()
        if id(contents) in seen:
            continue
        seen.add(id(contents))
        place = places[id(contents)]
        cls = _VALIDATORS[specification.name]
        if (id(contents), cls) not in meta_checked:
            meta_checked.add((id(contents), cls))
            _meta_check(contents, cls, place)
        readers = (root, specification)
        fault = _unreadable(contents, readers, place) or (
            _taken(contents, root) if contents is schema else _identified(contents, readers, place)
        )
        if fault is not None:
            raise fault
        for anchor in specification.anchors_in(contents):
            keyword = _anchor_keyword(anchor, specification)
            if anchor.name in names:
                message = (
                    f"anchor {anchor.name!r} ({keyword}) is declared more than once, "
                    f"so '#{anchor.name}' could resolve to either"
                )
                raise jsonschema.SchemaError(message, path=[*_steps(place), keyword])
            names.add(anchor.name)
            if isinstance(anchor, DynamicAnchor) and contents is not schema:
                dynamic_below_root.setdefault(anchor.name, place)
        held = [each for each in specification.subresources_of(contents) if isinstance(each, Mapping)]
        for each in sorted(held, key=lambda each: order[id(each)], reverse=True):
            dialect = each.get("$schema")
            if dialect is not None and not isinstance(dialect, str):
                raise jsonschema.SchemaError(
                    f"{dialect!r} is not a string", path=[*_steps(places[id(each)]), "$schema"]
                )
            pending.append((specification.detect(each), each))
    return dynamic_below_root


def _meta_checked_by_root(schema: dict[str, Any], validator: type[Validator]) -> set[tuple[int, type[Validator]]]:
    # Every subschema ``validator``'s meta-check of ``schema`` holds to that meta-schema, paired with
    # ``validator``: the root, each one an applicator keyword of ``validator`` holds, and each entry of
    # the definitions containers its meta-schema describes, at any depth.
    containers = _DEFINITIONS[validator]
    checked: set[tuple[int, type[Validator]]] = set()
    pending = [schema]
    while pending:
        subschema = pending.pop()
        if (id(subschema), validator) in checked:
            continue
        checked.add((id(subschema), validator))
        pending.extend(child for _, child, _ in _children(subschema, validator))
        for container in containers:
            held = subschema.get(container)
            if isinstance(held, dict):
                pending.extend(entry for entry in held.values() if isinstance(entry, dict))
    return checked


class _Node(NamedTuple):
    place: _Place
    subschema: dict[str, Any]
    validator: type[Validator]
    ignores_ref_siblings: bool


def _check_evaluated_subschemas(schema: dict[str, Any], validator: type[Validator]) -> list[dict[str, Any]]:
    # ``schema`` is one resource whose root identifier names no meta-schema
    # (:func:`_check_one_resource`); then every subschema an instance
    # check evaluates when ``validator`` judges it (each one an applicator keyword holds, and each
    # one a reference resolves to, through the resolver the instance check uses) is checked once per
    # class: it carries no identifier, its ``$schema`` is a URI string, one naming another dialect
    # than the subschema holding it holds that dialect's meta-schema, a reference resolves to a
    # schema holding its own dialect's meta-schema, each array index its pointer names is an RFC
    # 6901 index, and its ``patternProperties`` compile; and, when the root carries an identifier
    # and a reference enters a meta-schema, no ``$dynamicAnchor`` below the root carries a name
    # the subschema it reaches, or one reachable from it, binds dynamically (:data:`_DYNAMIC_BINDINGS`)
    # or the reference names as a ``$dynamicAnchor`` of it (the dynamic lookup would re-enter the schema there with the
    # meta-schema's base URI).
    # Then no cycle of subschemas evaluated against the same instance closes, since evaluating one
    # never ends. The first fault raises as a meta-schema fault at its place. Returns every subschema
    # it checked, each once, the root first.
    places = _places(schema)
    meta_checked = _meta_checked_by_root(schema, validator)
    dynamic_below_root = _check_one_resource(schema, validator, places, meta_checked)
    walk = _Walk(schema, validator, places, meta_checked, dynamic_below_root)
    ignores = validator in _IGNORES_REF_SIBLINGS
    walk.pending.append(((id(schema), validator, ignores), _Node(None, schema, validator, ignores)))
    evaluated: dict[int, dict[str, Any]] = {}
    while walk.pending:
        key, node = walk.pending.pop()
        if key not in walk.edges:
            walk.visit(key, node)
            evaluated.setdefault(key[0], node.subschema)
    _refuse_in_place_cycles(walk.edges)
    return list(evaluated.values())


class _Walk:
    # The state of one :func:`_check_evaluated_subschemas` walk.

    def __init__(
        self,
        schema: dict[str, Any],
        validator: type[Validator],
        places: dict[int, _Place],
        meta_checked: set[tuple[int, type[Validator]]],
        dynamic_below_root: dict[str, _Place],
    ) -> None:
        self.schema = schema
        self.places = places
        self.root_specification = _specification(validator)
        root = self.root_specification.create_resource(schema)
        # The one resolver every reference resolves through, as the instance check's root
        # resolver resolves it, and what each distinct reference resolved to.
        self.resolver = _REFERENCE_REGISTRY.resolver_with_root(root)
        # The base URI references resolve against, the URIs naming the schema's own resource,
        # whether the root carries an identifier (so a meta-schema's dynamic lookup re-enters the
        # schema's resource), and the place of each ``$dynamicAnchor`` below the root by name.
        self.base_uri = root.id() or ""
        self.own_uris = {self.base_uri, urldefrag(self.base_uri).url}
        self.root_identified = bool(root.id())
        self.dynamic_below_root = dynamic_below_root
        self.resolved: dict[tuple[str, str], Resolved[Any]] = {}
        self.pending: list[tuple[_NodeKey, _Node]] = []
        self.edges: dict[_NodeKey, list[_Edge]] = {}
        # The subschemas already held to a class's meta-schema, with that class.
        self.meta_checked = meta_checked

    def visit(self, key: _NodeKey, node: _Node) -> None:
        # Check ``node`` and queue every subschema it evaluates, recording the ones evaluated
        # against the same instance as its edges.
        out = self.edges[key] = []
        fault = _pattern_properties_fault(node.subschema)
        if fault is not None:
            message, cause = fault
            raise jsonschema.SchemaError(message, path=[*_steps(node.place), "patternProperties"], cause=cause)
        if not (node.ignores_ref_siblings and "$ref" in node.subschema):
            for keys, sub, applicator in _children(node.subschema, node.validator):
                self._applicator(node, out, _under(node.place, *keys), sub, applicator)
        for keyword in sorted(_REFERENCES[node.validator] & node.subschema.keys()):
            self._reference(node, out, keyword)

    def _held_to_its_dialect(self, subschema: dict[str, Any], validator: type[Validator], place: _Place) -> None:
        # Meta-check ``subschema`` against ``validator``'s meta-schema, once per pair.
        if (id(subschema), validator) not in self.meta_checked:
            self.meta_checked.add((id(subschema), validator))
            _meta_check(subschema, validator, place)

    def _in_the_resource(self, subschema: dict[str, Any], classes: tuple[type[Validator], ...], place: _Place) -> None:
        # Refuse an identifier in ``subschema`` read as the root's specification, or as the
        # specification of a class in ``classes`` (the one holding it, the one evaluating it).
        if subschema is self.schema:
            return
        readers = (self.root_specification, *(_specification(cls) for cls in classes))
        fault = _unreadable(subschema, readers, place) or _identified(subschema, readers, place)
        if fault is not None:
            raise fault

    def _applicator(
        self, node: _Node, out: list[_Edge], place: _Place, sub: dict[str, Any], applicator: _Applicator
    ) -> None:
        cls = node.validator
        sub_cls = _dialect(sub, cls, place)
        if sub_cls is not cls:
            self._held_to_its_dialect(sub, sub_cls, place)
        self._in_the_resource(sub, (cls, sub_cls), place)
        rule = sub_cls if applicator.evolved else cls
        key: _NodeKey = (id(sub), sub_cls, rule in _IGNORES_REF_SIBLINGS)
        if applicator.same_instance:
            out.append(_Edge(key, None, None, node.place))
        self.pending.append((key, _Node(place, sub, sub_cls, key[2])))

    def _reference(self, node: _Node, out: list[_Edge], keyword: str) -> None:
        ref = node.subschema[keyword]
        resolved = self.resolved.get((keyword, ref)) if isinstance(ref, str) else None
        if resolved is None:
            entry = self._entry(keyword, ref, node.place) if keyword != "$recursiveRef" else None
            resolved = self.resolved[keyword, ref] = _resolve(self.resolver, keyword, ref, node.place)
            if entry is not None:
                self._refuse_reentry(keyword, ref, *entry)
        target = resolved.contents
        if isinstance(target, bool) or (isinstance(target, dict) and id(target) not in self.places):
            # A boolean holds nothing to check; an object outside the schema is a meta-schema
            # jsonschema carries.
            return
        if not isinstance(target, dict):
            message = f"{keyword} {ref!r} points at a value that is not a schema"
            raise jsonschema.SchemaError(message, path=[*_steps(node.place), keyword])
        place = self.places[id(target)]
        target_cls = _dialect(target, node.validator, place)
        self._held_to_its_dialect(target, target_cls, place)
        self._in_the_resource(target, (node.validator, target_cls), place)
        ignores = node.validator in _IGNORES_REF_SIBLINGS
        key: _NodeKey = (id(target), target_cls, ignores)
        out.append(_Edge(key, keyword, ref, node.place))
        self.pending.append((key, _Node(place, target, target_cls, ignores)))

    def _entry(self, keyword: str, ref: Any, place: _Place) -> tuple[str, str] | None:
        # The resource URI and the fragment of ``ref`` as the lookup reads them, before it resolves:
        # each array index a pointer names into the schema or a meta-schema must be one RFC 6901
        # allows, which the lookup does not hold it to. ``None`` unless ``ref`` enters a resource
        # of the registry other than the schema's own.
        if not isinstance(ref, str):
            return None
        if ref.startswith("#"):
            uri, fragment = self.base_uri, ref[1:]
        else:
            uri, fragment = urldefrag(urljoin(self.base_uri, ref))
        own = uri in self.own_uris
        if fragment.startswith("/") and (own or uri in _REFERENCE_REGISTRY):
            contents = self.schema if own else _REFERENCE_REGISTRY[uri].contents
            try:
                check_array_indexes(contents, fragment, keyword=keyword, ref=ref)
            except ValueError as exc:
                raise jsonschema.SchemaError(str(exc), path=[*_steps(place), keyword], cause=exc) from exc
        return None if own or uri not in _REFERENCE_REGISTRY else (uri, fragment)

    def _refuse_reentry(self, keyword: str, ref: str, uri: str, fragment: str) -> None:
        # ``ref`` has entered the registry resource ``uri`` at ``fragment``. When the root carries an
        # identifier, a dynamic lookup there (one the subschema reached refers to, one reachable from
        # it, or ``ref`` naming a ``$dynamicAnchor`` of ``uri``) binds a ``$dynamicAnchor`` of its
        # name in the schema's resource;
        # one below the root would be evaluated with the meta-schema's base URI, its references
        # resolved against the meta-schema.
        if not self.root_identified:
            return
        entered = fragment if names_a_dynamic_anchor(_REFERENCE_REGISTRY, uri, fragment) else None
        bound = _DYNAMIC_BINDINGS.bound(uri, fragment)
        name = next((name for name in self.dynamic_below_root if name in bound or name == entered), None)
        if name is None:
            return
        reach = (
            f"{keyword} {ref!r} names the meta-schema's $dynamicAnchor {name!r}, which"
            if name == entered
            else f"the meta-schema's $dynamicRef '#{name}'"
        )
        message = (
            f"$dynamicAnchor {name!r} below the root is not allowed when the schema has an $id and refers into a "
            f"JSON Schema meta-schema: {reach} would re-enter the schema here and resolve its references against "
            "the meta-schema — rename the anchor"
        )
        raise jsonschema.SchemaError(message, path=[*_steps(self.dynamic_below_root[name]), "$dynamicAnchor"])


def check_schema_rules(schema: Any, validator: type[Validator], *, subject: str) -> None:
    """Raise ``ValueError`` when ``schema`` breaks ``validator``'s meta-schema; return ``None`` when it holds.

    ``validator`` is the class the instance check judges instances with; its own ``check_schema``
    (its meta-schema and that meta-schema's format checks, a ``pattern`` that fails to compile in
    any way included) is the rule, and a number that is not finite anywhere in ``schema``, or a
    schema holding itself, is refused first (:func:`check_plain_json`). The schema must be one
    resource: its root identifier names no JSON Schema meta-schema (the instance check would put the
    schema in that meta-schema's place), no subschema below the root carries an identifier the
    resolver reads, and no anchor name is declared twice, so each reference resolves to one
    subschema wherever the instance check meets it; and, when the root carries an identifier and a
    reference enters a meta-schema, no ``$dynamicAnchor`` below the root carries a name the
    subschema it reaches, or one reachable from it by reference, binds dynamically, or that the
    reference names as a ``$dynamicAnchor`` of it (the dynamic lookup would re-enter the schema
    there with the meta-schema's base URI). Then every subschema an
    instance check evaluates is checked once per class evaluating it, under every applicator
    keyword of that dialect and at every ``$ref``/``$dynamicRef``/``$recursiveRef`` target: a
    ``$schema`` there must be a URI string (one naming a dialect ``jsonschema`` does not know is
    read as the dialect of the schema holding it), a subschema naming another dialect holds that
    dialect's meta-schema, its ``patternProperties`` compile, a reference resolves inside the
    schema itself or into a meta-schema (none is fetched) to a schema holding the rules, each array
    index a pointer names is ``0`` or digits without a leading zero (RFC 6901), and no cycle of
    references evaluates the same instance value forever. ``subject`` opens the message
    (``"form schema"``, ``"JSON schema"``), which names the top-level property the first fault
    sits under, or the fault's location in the schema.
    """
    evaluated_subschemas(schema, validator, subject=subject)


def evaluated_subschemas(schema: Any, validator: type[Validator], *, subject: str) -> list[dict[str, Any]]:
    """Every object subschema an instance check by ``validator`` evaluates inside ``schema``, each once, the root first.

    ``schema`` is checked first, as :func:`check_schema_rules` checks it, and a fault raises the
    same ``ValueError``. The subschemas are the ones that check walks: each one an applicator
    keyword holds, and each one a reference resolves to in any form (a pointer, an anchor, an
    absolute or a relative reference under the root's ``$id``). A boolean subschema and a
    meta-schema a reference resolves into are not listed; neither is a non-object ``schema``.
    """
    check_plain_json(schema, subject=subject)
    try:
        _meta_check(schema, validator, None)
        return _check_evaluated_subschemas(schema, validator) if isinstance(schema, dict) else []
    except jsonschema.SchemaError as exc:
        raise ValueError(_rules_message(exc, subject)) from exc
    except RecursionError as exc:
        raise ValueError(f"{subject} nests too deeply to check") from exc


def schema_validator_class(schema: Any, default: type[Validator], *, subject: str) -> type[Validator]:
    """The validator class an instance check judges ``schema`` with.

    The one the schema's own ``$schema`` names, ``default`` when it names none or names a dialect
    ``jsonschema`` does not know. Raises
    ``ValueError`` opened by ``subject`` when the ``$schema`` is not a URI string, which names no
    dialect at all.
    """
    try:
        return _dialect(schema, default, None)
    except jsonschema.SchemaError as exc:
        raise ValueError(_rules_message(exc, subject)) from exc
