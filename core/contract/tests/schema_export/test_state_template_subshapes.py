"""The state-template document describes its sub-shapes structurally.

An opaque ``dict`` field publishes as a bare ``object``, under-describing the exact
documents an editor generates its validators from. The document's schema must reach
a typed sub-model for ``template_jq`` / ``reconcile`` / ``declarations``, with the
program fields marked as templated text.
"""

from __future__ import annotations

from typing import Any, cast

from tai42_contract.states.models import StateTemplateDocument
from tai42_contract.template import TEMPLATED_TEXT_ANNOTATION_KEY


def _resolve(schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """The definition a single-``$ref`` (optionally nullable) property points at."""
    ref = cast("str | None", schema.get("$ref"))
    if ref is None:
        branches = cast("list[dict[str, Any]]", schema.get("anyOf", []))
        for branch in branches:
            if "$ref" in branch:
                ref = cast(str, branch["$ref"])
                break
    assert isinstance(ref, str), f"{schema} is not a reference to a sub-model"
    return cast("dict[str, Any]", defs[ref.rsplit("/", 1)[-1]])


def test_document_reaches_typed_subshapes() -> None:
    js = StateTemplateDocument.model_json_schema()
    defs = js["$defs"]
    props = js["properties"]

    declarations = _resolve(props["declarations"], defs)
    assert set(declarations["properties"]) == {"schema", "check"}

    reconcile = _resolve(props["reconcile"], defs)
    assert set(reconcile["properties"]) == {"orphans", "close", "resolutions"}

    object_branch = next(b for b in props["template_jq"]["anyOf"] if b.get("type") == "object")
    template_jq = _resolve(object_branch["additionalProperties"], defs)
    assert {"purpose", "jq", "params", "reads", "writes"} <= set(template_jq["properties"])
    purpose = template_jq["properties"]["purpose"]
    assert purpose.get("enum") == ["input", "update"]


def test_program_bodies_are_marked_templated_text() -> None:
    js = StateTemplateDocument.model_json_schema()
    assert TEMPLATED_TEXT_ANNOTATION_KEY in js["$defs"]["TemplatedText"]
    jq_schema = js["$defs"]["StateTemplateJq"]["properties"]["jq"]
    assert jq_schema.get("$ref", "").endswith("TemplatedText")
