"""The ``x-tai42-expression`` vendor annotation on jq-typed string fields.

The mixins declare a generic payload on ``condition`` / ``expr``; each inheriting
surface either carries it as-is (hook registration, access-control policy/role)
or overrides it with surface-specific facts (the backend callback). The
annotation is schema METADATA only and strictly additive: every other property —
and the annotated properties themselves, minus the vendor key — must generate
byte-identical schemas to plain field declarations.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel

from tai42_contract.access_control import AccessPolicy, RoleDefinition
from tai42_contract.backend import CallbackSchema
from tai42_contract.hooks import HookParams, HookRegister
from tai42_contract.template import (
    EXPRESSION_ANNOTATION_KEY,
    ConditionMixin,
    ExprMixin,
    expression_annotation,
)

# The generic mixin payloads, pinned VERBATIM: this is the wire contract schema
# consumers read, so a wording change here is a deliberate contract change.
GENERIC_CONDITION_PAYLOAD: dict[str, Any] = {
    "language": "jq",
    "label": "condition",
    "blurb": "the declaring surface's input document",
    "returns": "truthy to proceed; a falsy result gates the surface's action off",
}

GENERIC_EXPR_PAYLOAD: dict[str, Any] = {
    "language": "jq",
    "label": "expression",
    "blurb": "the declaring surface's input document",
    "returns": "the transformed value the declaring surface consumes",
}

# The callback overrides, pinned VERBATIM: both expressions run over the finished
# backend task's raw tool result (untyped, hence ``keys: []``).
CALLBACK_CONDITION_PAYLOAD: dict[str, Any] = {
    "language": "jq",
    "label": "condition",
    "blurb": "the backend task's tool result",
    "keys": [],
    "returns": "truthy to run the callback",
    "sample": {},
}

CALLBACK_EXPR_PAYLOAD: dict[str, Any] = {
    "language": "jq",
    "label": "expression",
    "blurb": "the backend task's tool result",
    "keys": [],
    "returns": "an object — the follow-up tool's kwargs",
    "caveats": [
        "an empty expression, or a jq pipeline producing no output, "
        "yields {} (the follow-up tool runs with empty kwargs) — not an error"
    ],
    "sample": {},
}


# -- the payload builder -------------------------------------------------------


def test_builder_always_tags_jq_and_omits_absent_entries() -> None:
    assert expression_annotation() == {"language": "jq"}


def test_builder_emits_every_supplied_entry_in_the_contract_shape() -> None:
    payload = expression_annotation(
        label="verse",
        blurb="the stanza envelope",
        keys=[("meter", "beats per line"), ("rhyme", "the ending scheme")],
        returns="a trimmed stanza object",
        caveats=["an unbound meter reads as free verse"],
        sample={"meter": 4},
    )
    assert payload == {
        "language": "jq",
        "label": "verse",
        "blurb": "the stanza envelope",
        "keys": [
            {"name": "meter", "gloss": "beats per line"},
            {"name": "rhyme", "gloss": "the ending scheme"},
        ],
        "returns": "a trimmed stanza object",
        "caveats": ["an unbound meter reads as free verse"],
        "sample": {"meter": 4},
    }


def test_builder_distinguishes_empty_keys_and_falsy_samples_from_absent() -> None:
    # ``keys=[]`` states "untyped input" and ``sample=None``/``{}`` are real
    # sample documents — all distinct from omitting the argument.
    assert expression_annotation(keys=[])["keys"] == []
    assert expression_annotation(sample=None)["sample"] is None
    assert expression_annotation(sample={})["sample"] == {}
    assert "sample" not in expression_annotation()


def test_builder_payload_is_json_serializable() -> None:
    payload = expression_annotation(keys=[("k", "g")], caveats=["c"], sample={"n": 1})
    assert json.loads(json.dumps(payload)) == payload


# -- mixin-level schemas -------------------------------------------------------


def test_mixin_condition_and_expr_carry_the_generic_annotation() -> None:
    cond = ConditionMixin.model_json_schema()["properties"]["condition"]
    expr = ExprMixin.model_json_schema()["properties"]["expr"]
    assert cond[EXPRESSION_ANNOTATION_KEY] == GENERIC_CONDITION_PAYLOAD
    assert expr[EXPRESSION_ANNOTATION_KEY] == GENERIC_EXPR_PAYLOAD


def test_template_companion_fields_stay_unannotated() -> None:
    # Only the jq STRINGS are expressions; the template id/kwargs companions are not.
    cond_props = ConditionMixin.model_json_schema()["properties"]
    expr_props = ExprMixin.model_json_schema()["properties"]
    for name in ("condition_id", "condition_kwargs"):
        assert EXPRESSION_ANNOTATION_KEY not in cond_props[name]
    for name in ("expr_id", "expr_kwargs"):
        assert EXPRESSION_ANNOTATION_KEY not in expr_props[name]


def test_annotation_is_purely_additive_to_a_plain_declaration() -> None:
    # A control model declared WITHOUT the annotation must generate the same
    # schema, byte for byte, once the vendor key is removed — proving the
    # annotation adds one key and changes nothing else (defaults, titles,
    # nullability, required set).
    class PlainConditionModel(BaseModel):
        condition: str | None = None
        condition_id: str | None = None
        condition_kwargs: dict[str, Any] | None = None

    annotated = ConditionMixin.model_json_schema()
    control = PlainConditionModel.model_json_schema()
    assert annotated["properties"]["condition"].pop(EXPRESSION_ANNOTATION_KEY) == GENERIC_CONDITION_PAYLOAD
    for schema in (annotated, control):
        schema.pop("title")
    assert json.dumps(annotated, sort_keys=True) == json.dumps(control, sort_keys=True)


# -- every inheriting surface --------------------------------------------------


@pytest.mark.parametrize(
    ("model", "field", "payload"),
    [
        (HookRegister, "condition", GENERIC_CONDITION_PAYLOAD),
        (HookRegister, "expr", GENERIC_EXPR_PAYLOAD),
        (HookParams, "condition", GENERIC_CONDITION_PAYLOAD),
        (HookParams, "expr", GENERIC_EXPR_PAYLOAD),
        (AccessPolicy, "condition", GENERIC_CONDITION_PAYLOAD),
        (RoleDefinition, "condition", GENERIC_CONDITION_PAYLOAD),
        (CallbackSchema, "condition", CALLBACK_CONDITION_PAYLOAD),
        (CallbackSchema, "expr", CALLBACK_EXPR_PAYLOAD),
    ],
)
def test_inheriting_surfaces_carry_the_annotation(model: type[BaseModel], field: str, payload: dict[str, Any]) -> None:
    prop = model.model_json_schema()["properties"][field]
    assert prop[EXPRESSION_ANNOTATION_KEY] == payload


def test_callback_override_changes_only_the_annotation_payload() -> None:
    # The callback redeclares ``condition``/``expr`` solely to refine the vendor
    # payload; type, default, and field ORDER must match the mixin composition of
    # a callback that never overrode them.
    class PlainMixinCondition(BaseModel):
        condition: str | None = None
        condition_id: str | None = None
        condition_kwargs: dict[str, Any] | None = None

    class PlainMixinExpr(BaseModel):
        expr: str | None = None
        expr_id: str | None = None
        expr_kwargs: dict[str, Any] | None = None

    class PlainCallbackSchema(PlainMixinCondition, PlainMixinExpr):
        tool: str = ""

    annotated = CallbackSchema.model_json_schema()
    control = PlainCallbackSchema.model_json_schema()
    assert list(annotated["properties"]) == list(control["properties"])
    assert annotated["properties"]["condition"].pop(EXPRESSION_ANNOTATION_KEY) == CALLBACK_CONDITION_PAYLOAD
    assert annotated["properties"]["expr"].pop(EXPRESSION_ANNOTATION_KEY) == CALLBACK_EXPR_PAYLOAD
    for schema in (annotated, control):
        schema.pop("title")
    assert json.dumps(annotated, sort_keys=True) == json.dumps(control, sort_keys=True)


def test_unannotated_string_fields_are_byte_identical_to_plain_declarations() -> None:
    # An unannotated string property must not pick up the vendor key nor any
    # other drift: ``tool`` and hook ``name`` pin the exact schema bytes.
    tool_prop = CallbackSchema.model_json_schema()["properties"]["tool"]
    assert json.dumps(tool_prop, sort_keys=True) == json.dumps(
        {"default": "", "title": "Tool", "type": "string"}, sort_keys=True
    )
    name_prop = HookRegister.model_json_schema()["properties"]["name"]
    assert json.dumps(name_prop, sort_keys=True) == json.dumps(
        {"minLength": 1, "title": "Name", "type": "string"}, sort_keys=True
    )


def test_annotation_payloads_serialize_into_openapi_json() -> None:
    # The annotation must survive a plain JSON round-trip of the whole model
    # schema — the path an OpenAPI document takes to any client.
    for model in (HookRegister, AccessPolicy, RoleDefinition, CallbackSchema):
        schema = model.model_json_schema()
        assert json.loads(json.dumps(schema)) == schema
