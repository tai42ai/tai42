"""The pure schema validators and the page-size clamp — no store, no service instance."""

from __future__ import annotations

import re

import pytest
from jsonschema import Draft202012Validator
from tai42_contract.states import RecordValidation
from tai42_contract.states.errors import SchemaValidationError, TemplateValidationError, ValueValidationError

from tai42_skeleton import states as states_pkg
from tai42_skeleton.states.schema import RecordValidator, _document_reasons, _validate_schema, schema_nodes
from tai42_skeleton.states.service.rows import _page_limit
from tai42_skeleton.states.templates import compose_effective_schema, validate_template

_NOT_FETCHED = "it names a schema outside this one, and no schema is fetched"

_RECORD_SCHEMA = {
    "type": "object",
    "properties": {"n": {"type": "integer"}, "tag": {"type": "string"}},
    "required": ["n", "tag"],
    "additionalProperties": False,
}


def test_validate_schema_shape_refusals() -> None:
    with pytest.raises(SchemaValidationError, match="must be a JSON object"):
        _validate_schema("nope")
    with pytest.raises(SchemaValidationError, match='"type": "object"'):
        _validate_schema({"type": "string"})
    with pytest.raises(SchemaValidationError, match="at least one property"):
        _validate_schema({"type": "object", "properties": {}})
    with pytest.raises(SchemaValidationError, match="property 'n' breaks the JSON Schema rules: 123 is not valid"):
        _validate_schema({"type": "object", "properties": {"n": {"type": 123}}})


def test_validate_schema_accepts_resolvable_local_refs() -> None:
    _validate_schema(
        {
            "type": "object",
            "properties": {"a": {"$ref": "#/$defs/x"}, "b": {"$ref": "#"}, "c": {"$ref": "#/allOf/0"}},
            "allOf": [{"title": "t"}],
            "$defs": {"x": {"type": "integer"}},
        }
    )
    # a #anchor that resolves (nested inside a list, exercising the list-walk)
    _validate_schema(
        {
            "type": "object",
            "properties": {"a": {"$ref": "#named"}},
            "allOf": [{"$anchor": "named", "type": "object"}],
        }
    )


def test_validate_schema_percent_decodes_refs() -> None:
    # RFC 6901 §6: a URI-fragment pointer is percent-decoded WHOLE, then split on "/", then
    # ~1/~0-unescaped — matching jsonschema's resolver, so this syntax pre-check accepts
    # exactly what _validate_document later resolves. "%20" decodes to a space in the key.
    _validate_schema(
        {
            "type": "object",
            "properties": {"a": {"$ref": "#/$defs/a%20b"}},
            "$defs": {"a b": {"type": "integer"}},
        }
    )
    # A key that literally contains "/" is named with ~1 (RFC 6901), never %2F: %2F decodes
    # to a separator before the split, so only #/$defs/a~1b reaches the key "a/b".
    _validate_schema(
        {
            "type": "object",
            "properties": {"a": {"$ref": "#/$defs/a~1b"}},
            "$defs": {"a/b": {"type": "integer"}},
        }
    )
    # %2F decodes to a separator before the split, so it can never name a key holding "/".
    with pytest.raises(SchemaValidationError, match="does not resolve"):
        _validate_schema(
            {
                "type": "object",
                "properties": {"a": {"$ref": "#/$defs/a%2Fb"}},
                "$defs": {"a/b": {"type": "integer"}},
            }
        )
    # A percent-encoded ref that names no key is still refused loudly.
    with pytest.raises(SchemaValidationError, match="does not resolve"):
        _validate_schema(
            {
                "type": "object",
                "properties": {"a": {"$ref": "#/$defs/x%20y"}},
                "$defs": {"a b": {"type": "integer"}},
            }
        )


def test_validate_schema_ref_refusals() -> None:
    with pytest.raises(SchemaValidationError, match=_NOT_FETCHED):
        _validate_schema({"type": "object", "properties": {"a": {"$ref": "http://x/y"}}})
    with pytest.raises(SchemaValidationError, match="does not resolve"):
        _validate_schema({"type": "object", "properties": {"a": {"$ref": "#/$defs/missing"}}})
    with pytest.raises(SchemaValidationError, match="no \\$anchor"):
        _validate_schema({"type": "object", "properties": {"a": {"$ref": "#absent"}}})
    with pytest.raises(SchemaValidationError, match="dynamicRef"):
        _validate_schema({"type": "object", "properties": {"x": {"type": "string"}}, "allOf": [{"$dynamicRef": "#m"}]})


def test_validate_schema_refuses_an_id_below_the_root() -> None:
    with pytest.raises(SchemaValidationError, match="below the root is not allowed: the schema is one resource"):
        _validate_schema({"type": "object", "properties": {"a": {"$id": "urn:example:a", "type": "string"}}})


def test_validate_schema_accepts_an_id_at_the_root() -> None:
    _validate_schema({"$id": "urn:example:root", "type": "object", "properties": {"a": {"type": "string"}}})


def test_validate_schema_accepts_a_field_named_like_a_keyword() -> None:
    # A property NAMED ``$id`` or ``$anchor`` is a field, not the keyword: its value is a subschema.
    _validate_schema({"type": "object", "properties": {"$id": {"type": "string"}, "$anchor": {"type": "string"}}})


def test_validate_schema_refuses_a_duplicate_anchor() -> None:
    with pytest.raises(
        SchemaValidationError, match=re.escape("is declared more than once, so '#t' could resolve to either")
    ):
        _validate_schema(
            {
                "type": "object",
                "properties": {"a": {"$ref": "#t"}},
                "$defs": {"x": {"$anchor": "t", "type": "string"}, "y": {"$anchor": "t", "type": "integer"}},
            }
        )


@pytest.mark.parametrize("keyword", ["default", "examples", "const"])
def test_validate_schema_accepts_an_id_inside_data_keywords(keyword: str) -> None:
    # ``default`` / ``examples`` / ``const`` hold instance DATA, never a subschema: a ``$id`` there is a value.
    value = [{"$id": "a1"}] if keyword == "examples" else {"$id": "a1"}
    _validate_schema({"type": "object", "properties": {"x": {"type": "object", keyword: value}}})


def test_validate_schema_accepts_repeated_anchor_values_inside_enum() -> None:
    _validate_schema({"type": "object", "properties": {"x": {"enum": [{"$anchor": "k"}, {"$anchor": "k"}]}}})


def test_validate_schema_accepts_a_ref_shaped_value_inside_const() -> None:
    _validate_schema({"type": "object", "properties": {"x": {"const": {"$ref": "#/nope"}}}})


def test_validate_schema_refuses_a_ref_to_an_anchor_that_only_appears_as_data() -> None:
    schema = {"type": "object", "properties": {"x": {"$ref": "#k"}, "y": {"const": {"$anchor": "k"}}}}
    with pytest.raises(SchemaValidationError, match="does not resolve"):
        _validate_schema(schema)
    with pytest.raises(SchemaValidationError, match="does not resolve"):
        RecordValidator(schema)


def test_validate_schema_resolves_a_plain_ref_to_a_dynamic_anchor() -> None:
    schema = {
        "type": "object",
        "properties": {"x": {"$ref": "#q"}},
        "$defs": {"d": {"$dynamicAnchor": "q", "type": "string"}},
    }
    _validate_schema(schema)
    assert [r.path for r in RecordValidator(schema).validate({"x": 1}).reasons] == ["$.x"]


def test_validate_schema_refuses_a_duplicate_dynamic_anchor() -> None:
    schema = {
        "type": "object",
        "properties": {"a": {"$dynamicAnchor": "t", "type": "string"}, "b": {"$dynamicAnchor": "t", "type": "integer"}},
    }
    with pytest.raises(
        SchemaValidationError, match=re.escape("is declared more than once, so '#t' could resolve to either")
    ):
        RecordValidator(schema)


def test_validate_schema_refuses_an_anchor_name_shared_with_a_dynamic_anchor() -> None:
    with pytest.raises(
        SchemaValidationError, match=re.escape("is declared more than once, so '#t' could resolve to either")
    ):
        _validate_schema(
            {
                "type": "object",
                "properties": {"a": {"$ref": "#t"}},
                "$defs": {"x": {"$anchor": "t", "type": "string"}, "y": {"$dynamicAnchor": "t", "type": "integer"}},
            }
        )


def _through_unknown_keyword(inner: dict) -> dict:
    """A schema whose field refs a node kept under an unknown keyword, which then carries ``inner``."""
    return {"type": "object", "properties": {"a": {"$ref": "#/x/t"}}, "x": {"t": inner}}


@pytest.mark.parametrize(
    ("schema", "match"),
    [
        (_through_unknown_keyword({"$ref": "#/nowhere"}), "does not resolve"),
        (
            {
                "type": "object",
                "properties": {"a": {"$ref": "#/properties/b/default"}, "b": {"default": {"$ref": "#/nowhere"}}},
            },
            "does not resolve",
        ),
        (_through_unknown_keyword({"$dynamicRef": "#foo"}), "dynamicRef"),
        (
            _through_unknown_keyword({"$ref": "https://example.com/s"}),
            _NOT_FETCHED,
        ),
        (
            _through_unknown_keyword({"$id": "https://other/", "type": "string"}),
            "below the root is not allowed: the schema is one resource",
        ),
        (_through_unknown_keyword({"$ref": "#nope"}), "does not resolve"),
        (_through_unknown_keyword({"type": 123}), "at x/t/type: 123 is not valid"),
        (
            {"type": "object", "properties": {"a": {"$ref": "#/required"}}, "required": ["a"]},
            "points at a value that is not a schema",
        ),
    ],
)
def test_validate_schema_checks_every_node_a_pointer_ref_reaches(schema: dict, match: str) -> None:
    # jsonschema evaluates a pointer ref's target as a schema wherever it sits (under an unknown or
    # a data keyword too), so the declaration rules hold for it and everything it reaches.
    with pytest.raises(SchemaValidationError, match=match):
        _validate_schema(schema)
    with pytest.raises(SchemaValidationError, match=match):
        RecordValidator(schema)


def test_validate_schema_accepts_a_well_formed_pointer_target_under_an_unknown_keyword() -> None:
    schema = _through_unknown_keyword({"type": "string"})
    _validate_schema(schema)
    assert [r.path for r in RecordValidator(schema).validate({"a": 1}).reasons] == ["$.a"]


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {"v": {"$ref": "#/$defs/x"}}, "$defs": {"x": {"$ref": "#/$defs/x"}}},
        {"type": "object", "properties": {"a": {"type": "string"}}, "$ref": "#"},
        {
            "type": "object",
            "properties": {"v": {"$ref": "#/$defs/x"}},
            "$defs": {"x": {"$ref": "#/$defs/y"}, "y": {"allOf": [{"$ref": "#/$defs/x"}]}},
        },
        {
            "type": "object",
            "properties": {"v": {"$ref": "#a"}},
            "$defs": {"x": {"$anchor": "a", "not": {"$ref": "#a"}}},
        },
    ],
)
def test_validate_schema_refuses_a_ref_cycle_that_never_descends(schema: dict) -> None:
    with pytest.raises(SchemaValidationError, match="cycle"):
        _validate_schema(schema)


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {"a": {"type": "string"}}, "if": {"$ref": "#"}},
        {"type": "object", "properties": {"a": {"type": "string"}}, "if": True, "then": {"$ref": "#"}},
        {"type": "object", "properties": {"a": {"type": "string"}}, "if": False, "else": {"$ref": "#"}},
        {"type": "object", "properties": {"a": {"type": "string"}}, "anyOf": [True, {"$ref": "#"}]},
        {"type": "object", "properties": {"a": {"type": "string"}}, "oneOf": [{"$ref": "#"}]},
        {"type": "object", "properties": {"a": {"type": "string"}}, "dependentSchemas": {"a": {"$ref": "#"}}},
    ],
)
def test_validate_schema_refuses_a_cycle_through_every_same_instance_applicator(schema: dict) -> None:
    with pytest.raises(
        SchemaValidationError, match="forms a cycle of references that never descends into the instance"
    ):
        _validate_schema(schema)


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {"a": {"type": "string"}}, "then": {"$ref": "#"}},
        {"type": "object", "properties": {"a": {"type": "string"}}, "else": {"$ref": "#"}},
        {"type": "object", "properties": {"a": {"type": "string"}}, "$defs": {"x": {"$ref": "#/$defs/x"}}},
    ],
)
def test_validate_schema_accepts_a_cycle_validation_never_evaluates(schema: dict) -> None:
    # ``then`` / ``else`` without ``if`` and a definition no reference reaches are never evaluated.
    _validate_schema(schema)
    assert [r.path for r in RecordValidator(schema).validate({"a": 1}).reasons] == ["$.a"]


@pytest.mark.parametrize(
    ("schema", "doc", "paths"),
    [
        (
            {
                "type": "object",
                "properties": {"n": {"type": "integer"}, "kids": {"type": "array", "items": {"$ref": "#"}}},
            },
            {"kids": [{"kids": [{"n": "s"}]}]},
            ["$.kids[0].kids[0].n"],
        ),
        (
            {
                "type": "object",
                "properties": {"n": {"type": "integer"}, "kids": {"contains": {"$ref": "#"}, "minContains": 0}},
            },
            {"kids": [{"n": 1}]},
            [],
        ),
        (
            {
                "type": "object",
                "properties": {"m": {"$ref": "#/$defs/s"}},
                "$defs": {"s": {"type": ["object", "string"], "maxLength": 3, "propertyNames": {"$ref": "#/$defs/s"}}},
            },
            {"m": {"long-name": 1}},
            ["$.m"],
        ),
        (
            {"type": "object", "properties": {"n": {"type": "integer"}}, "unevaluatedProperties": {"$ref": "#"}},
            {"extra": {"n": "s"}},
            ["$"],  # an unevaluated-property misfit is reported on the object carrying it
        ),
    ],
)
def test_validate_schema_accepts_recursion_into_child_instances(schema: dict, doc: dict, paths: list[str]) -> None:
    _validate_schema(schema)
    assert [r.path for r in RecordValidator(schema).validate(doc).reasons] == paths


def _not_an_index(ref: str, segment: str) -> str:
    return (
        f"$ref {ref!r} indexes an array with {segment!r}, which is not a JSON Pointer array index: "
        "write the index as a whole number like 0 or 2"
    )


@pytest.mark.parametrize("segment", ["\u2460", "\uff11", "-1", "+1", "01"])
def test_validate_schema_refuses_a_pointer_segment_that_is_not_an_array_index(segment: str) -> None:
    ref = f"#/allOf/{segment}"
    schema = {"type": "object", "properties": {"x": {"$ref": ref}}, "allOf": [{}, {}]}
    refusal = re.escape(f"state schema property 'x' breaks the JSON Schema rules: {_not_an_index(ref, segment)}")
    with pytest.raises(SchemaValidationError, match=refusal):
        _validate_schema(schema)
    with pytest.raises(SchemaValidationError, match=refusal):
        RecordValidator(schema)


def test_a_template_whose_pointer_is_not_an_array_index_is_refused_at_save() -> None:
    ref = "#/properties/a/anyOf/-1"
    fragment = {
        "type": "object",
        "properties": {"a": {"anyOf": [{"type": "string"}, {"type": "integer"}]}, "b": {"$ref": ref}},
    }
    refusal = (
        "template 't1' schema fragment is not a valid object schema: state schema property 'b' breaks the "
        f"JSON Schema rules: {_not_an_index(ref, '-1')}"
    )
    with pytest.raises(TemplateValidationError, match=re.escape(refusal)):
        validate_template({"kind": "state-template", "name": "t1", "schema": fragment})


@pytest.mark.parametrize("segment", ["01", "00", "\uff11", "-1"])
def test_the_pointer_reader_takes_an_array_index_only_in_json_pointer_form(segment: str) -> None:
    # The reader that rebases a template's pointers on attach reads an index as the rules check does.
    ref = f"#/allOf/{segment}"
    with pytest.raises(SchemaValidationError, match=re.escape(_not_an_index(ref, segment))):
        schema_nodes({"type": "object", "properties": {"x": {"$ref": ref}}, "allOf": [{}, {}]})


@pytest.mark.parametrize("segment", ["0", "1"])
def test_the_pointer_reader_follows_a_json_pointer_array_index(segment: str) -> None:
    # ``x-list`` is no subschema position, so only the pointer brings its item into the nodes.
    items: list[dict] = [{"type": "string"}, {"type": "integer"}]
    schema = {"type": "object", "properties": {"x": {"$ref": f"#/x-list/{segment}"}}, "x-list": items}
    reached = [i for i, item in enumerate(items) if any(node is item for node in schema_nodes(schema))]
    assert reached == [int(segment)]


def test_validate_schema_accepts_a_recursive_data_schema() -> None:
    schema = {"type": "object", "properties": {"n": {"type": "integer"}, "child": {"$ref": "#"}}}
    _validate_schema(schema)
    assert [r.path for r in RecordValidator(schema).validate({"child": {"child": {"n": "s"}}}).reasons] == [
        "$.child.child.n"
    ]


def test_validate_schema_counts_only_registered_anchors_for_uniqueness() -> None:
    # ``x`` sits under an unknown keyword: only ``b``'s anchor is registered, so ``#t`` is unambiguous.
    schema = {
        "type": "object",
        "properties": {"a": {"$ref": "#/x"}, "b": {"$anchor": "t", "type": "string"}, "c": {"$ref": "#t"}},
        "x": {"$anchor": "t", "type": "integer"},
    }
    _validate_schema(schema)
    assert [r.path for r in RecordValidator(schema).validate({"c": 1}).reasons] == ["$.c"]


_DRAFT_07 = "http://json-schema.org/draft-07/schema#"


@pytest.mark.parametrize(
    "schema",
    [
        {
            "type": "object",
            "properties": {"a": {"$schema": _DRAFT_07, "type": "object", "dependencies": {"x": {"$ref": "#"}}}},
        },
        {"$schema": _DRAFT_07, "type": "object", "properties": {"a": {"type": "string"}}},
        {"type": "object", "properties": {"a": {"$ref": "#/x/t"}}, "x": {"t": {"$schema": _DRAFT_07}}},
    ],
)
def test_validate_schema_refuses_a_schema_dialect_other_than_draft_2020_12(schema: dict) -> None:
    with pytest.raises(SchemaValidationError, match="is not supported — the schema is one draft 2020-12 document"):
        _validate_schema(schema)
    with pytest.raises(SchemaValidationError, match="draft 2020-12 document"):
        RecordValidator(schema)


def _reached_by_every_reference_form(target: dict) -> list[dict]:
    # ``target`` under an unknown keyword, reached by a pointer, by an absolute reference under the
    # root's ``$id`` and by a reference relative to it.
    return [
        {"type": "object", "properties": {"a": {"$ref": "#/x"}}, "x": target},
        {"$id": "https://e.com/s", "type": "object", "properties": {"a": {"$ref": "https://e.com/s#/x"}}, "x": target},
        {"$id": "https://e.com/s", "type": "object", "properties": {"a": {"$ref": "s#/x"}}, "x": target},
    ]


@pytest.mark.parametrize(
    ("schema", "match"),
    [
        *(
            (schema, "dynamicRef is not supported")
            for schema in _reached_by_every_reference_form({"$dynamicRef": "#/$defs/d"})
        ),
        *((schema, "draft 2020-12 document") for schema in _reached_by_every_reference_form({"$schema": _DRAFT_07})),
    ],
    ids=[f"{rule}-{form}" for rule in ("dynamic-ref", "dialect") for form in ("pointer", "absolute", "relative")],
)
def test_validate_schema_holds_the_dialect_rules_at_every_reference_form(schema: dict, match: str) -> None:
    schema = {**schema, "$defs": {"d": {"type": "string"}}}
    with pytest.raises(SchemaValidationError, match=match):
        _validate_schema(schema)
    with pytest.raises(SchemaValidationError, match=match):
        RecordValidator(schema)


_M20 = "https://json-schema.org/draft/2020-12/schema"
_REENTRY = (
    "at $defs/x/$dynamicAnchor: $dynamicAnchor 'meta' below the root is not allowed when the schema has an $id "
    "and refers into a JSON Schema meta-schema: the meta-schema's $dynamicRef '#meta' would re-enter the schema "
    "here and resolve its references against the meta-schema — rename the anchor"
)


def _meta_property(**root: object) -> dict:
    # A property checked against the draft 2020-12 meta-schema beside a ``$dynamicAnchor`` named as
    # the meta-schema's own, below the root.
    return {
        **root,
        "type": "object",
        "properties": {"m": {"$ref": _M20}},
        "$defs": {"x": {"$dynamicAnchor": "meta", "$ref": "#/$defs/y"}, "y": {"type": "object"}},
    }


def test_validate_schema_refuses_a_dynamic_anchor_a_meta_schema_would_re_enter() -> None:
    schema = _meta_property(**{"$id": "https://e.com/s"})
    with pytest.raises(SchemaValidationError, match=re.escape(_REENTRY)):
        _validate_schema(schema)
    with pytest.raises(SchemaValidationError, match=re.escape(_REENTRY)):
        RecordValidator(schema)


_ENTERED = (
    "at $defs/x/$dynamicAnchor: $dynamicAnchor 'meta' below the root is not allowed when the schema has an $id "
    "and refers into a JSON Schema meta-schema: {keyword} 'https://json-schema.org/draft/2020-12/schema#meta' "
    "names the meta-schema's $dynamicAnchor 'meta', which would re-enter the schema here and resolve its "
    "references against the meta-schema — rename the anchor"
)


@pytest.mark.parametrize(
    "defs",
    [
        {"x": {"$dynamicAnchor": "meta", "$ref": "#/$defs/y"}, "y": {"type": "object"}},
        {"x": {"$dynamicAnchor": "meta", "type": "object"}},
    ],
    ids=["anchor-with-a-reference", "anchor-without-references"],
)
@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef"])
def test_validate_schema_refuses_a_reference_entering_a_meta_schema_s_dynamic_anchor(keyword: str, defs: dict) -> None:
    schema = {"$id": "https://e.com/s", "type": "object", "properties": {"m": {keyword: f"{_M20}#meta"}}, "$defs": defs}
    with pytest.raises(SchemaValidationError, match=re.escape(_ENTERED.format(keyword=keyword))):
        _validate_schema(schema)
    with pytest.raises(SchemaValidationError, match=re.escape(_ENTERED.format(keyword=keyword))):
        RecordValidator(schema)


def test_validate_schema_accepts_a_meta_schema_reference_beside_that_anchor_without_a_root_id() -> None:
    validator = RecordValidator(_meta_property())
    assert validator.validate({"m": {"type": "string"}}).fits
    assert not validator.validate({"m": {"type": 1}}).fits


_APPLICATOR = "https://json-schema.org/draft/2020-12/meta/applicator"
_CORE = "https://json-schema.org/draft/2020-12/meta/core"


def _taken(uri: str) -> str:
    return (
        f"state schema breaks the JSON Schema rules at $id: $id {uri!r} names a JSON Schema meta-schema: a schema "
        "cannot take a meta-schema's identifier — give it an identifier of its own, or drop the $id"
    )


@pytest.mark.parametrize("uri", [_CORE, _APPLICATOR, _M20, f"{_M20}#"])
def test_validate_schema_refuses_a_root_id_naming_a_meta_schema(uri: str) -> None:
    schema = {"$id": uri, "type": "object", "properties": {"m": {"$ref": _M20}}}
    with pytest.raises(SchemaValidationError, match=re.escape(_taken(uri))):
        _validate_schema(schema)
    with pytest.raises(SchemaValidationError, match=re.escape(_taken(uri))):
        RecordValidator(schema)
    with pytest.raises(SchemaValidationError, match=re.escape(_taken(uri))):
        compose_effective_schema(schema, [])


def test_a_template_under_a_meta_schema_identifier_is_refused_at_save() -> None:
    fragment = {"$id": _APPLICATOR, "type": "object", "properties": {"m": {"$ref": _M20}}}
    refusal = f"template 't1' schema fragment is not a valid object schema: {_taken(_APPLICATOR)}"
    with pytest.raises(TemplateValidationError, match=re.escape(refusal)):
        validate_template({"kind": "state-template", "name": "t1", "schema": fragment})


def test_validate_schema_accepts_a_pointer_into_a_meta_schema_subschema_binding_no_dynamic_name() -> None:
    schema = {
        "$id": "https://e.com/s",
        "type": "object",
        "properties": {"m": {"$ref": f"{_CORE}#/$defs/anchorString"}},
        "$defs": {"x": {"$dynamicAnchor": "meta", "$ref": "#/$defs/y"}, "y": {"type": "object"}},
    }
    validator = RecordValidator(schema)
    assert validator.validate({"m": "ab"}).fits
    assert not validator.validate({"m": "1 2"}).fits


@pytest.mark.parametrize(
    "dialect", ["https://json-schema.org/draft/2020-12/schema", "https://json-schema.org/draft/2020-12/schema#"]
)
def test_validate_schema_accepts_an_explicit_draft_2020_12_dialect(dialect: str) -> None:
    _validate_schema({"$schema": dialect, "type": "object", "properties": {"a": {"$schema": dialect}}})


# A schema ``check_schema`` passes that a validator still cannot use: the keys of
# ``patternProperties`` joined with ``|`` (as ``additionalProperties`` matches them) do not
# compile, and a ``NaN`` bound compares false against every number, so it never applies.
_JOINED_KEYS_FAIL = {
    "type": "object",
    "patternProperties": {"x": {}, "(?i)y": {}},
    "additionalProperties": False,
}
_NAN_BOUND = {"type": "number", "maximum": float("nan")}
_UNUSABLE = [
    pytest.param(
        _JOINED_KEYS_FAIL,
        "breaks the JSON Schema rules: the keys joined with '|', which additionalProperties matches every other "
        "key against, do not compile",
        id="joined-pattern-keys",
    ),
    pytest.param(_NAN_BOUND, "that is not a finite number (nan)", id="nan-bound"),
]


@pytest.mark.parametrize(("subschema", "message"), _UNUSABLE)
def test_a_declared_schema_a_validator_cannot_use_is_refused(subschema: dict, message: str) -> None:
    schema = {"type": "object", "properties": {"a": subschema}}
    with pytest.raises(SchemaValidationError, match=re.escape("state schema property 'a' ")):
        _validate_schema(schema)
    with pytest.raises(SchemaValidationError, match=re.escape(message)):
        RecordValidator(schema)


def _shape_template():
    return validate_template(
        {
            "kind": "state-template",
            "name": "shape",
            "parameters": {"shape": {"schema": {"type": "object"}}},
            "schema": {"type": "object", "properties": {"a": {"$parameter": "shape"}}},
        }
    )


@pytest.mark.parametrize(("subschema", "message"), _UNUSABLE)
def test_a_composed_schema_a_validator_cannot_use_is_refused(subschema: dict, message: str) -> None:
    # The template is valid on its own; the attach parameter makes the composed document unusable.
    base = {"type": "object", "properties": {"n": {"type": "integer"}}}
    with pytest.raises(SchemaValidationError, match=re.escape("state schema property 'box' ")) as raised:
        compose_effective_schema(base, [(_shape_template(), ["box"], {"shape": subschema})])
    assert message in str(raised.value)


def test_a_record_checked_against_an_accepted_schema_meets_only_validation_errors() -> None:
    schema = {
        "type": "object",
        "$defs": {"tag": {"$anchor": "tag", "type": "string"}},
        "properties": {
            "a": {"oneOf": [{"$ref": "#tag"}, {"$ref": "#/$defs/tag", "minLength": 3}]},
            "b": {"type": "object", "patternProperties": {"^x": {}}, "additionalProperties": False},
        },
    }
    validator = RecordValidator(schema)
    for doc in ({"a": "s"}, {"a": 1}, {"b": {"y": 1}}, {"a": "sss", "b": {"x": 1}}):
        assert isinstance(validator.validate(doc), RecordValidation)


def test_document_reasons_empty_when_the_document_fits() -> None:
    validator = Draft202012Validator(_RECORD_SCHEMA)
    assert _document_reasons(validator, {"n": 1, "tag": "x"}) == []


def test_document_reasons_lists_every_failure_sorted_by_json_path() -> None:
    validator = Draft202012Validator(_RECORD_SCHEMA)
    # Two independent failures: the wrong type on ``n`` and an undeclared extra key. Both are
    # reported (never only the first), ordered by json_path.
    reasons = _document_reasons(validator, {"n": "nope", "extra": 1})
    paths = [r.path for r in reasons]
    assert paths == sorted(paths)
    joined = " ".join(f"{r.path} {r.message}" for r in reasons)
    assert "'tag' is a required property" in joined
    assert "extra" in joined  # additionalProperties refusal
    assert any(r.path == "$.n" for r in reasons)  # the type failure names the node


def test_document_reasons_reports_a_root_non_object() -> None:
    validator = Draft202012Validator(_RECORD_SCHEMA)
    reasons = _document_reasons(validator, ["not", "an", "object"])  # type: ignore[arg-type]
    assert reasons
    assert all(r.path == "$" for r in reasons)


def test_record_validator_answers_fits_with_no_reasons() -> None:
    assert RecordValidator(_RECORD_SCHEMA).validate({"n": 1, "tag": "x"}) == RecordValidation(fits=True, reasons=[])


def test_record_validator_reports_every_misfit_reason_sorted_by_json_path() -> None:
    doc = {"n": "nope", "extra": 1}
    result = RecordValidator(_RECORD_SCHEMA).validate(doc)
    assert result.fits is False
    paths = [r.path for r in result.reasons]
    assert paths == sorted(paths)
    joined = " ".join(f"{r.path} {r.message}" for r in result.reasons)
    assert "'tag' is a required property" in joined
    assert "extra" in joined
    assert any(r.path == "$.n" for r in result.reasons)
    assert result.reasons == _document_reasons(Draft202012Validator(_RECORD_SCHEMA), doc)


def test_record_validator_refuses_a_non_object_document() -> None:
    validator = RecordValidator(_RECORD_SCHEMA)
    with pytest.raises(ValueValidationError, match="a record document must be a JSON object"):
        validator.validate(["not", "an", "object"])


@pytest.mark.parametrize(
    ("schema", "match"),
    [
        ({"type": "string"}, '"type": "object"'),
        ({"type": "object", "properties": {}}, "at least one property"),
        ({"type": "object", "properties": {"n": {"type": 123}}}, "breaks the JSON Schema rules"),
        (
            {"type": "object", "properties": {"a": {"$ref": "http://x/y"}}},
            _NOT_FETCHED,
        ),
        ({"type": "object", "properties": {"a": {"$ref": "#/$defs/missing"}}}, "does not resolve"),
        (
            {"type": "object", "properties": {"x": {"type": "string"}}, "allOf": [{"$dynamicRef": "#m"}]},
            "dynamicRef",
        ),
    ],
)
def test_record_validator_refuses_an_undeclarable_schema_at_construction(schema: dict, match: str) -> None:
    with pytest.raises(SchemaValidationError, match=match):
        RecordValidator(schema)


def test_record_validator_is_exported_by_the_states_package() -> None:
    assert states_pkg.RecordValidator is RecordValidator


def test_record_validator_over_a_composed_effective_schema_with_trace() -> None:
    # A neutral consumer: a base schema and a tracing template parsed by the public template
    # parser, composed at a path into the effective schema a declared state validates against.
    base = {"type": "object", "properties": {"n": {"type": "integer"}}, "additionalProperties": False}
    template = validate_template(
        {
            "kind": "state-template",
            "name": "tag-box",
            "schema": {"type": "object", "properties": {"tag": {"type": "string"}}, "additionalProperties": False},
            "trace": {"enabled": True},
        }
    )
    validator = RecordValidator(compose_effective_schema(base, [(template, ["box"], {})]))

    stamped = {
        "n": 1,
        "box": {
            "tag": "x",
            "_trace": {"meta": None, "run": None, "turn": None, "inbound": None, "at": "2026-01-01T00:00:00Z"},
        },
    }
    assert validator.validate(stamped) == RecordValidation(fits=True, reasons=[])

    result = validator.validate({"n": 1, "box": {"tag": 5}})
    assert result.fits is False
    assert [r.path for r in result.reasons] == ["$.box.tag"]


def test_page_limit_clamps_and_refuses() -> None:
    assert _page_limit(None) == 200
    assert _page_limit(10) == 10
    assert _page_limit(10_000) == 500  # clamped to the hard cap
    with pytest.raises(ValueValidationError, match="positive integer"):
        _page_limit(0)
    with pytest.raises(ValueValidationError, match="positive integer"):
        _page_limit(True)
