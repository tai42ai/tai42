"""Static validity of a state's JSON-Schema and its stored document.

The pure draft 2020-12 validators the template document parser and the states service
both lean on: an object-rooted schema check, the document rules every schema a record or a
value is validated against holds (:func:`check_document_rules`), full-document validation
against an effective schema, and the conservative narrowing comparison two schemas are
compared with. :class:`RecordValidator` is the public face over these validators for a caller
with no store bound. Depends only on the standard library, ``jsonschema``/``referencing``, the
kit's JSON Schema rules and the contract's states models.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import unquote

import jsonschema
from jsonschema import Draft202012Validator
from referencing.jsonschema import DRAFT202012
from tai42_contract.states.errors import SchemaValidationError, ValueValidationError
from tai42_contract.states.validation import RecordValidation, ValidationReason
from tai42_kit.utils.data.json_schema_util import SCHEMA_REFS, evaluated_subschemas, json_pointer_array_index


def _canonical(value: Any) -> str:
    """A byte-stable canonical form for comparing two field schemas for equality."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _validate_schema(schema: Any) -> None:
    """Accept any valid, object-rooted JSON Schema (draft 2020-12) with at least one property.

    Refuse everything else loudly. Nesting to any depth is the point — the record
    document is validated WHOLE against this schema on every write.
    """
    if not isinstance(schema, dict):
        raise SchemaValidationError("schema must be a JSON object")
    if schema.get("type") != "object":
        raise SchemaValidationError('schema must declare "type": "object"')
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        raise SchemaValidationError("schema must declare at least one property")
    check_document_rules(schema, subject="state schema")


# The draft 2020-12 metaschema URI, as a ``$schema`` value may spell it: any other ``$schema``
# makes jsonschema switch dialect for that node, whose keyword positions the rules do not model.
_DRAFT_2020_12 = frozenset(
    {"https://json-schema.org/draft/2020-12/schema", "https://json-schema.org/draft/2020-12/schema#"}
)


def check_document_rules(schema: Any, *, subject: str) -> None:
    """Refuse a schema a draft 2020-12 validator could not check a document against, naming the fault.

    The JSON Schema rules are the kit's
    (:func:`~tai42_kit.utils.data.json_schema_util.check_schema_rules` with
    :class:`~jsonschema.Draft202012Validator`): the meta-schema, compiling ``pattern`` and
    ``patternProperties`` regexes, finite numbers, one resource (no root ``$id`` naming a
    meta-schema, no ``$id`` below the root, unique anchor names, and, under a root ``$id``, no
    ``$dynamicAnchor`` below the root whose name the meta-schema subschema a reference reaches binds
    dynamically or the reference names), every reference
    resolving inside the schema or into a meta-schema (none is fetched), every pointer indexing an
    array with an RFC 6901 index, and no cycle of references that never descends into the
    document. On top of them, a
    states document is one draft 2020-12 dialect without dynamic references: a ``$schema`` naming
    another dialect, or a ``$dynamicRef``, anywhere among the subschemas validation evaluates
    (:func:`~tai42_kit.utils.data.json_schema_util.evaluated_subschemas`: under every applicator
    and at every reference target, whatever form the reference takes) is refused. ``subject``
    opens the message (``"state schema"``, a template's value-schema place). Every refusal raises
    :class:`~tai42_contract.states.errors.SchemaValidationError`.
    """
    try:
        nodes = evaluated_subschemas(schema, Draft202012Validator, subject=subject)
    except ValueError as exc:
        raise SchemaValidationError(str(exc)) from exc
    if any("$dynamicRef" in node for node in nodes):
        raise SchemaValidationError(f"{subject}: $dynamicRef is not supported — use $ref with root-level $defs")
    for node in nodes:
        dialect = node.get("$schema")
        if dialect is not None and dialect not in _DRAFT_2020_12:
            raise SchemaValidationError(
                f"{subject}: $schema {dialect!r} is not supported — the schema is one draft 2020-12 document"
            )


def schema_nodes(root: dict[str, Any]) -> list[dict[str, Any]]:
    """``root``'s subschemas and every node a ``#/…`` pointer ref among them reaches, each once.

    ``root``'s subschemas (:func:`_subschemas`), closed over the target of every ``#/…`` pointer
    ref among them (an absolute ref is not followed): jsonschema evaluates a pointer's target as a
    schema wherever it sits (under an unknown or a data keyword too) and follows the refs inside
    it, so the target's subschemas join the set, to a fixpoint (a node already in the set is not
    walked again, so cyclic refs end). Each pointer is resolved against ``root``; one that does
    not resolve raises :class:`SchemaValidationError`. A target outside the subschema positions —
    which ``root``'s own ``check_schema`` does not cover — is checked once as a draft 2020-12
    schema, so the walk stays linear in the schema's size and its ref count.
    """
    checked = {id(node) for node in _subschemas(root)}
    seen: dict[int, dict[str, Any]] = {}

    def visit(node: Any) -> None:
        if not isinstance(node, dict) or id(node) in seen:
            return
        seen[id(node)] = node
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            target = _resolve_pointer(root, ref)
            if id(target) not in seen and id(target) not in checked:
                _check_pointer_target(ref, target)
                checked.update(id(sub) for sub in _subschemas(target))
            visit(target)
        for child in DRAFT202012.subresources_of(node):
            visit(child)

    visit(root)
    return list(seen.values())


def _resolve_pointer(root: dict[str, Any], ref: str) -> Any:
    """The node the ``#/…`` pointer ``ref`` names in ``root``.

    An array is indexed only by an RFC 6901 index (``0`` or digits without a leading zero), as the
    JSON Schema rules check reads it. A segment that is no such index, or a missing node, raises
    :class:`SchemaValidationError`.
    """
    node: Any = root
    for raw in unquote(ref[2:]).split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and token in node:
            node = node[token]
            continue
        if isinstance(node, list):
            try:
                index = json_pointer_array_index(raw, keyword="$ref", ref=ref)
            except ValueError as exc:
                raise SchemaValidationError(str(exc)) from exc
            if index < len(node):
                node = node[index]
                continue
        raise SchemaValidationError(f"$ref {ref!r} does not resolve — {token!r} is missing")
    return node


def _check_pointer_target(ref: str, target: Any) -> None:
    """Refuse a pointer ``ref`` whose ``target`` is not a valid draft 2020-12 schema."""
    try:
        Draft202012Validator.check_schema(target)
    except jsonschema.SchemaError as exc:
        raise SchemaValidationError(
            f"$ref {ref!r} points at a node that is not a valid JSON Schema (draft 2020-12): {exc.message}"
        ) from exc


def _subschemas(schema: Any):
    """Yield ``schema`` and every subschema within it, as draft 2020-12 itself locates them.

    The positions come from the ``referencing`` library's draft 2020-12 specification — the
    same view ``jsonschema`` resolves references with — so the walk never enters instance data
    (``const``, ``enum``, ``default``, ``examples``). Boolean subschemas carry no keywords and
    are skipped.
    """
    if not isinstance(schema, dict):
        return
    yield schema
    for child in DRAFT202012.subresources_of(schema):
        yield from _subschemas(child)


def iter_refs(node: Any):
    """Yield the ``$ref`` string of every node of the schema document's :func:`schema_nodes`."""
    if not isinstance(node, dict):
        return
    for schema_node in schema_nodes(node):
        ref = schema_node.get("$ref")
        if isinstance(ref, str):
            yield ref


def _validate_document(validator: Draft202012Validator, doc: dict[str, Any], *, where: str | None = None) -> None:
    """Validate the FULL record document with the effective schema's ``validator``.

    The error names the offending JSON path, prefixed with ``where`` (the state and subject a
    batch validation checks) when given. Loud on the first failure.
    """
    prefix = "" if where is None else f"{where}: "
    try:
        validator.validate(doc)
    except jsonschema.ValidationError as exc:
        raise ValueValidationError(
            f"{prefix}record invalid under the state schema at {exc.json_path}: {exc.message}"
        ) from exc


def _document_reasons(validator: Draft202012Validator, doc: dict[str, Any]) -> list[ValidationReason]:
    """EVERY way ``doc`` fails ``validator``'s effective schema, ordered by JSON path.

    The read-only sibling of :func:`_validate_document`: where the write path raises on the first
    failure, this collects them all through ``validator.iter_errors`` so a caller reports the whole
    misfit at once. Each reason carries the offending node's ``json_path`` and the validator's
    message; an empty list means the document fits.
    """
    errors = list(validator.iter_errors(doc))
    errors.sort(key=lambda error: (error.json_path, error.message))
    return [ValidationReason(path=error.json_path, message=error.message) for error in errors]


class RecordValidator:
    """Validates record documents against one schema, as a write to a declared state would.

    For a caller with no store bound (a test, a check before a deploy): give it the schema a
    record is validated WHOLE against — a declared state's effective schema (the base schema
    with its attachments composed in) — and call :meth:`validate` per document. The schema is
    checked once at construction by the same rules a declaration is (object-rooted, at least
    one property, and :func:`check_document_rules`), and refused loudly with
    :class:`~tai42_contract.states.errors.SchemaValidationError`; the compiled validator is
    then reused for every document. A by-id schema is resolved by the caller first.
    """

    def __init__(self, schema: dict[str, Any]) -> None:
        """Check ``schema`` by the declaration rules, then compile its draft 2020-12 validator once."""
        _validate_schema(schema)
        self._validator = Draft202012Validator(schema, registry=SCHEMA_REFS)

    def validate(self, data: Any) -> RecordValidation:
        """EVERY way ``data`` misfits the schema, ordered by JSON path; ``fits`` when there is none.

        A non-object ``data`` raises :class:`~tai42_contract.states.errors.ValueValidationError`.
        """
        if not isinstance(data, dict):
            raise ValueValidationError("a record document must be a JSON object")
        reasons = _document_reasons(self._validator, data)
        return RecordValidation(fits=not reasons, reasons=reasons)


def _is_narrowing(old_schema: dict[str, Any], new_schema: dict[str, Any]) -> bool:
    """Whether ``new_schema`` removes or changes any top-level property of ``old_schema``.

    Also true when it changes any ROOT keyword outside ``properties``.
    Deliberately conservative: any property-subtree edit or root-keyword edit
    registers as narrowing.
    """
    old_props = old_schema.get("properties", {}) if isinstance(old_schema, dict) else {}
    new_props = new_schema.get("properties", {})
    for fname, fschema in old_props.items():
        if fname not in new_props or _canonical(new_props[fname]) != _canonical(fschema):
            return True
    old_root = {k: v for k, v in old_schema.items() if k != "properties"} if isinstance(old_schema, dict) else {}
    new_root = {k: v for k, v in new_schema.items() if k != "properties"}
    return _canonical(old_root) != _canonical(new_root)


__all__ = [
    "RecordValidator",
    "check_document_rules",
    "iter_refs",
    "schema_nodes",
]
