"""A schema keeps clear of the registry its references resolve through.

Its root takes no identifier of a resource in that registry, since the instance check would put the
schema in that meta-schema's place. A reference into a meta-schema is held to the names the
subschema it reaches binds dynamically, not to every name its whole resource binds.
"""

from __future__ import annotations

import copy
import re
from typing import Any

import pytest
from jsonschema import FormatChecker
from jsonschema.validators import validator_for
from jsonschema_specifications import REGISTRY as SPECIFICATIONS
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from tai42_kit.utils.data.json_schema_util import SCHEMA_REFS, schema_rules
from tai42_kit.utils.data.json_schema_util.dynamic_bindings import DynamicBindings

from ._dialect_places import _D4, _D7, _D2020, _check

_M20 = "https://json-schema.org/draft/2020-12/schema"
_META = "https://json-schema.org/draft/2020-12/meta/"
_APP = _META + "applicator"
_CORE = _META + "core"
_D4_URI = _D4.META_SCHEMA["$schema"]
_D7_URI = _D7.META_SCHEMA["$schema"]
_X_TO_Y = {"x": {"$dynamicAnchor": "meta", "$ref": "#/$defs/y"}, "y": {"type": "object"}}


def _taken(keyword: str, value: str) -> str:
    # The refusal of a root identifier naming a meta-schema, after the path.
    return (
        f"at {keyword}: {keyword} {value!r} names a JSON Schema meta-schema: a schema cannot take a meta-schema's "
        f"identifier — give it an identifier of its own, or drop the {keyword}"
    )


@pytest.mark.parametrize("uri", sorted(SPECIFICATIONS))
@pytest.mark.parametrize("suffix", ["", "#"], ids=["bare", "empty-fragment"])
def test_a_root_identifier_naming_a_meta_schema_is_refused(uri, suffix):
    with pytest.raises(ValueError, match=re.escape(_taken("$id", uri + suffix))):
        _check({"$id": uri + suffix, "type": "object"})


def test_a_draft_04_root_id_naming_a_meta_schema_is_refused():
    with pytest.raises(ValueError, match=re.escape(_taken("id", _D4_URI))):
        _check({"$schema": _D4_URI, "id": _D4_URI, "type": "object"})


_TAKES_A_META_SCHEMA_ID: dict[str, dict[str, Any]] = {
    "schema-own-pointer": {"$id": _M20, "properties": {"p": {"$ref": "#/$defs/s"}}, "$defs": {"s": {"type": "string"}}},
    "applicator-own-pointer": {
        "$id": _APP,
        "properties": {"p": {"$ref": "#/$defs/s"}},
        "$defs": {"s": {"type": "string"}},
    },
    "applicator-refers-to-schema": {"$id": _APP, "$ref": _M20, "$defs": {"schemaArray": {"type": "string"}}},
    "schema-anchor-below-refers-to-applicator": {"$id": _M20, "$defs": _X_TO_Y, "$ref": _APP},
    "core-refers-to-schema": {"$id": _CORE, "$ref": _M20, "properties": {"q": {"type": "string"}}},
    "property-refers-to-schema": {"$id": _APP, "type": "object", "properties": {"m": {"$ref": _M20}}},
}


@pytest.mark.parametrize("schema", list(_TAKES_A_META_SCHEMA_ID.values()), ids=list(_TAKES_A_META_SCHEMA_ID))
def test_a_schema_under_a_meta_schema_identifier_is_refused_before_its_references(schema):
    with pytest.raises(ValueError, match=re.escape(_taken("$id", schema["$id"]))):
        _check(schema)


@pytest.mark.parametrize("uri", sorted(SPECIFICATIONS))
def test_a_meta_schema_pasted_under_its_own_identifier_is_refused(uri):
    schema = copy.deepcopy(SPECIFICATIONS.contents(uri))
    assert isinstance(schema, dict)
    keyword = "$id" if "$id" in schema else "id"
    with pytest.raises(ValueError, match=re.escape(_taken(keyword, schema[keyword]))):
        _check(schema)


def test_the_instance_check_puts_a_schema_under_a_meta_schema_identifier_in_that_meta_schema_s_place():
    # What the refusal prevents: the 2020-12 meta-schema then checks ``properties`` with the schema itself.
    schema = {"$id": _APP, "type": "object", "properties": {"m": {"$ref": _M20}}}
    assert _D2020(schema, registry=SCHEMA_REFS).is_valid({"m": {"properties": 5}})
    del schema["$id"]
    assert not _D2020(schema, registry=SCHEMA_REFS).is_valid({"m": {"properties": 5}})
    with pytest.raises(RecursionError):
        list(_D2020({"$id": _APP, "$ref": _M20}, registry=SCHEMA_REFS).iter_errors({"type": "object"}))


@pytest.mark.parametrize(
    "schema",
    [
        # Beside ``$ref`` a draft-07 root's ``$id`` names no resource, so the meta-schema keeps its place.
        {"$schema": _D7_URI, "$id": _D7_URI, "$ref": "#/definitions/s", "definitions": {"s": {"type": "string"}}},
        {"$id": _APP + ".json", "type": "object", "properties": {"m": {"$ref": _M20}}},
        {"$id": "https://json-schema.org/draft/2020-12/", "type": "object"},
    ],
    ids=["draft-07-id-beside-ref", "near-applicator", "draft-directory"],
)
def test_a_root_identifier_naming_no_meta_schema_is_accepted(schema):
    _check(schema)


def _reentry(keyword: str) -> str:
    return (
        f"at $defs/x/{keyword}: $dynamicAnchor 'meta' below the root is not allowed when the schema has an $id and "
        "refers into a JSON Schema meta-schema: the meta-schema's $dynamicRef '#meta' would re-enter the schema here "
        "and resolve its references against the meta-schema — rename the anchor"
    )


_BINDING_TARGETS: dict[str, str] = {
    "schema-array": _APP + "#/$defs/schemaArray",
    "percent-encoded": _APP + "#/%24defs/schemaArray",
    "static-reference-to-a-binding-resource": _M20 + "#/allOf/0",
    "pointer-to-a-binding-reference": _APP + "#/properties/allOf",
}


@pytest.mark.parametrize("ref", list(_BINDING_TARGETS.values()), ids=list(_BINDING_TARGETS))
def test_a_pointer_to_a_meta_schema_subschema_binding_the_name_is_refused(ref):
    schema = {"$id": "https://e.com/r", "properties": {"p": {"$ref": ref}}, "$defs": _X_TO_Y}
    with pytest.raises(ValueError, match=re.escape(_reentry("$dynamicAnchor"))):
        _check(schema)


_UNBOUND_TARGETS: dict[str, tuple[str, list[Any], list[Any]]] = {
    "anchor-string": (_CORE + "#/$defs/anchorString", [{"p": "ab"}], [{"p": "1 2"}, {"p": 5}]),
    "uri-string": (_CORE + "#/$defs/uriString", [{"p": "https://e.com"}], [{"p": 5}]),
    "vocabulary-object": (_M20 + "#/$vocabulary", [{"p": 5}, {"p": "x"}], []),
}


@pytest.mark.parametrize(("ref", "fits", "misfits"), list(_UNBOUND_TARGETS.values()), ids=list(_UNBOUND_TARGETS))
def test_a_pointer_to_a_meta_schema_subschema_binding_no_name_is_accepted(ref, fits, misfits):
    schema = {"$id": "https://e.com/r", "properties": {"p": {"$ref": ref}}, "$defs": _X_TO_Y}
    _check(schema)
    validator = validator_for(schema)(schema, format_checker=FormatChecker(), registry=SCHEMA_REFS)
    assert [validator.is_valid(each) for each in fits] == [True] * len(fits)
    assert [validator.is_valid(each) for each in misfits] == [False] * len(misfits)


def test_the_names_a_meta_schema_target_binds_are_read_from_the_subschema_it_reaches():
    bindings = schema_rules._DYNAMIC_BINDINGS
    meta_schema = SPECIFICATIONS.contents(_M20)
    assert isinstance(meta_schema, dict)
    name = meta_schema["$dynamicAnchor"]
    assert name in bindings.bound(_M20, "")
    assert name in bindings.bound(_APP, "")
    assert name in bindings.bound(_APP, "/$defs/schemaArray")
    assert name in bindings.bound(_M20, "/allOf/1")
    assert not bindings.bound(_META + "validation", "")
    assert not bindings.bound(_D7_URI.rstrip("#"), "")
    assert not bindings.bound(_CORE, "/$defs/anchorString")
    assert not bindings.bound(_M20, "/$vocabulary")


def test_a_registry_resource_referring_outside_the_registry_is_refused_when_its_bindings_are_read():
    registry = Registry().with_resource(
        "https://e.com/a", DRAFT202012.create_resource({"$schema": _M20, "$ref": "https://e.com/b#/x"})
    )
    with pytest.raises(LookupError, match=re.escape("$ref 'https://e.com/b#/x' in https://e.com/a leads to")):
        DynamicBindings(registry)


def test_a_pointer_passing_a_value_that_holds_no_members_is_refused():
    with pytest.raises(TypeError, match=re.escape("#/$defs/anchorString/type/x in " + _CORE + " passes 'string'")):
        schema_rules._DYNAMIC_BINDINGS.bound(_CORE, "/$defs/anchorString/type/x")
