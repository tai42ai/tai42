"""The pure schema validators and the page-size clamp — no store, no service instance."""

from __future__ import annotations

import re

import pytest
from jsonschema import Draft202012Validator
from tai42_contract.states import RecordValidation
from tai42_contract.states.errors import SchemaValidationError, ValueValidationError

from tai42_skeleton import states as states_pkg
from tai42_skeleton.states.schema import RecordValidator, _document_reasons, _validate_document, _validate_schema
from tai42_skeleton.states.service.rows import _page_limit
from tai42_skeleton.states.templates import compose_effective_schema, validate_template

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
    with pytest.raises(SchemaValidationError, match="not a valid JSON Schema"):
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
    with pytest.raises(SchemaValidationError, match="remote"):
        _validate_schema({"type": "object", "properties": {"a": {"$ref": "http://x/y"}}})
    with pytest.raises(SchemaValidationError, match="does not resolve"):
        _validate_schema({"type": "object", "properties": {"a": {"$ref": "#/$defs/missing"}}})
    with pytest.raises(SchemaValidationError, match="no \\$anchor"):
        _validate_schema({"type": "object", "properties": {"a": {"$ref": "#absent"}}})
    with pytest.raises(SchemaValidationError, match="dynamicRef"):
        _validate_schema({"type": "object", "properties": {"x": {"type": "string"}}, "allOf": [{"$dynamicRef": "#m"}]})


def test_validate_schema_refuses_an_id_below_the_root() -> None:
    with pytest.raises(SchemaValidationError, match="\\$id below the root"):
        _validate_schema({"type": "object", "properties": {"a": {"$id": "urn:example:a", "type": "string"}}})


def test_validate_schema_accepts_an_id_at_the_root() -> None:
    _validate_schema({"$id": "urn:example:root", "type": "object", "properties": {"a": {"type": "string"}}})


def test_validate_schema_accepts_a_field_named_like_a_keyword() -> None:
    # A property NAMED ``$id`` or ``$anchor`` is a field, not the keyword: its value is a subschema.
    _validate_schema({"type": "object", "properties": {"$id": {"type": "string"}, "$anchor": {"type": "string"}}})


def test_validate_schema_refuses_a_duplicate_anchor() -> None:
    with pytest.raises(
        SchemaValidationError, match=re.escape("anchor 't' ($anchor / $dynamicAnchor) is declared more than once")
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
        SchemaValidationError, match=re.escape("anchor 't' ($anchor / $dynamicAnchor) is declared more than once")
    ):
        RecordValidator(schema)


def test_validate_schema_refuses_an_anchor_name_shared_with_a_dynamic_anchor() -> None:
    with pytest.raises(
        SchemaValidationError, match=re.escape("anchor 't' ($anchor / $dynamicAnchor) is declared more than once")
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
        (_through_unknown_keyword({"$ref": "https://example.com/s"}), "remote"),
        (_through_unknown_keyword({"$id": "https://other/", "type": "string"}), "\\$id below the root"),
        (_through_unknown_keyword({"$ref": "#nope"}), "does not resolve"),
        (_through_unknown_keyword({"type": 123}), "not a valid JSON Schema"),
        ({"type": "object", "properties": {"a": {"$ref": "#/required"}}, "required": ["a"]}, "not a valid JSON Schema"),
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


def _count_check_schema(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls = [0]
    original = Draft202012Validator.check_schema.__func__  # type: ignore[attr-defined]

    def counting(cls, schema, format_checker=None):
        calls[0] += 1
        return original(cls, schema)

    monkeypatch.setattr(Draft202012Validator, "check_schema", classmethod(counting))
    return calls


def test_validate_schema_checks_a_def_many_refs_share_once(monkeypatch: pytest.MonkeyPatch) -> None:
    big = {"type": "object", "properties": {f"f{i}": {"type": "string"} for i in range(200)}}
    schema = {
        "type": "object",
        "properties": {f"p{i}": {"$ref": "#/$defs/big"} for i in range(300)},
        "$defs": {"big": big},
    }
    calls = _count_check_schema(monkeypatch)
    _validate_schema(schema)
    # The whole document once; a target at a subschema position is covered by it.
    assert calls[0] == 1


def test_validate_schema_checks_an_out_of_position_target_once(monkeypatch: pytest.MonkeyPatch) -> None:
    schema = {
        "type": "object",
        "properties": {f"p{i}": {"$ref": "#/x/t"} for i in range(300)},
        "x": {"t": {"type": "object", "properties": {"c": {"type": "string"}}}},
    }
    calls = _count_check_schema(monkeypatch)
    _validate_schema(schema)
    assert calls[0] == 2  # the whole document, then the one target under the unknown keyword


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
        {"type": "object", "properties": {"a": {"type": "string"}}, "then": {"$ref": "#"}},
        {"type": "object", "properties": {"a": {"type": "string"}}, "else": {"$ref": "#"}},
        {"type": "object", "properties": {"a": {"type": "string"}}, "anyOf": [True, {"$ref": "#"}]},
        {"type": "object", "properties": {"a": {"type": "string"}}, "oneOf": [{"$ref": "#"}]},
        {"type": "object", "properties": {"a": {"type": "string"}}, "dependentSchemas": {"a": {"$ref": "#"}}},
        # A cycle no validation reaches is still refused: the references never descend.
        {"type": "object", "properties": {"a": {"type": "string"}}, "$defs": {"x": {"$ref": "#/$defs/x"}}},
    ],
)
def test_validate_schema_refuses_a_cycle_through_every_same_instance_applicator(schema: dict) -> None:
    with pytest.raises(
        SchemaValidationError, match="forms a cycle of references that never descends into the document"
    ):
        _validate_schema(schema)


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


def test_validate_schema_refuses_a_non_ascii_digit_pointer_index() -> None:
    schema = {"type": "object", "properties": {"x": {"$ref": "#/allOf/\u2460"}}, "allOf": [{}]}
    with pytest.raises(SchemaValidationError, match="is missing"):
        _validate_schema(schema)
    with pytest.raises(SchemaValidationError, match="is missing"):
        RecordValidator(schema)


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
            "properties": {"a": {"$schema": _DRAFT_07, "type": "object", "dependencies": {"x": {"$ref": "#/nope"}}}},
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


@pytest.mark.parametrize(
    "dialect", ["https://json-schema.org/draft/2020-12/schema", "https://json-schema.org/draft/2020-12/schema#"]
)
def test_validate_schema_accepts_an_explicit_draft_2020_12_dialect(dialect: str) -> None:
    _validate_schema({"$schema": dialect, "type": "object", "properties": {"a": {"$schema": dialect}}})


def test_validate_document_reports_unresolvable_ref() -> None:
    # A schema whose $ref cannot be resolved at validation time surfaces as a loud value
    # error, never an opaque referencing exception.
    schema = {"type": "object", "properties": {"a": {"$ref": "#/$defs/missing"}}}
    with pytest.raises(ValueValidationError):
        _validate_document(Draft202012Validator(schema), {"a": 1})


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


def test_document_reasons_raises_loudly_on_an_unresolvable_ref() -> None:
    schema = {"type": "object", "properties": {"a": {"$ref": "#/$defs/missing"}}}
    with pytest.raises(ValueValidationError, match="unresolvable"):
        _document_reasons(Draft202012Validator(schema), {"a": 1})


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
        ({"type": "object", "properties": {"n": {"type": 123}}}, "not a valid JSON Schema"),
        ({"type": "object", "properties": {"a": {"$ref": "http://x/y"}}}, "remote"),
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
