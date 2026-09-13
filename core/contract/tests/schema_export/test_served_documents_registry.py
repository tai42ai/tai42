"""The served-document registry and the bundle it produces.

The registry is EXPLICIT, so every entry is a real contract model and every published
document maps back to one; the bundle pins the contract version it was cut from; and
every modeled templated-text body is published as a reference to the marked
``TemplatedText`` definition, never a bare string — the platform half of the
"a body can never again be a bare string" guarantee.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from importlib.metadata import version
from typing import Any, cast

import pytest
from pydantic import BaseModel

from tai42_contract.schema_export import build_document_schemas, bundle_json
from tai42_contract.schema_export.registry import SERVED_DOCUMENTS
from tai42_contract.template import TEMPLATED_TEXT_ANNOTATION_KEY

# A minimal valid document for every reshaped state-family model — the surface this
# publication reshapes — so a round-trip through ``model_validate`` proves the registry
# model accepts and re-emits its own served shape.
_ROUND_TRIP_FIXTURES: dict[str, dict[str, Any]] = {
    "StateDeclaration": {
        "name": "notes",
        "schema": {"type": "object"},
        "subject_kinds": ["person"],
        "default_subject_kind": "person",
    },
    "StateTemplateDocument": {
        "name": "planner",
        "schema": {"type": "object"},
        "declarations": {"schema": {"type": "object"}, "check": {"content": ". == true"}},
        "template_jq": {
            "due": {"purpose": "input", "params": ["run"], "jq": {"content": "."}},
            "outcome": {"purpose": "update", "reads": [["l"]], "writes": [["l"]], "jq": {"content": "[]"}},
        },
        "reconcile": {"orphans": {"content": "."}, "close": {"content": "[]"}, "resolutions": {"content": "[]"}},
    },
    "StateInjection": {"template_jq": "due", "into": "context"},
}


def test_registry_entries_are_models_and_match_the_bundle() -> None:
    bundle = build_document_schemas()
    assert set(SERVED_DOCUMENTS) == set(bundle["documents"])
    for name, model in SERVED_DOCUMENTS.items():
        assert isinstance(model, type), name
        assert issubclass(model, BaseModel), name
        assert bundle["documents"][name], f"{name} published an empty schema"


def test_bundle_is_deterministic_and_json_stable() -> None:
    first = build_document_schemas()
    assert first == build_document_schemas()
    assert json.loads(bundle_json(first)) == first


def test_bundle_carries_the_running_contract_version() -> None:
    assert build_document_schemas()["contract_version"] == version("tai42-contract")


@pytest.mark.parametrize("name", sorted(_ROUND_TRIP_FIXTURES))
def test_state_family_documents_round_trip(name: str) -> None:
    model = SERVED_DOCUMENTS[name]
    parsed = model.model_validate(_ROUND_TRIP_FIXTURES[name])
    assert model.model_validate(parsed.model_dump(by_alias=True, exclude_none=True)) == parsed


def _templated_text_is_marked(bundle: dict[str, Any]) -> None:
    templated = bundle["$defs"]["TemplatedText"]
    assert TEMPLATED_TEXT_ANNOTATION_KEY in templated


def _refs(node: Any) -> Iterator[str]:
    """Every ``$ref`` string reachable from ``node``."""
    if isinstance(node, dict):
        mapping = cast(dict[str, Any], node)
        ref = mapping.get("$ref")
        if isinstance(ref, str):
            yield ref
        for value in mapping.values():
            yield from _refs(value)
    elif isinstance(node, list):
        for item in cast(list[Any], node):
            yield from _refs(item)


def test_templated_bodies_reference_the_marked_definition() -> None:
    # A property known to carry a templated body must reach the marked ``TemplatedText``
    # definition through a ``$ref`` — never be published as a bare string. The marker on
    # that definition is what a schema-to-validator generator keys on to emit a
    # templated-text validator instead of a plain string.
    bundle = build_document_schemas()
    _templated_text_is_marked(bundle)
    templated_ref = "#/$defs/TemplatedText"
    expectations = {
        "StateTemplateJq": ["jq"],
        "StateTemplateReconcile": ["orphans", "close", "resolutions"],
        "StateTemplateDeclarations": ["check"],
    }
    for def_name, properties in expectations.items():
        schema = bundle["$defs"][def_name]
        for prop in properties:
            assert templated_ref in set(_refs(schema["properties"][prop])), f"{def_name}.{prop} is not templated"
    # The declaration and template documents themselves admit a templated ``schema``.
    for document in ("StateDeclaration", "StateTemplateDocument"):
        schema = bundle["documents"][document]
        assert templated_ref in set(_refs(schema["properties"]["schema"])), f"{document}.schema is not templated"
