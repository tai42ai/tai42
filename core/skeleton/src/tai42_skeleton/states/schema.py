"""Static validity of a state's JSON-Schema and its stored document.

The pure draft 2020-12 validators the template document parser and the states service
both lean on: an object-rooted schema check, ``$ref`` resolvability, full-document
validation against an effective schema, and the conservative narrowing comparison two
schemas are compared with. :class:`RecordValidator` is the public face over these
validators for a caller with no store bound. Depends only on the standard library,
``jsonschema``/``referencing`` and the contract's states models.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from urllib.parse import unquote

import jsonschema
import referencing.exceptions
from jsonschema import Draft202012Validator
from referencing.jsonschema import DRAFT202012
from tai42_contract.states.errors import SchemaValidationError, ValueValidationError
from tai42_contract.states.validation import RecordValidation, ValidationReason


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
    try:
        Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as exc:
        raise SchemaValidationError(f"schema is not a valid JSON Schema (draft 2020-12): {exc.message}") from exc
    _validate_refs(schema)


def _validate_refs(schema: dict[str, Any]) -> None:
    """Refuse ``$ref``s ``check_schema`` cannot vouch for (SYNTAX-only).

    A remote ref, a dangling local one, ``$dynamicRef``, a ``$schema`` other than draft
    2020-12, a ``$id`` below the document root, an anchor name declared more than once and a
    ref cycle that never descends into the document are all declare-time refusals: the
    schema is ONE draft 2020-12 document every local ``$ref`` resolves against. Local
    support: ``#`` (root), ``#/json/pointer`` (resolved against the document), and
    ``#anchor`` (a ``$anchor`` or ``$dynamicAnchor`` name). The ref rules run over every node
    validation can evaluate as a schema (:func:`_schema_nodes`), never inside data keywords
    such as ``const``, ``enum``, ``default`` or ``examples`` unless a pointer ref targets
    them. Expects ``schema`` to have passed ``check_schema``.
    """
    positioned = list(_subschemas(schema))
    nodes = _schema_nodes(schema, positioned)
    _validate_one_document(schema, nodes, positioned)
    anchors = _anchor_targets(positioned)
    for node in nodes:
        ref = node.get("$ref")
        if not isinstance(ref, str):
            continue
        if not ref.startswith("#"):
            raise SchemaValidationError(f"remote $ref {ref!r} is not supported — inline the schema or use $defs")
        if ref == "#" or ref.startswith("#/"):
            continue  # a pointer ref's target was resolved by ``_schema_nodes``
        anchor = ref[1:]
        if anchor not in anchors:
            raise SchemaValidationError(
                f"$ref {ref!r} does not resolve — no $anchor or $dynamicAnchor {anchor!r} in the schema"
            )
    _refuse_in_place_cycles(schema, nodes, anchors)


# The draft 2020-12 metaschema URI, as a ``$schema`` value may spell it: any other ``$schema``
# makes jsonschema switch dialect for that node, whose keyword positions the rules do not model.
_DRAFT_2020_12 = frozenset(
    {"https://json-schema.org/draft/2020-12/schema", "https://json-schema.org/draft/2020-12/schema#"}
)


def _validate_one_document(
    schema: dict[str, Any], nodes: list[dict[str, Any]], positioned: list[dict[str, Any]]
) -> None:
    """Refuse the keywords that would make local ``$ref`` resolve other than against the document root.

    ``$dynamicRef``, a ``$schema`` naming any dialect but draft 2020-12 (validation would switch
    dialect there), and a ``$id`` below the root (a nested resource re-bases resolution), over
    ``nodes`` (the schema's :func:`_schema_nodes`); and an anchor name (``$anchor`` or
    ``$dynamicAnchor``, both answer a plain ``#name`` ref) declared on more than one of the
    ``positioned`` subschemas — the only anchors the ``referencing`` library registers, so a
    ``#name`` ref would resolve to just one of them.
    """
    if any("$dynamicRef" in node for node in nodes):
        raise SchemaValidationError("$dynamicRef is not supported — use $ref with root-level $defs")
    for node in nodes:
        dialect = node.get("$schema")
        if dialect is not None and dialect not in _DRAFT_2020_12:
            raise SchemaValidationError(
                f"$schema {dialect!r} is not supported — the schema is one draft 2020-12 document"
            )
    if any(DRAFT202012.id_of(node) is not None for node in nodes if node is not schema):
        raise SchemaValidationError(
            "$id below the root is not supported — the schema is one document; local $ref resolves against its root"
        )
    # One entry per (name, subschema): a subschema carrying the same name as both kinds is one target.
    declared = {(anchor.name, id(node)) for node in positioned for anchor in DRAFT202012.anchors_in(node)}
    names = [name for name, _node in declared]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise SchemaValidationError(
            f"anchor {duplicates[0]!r} ($anchor / $dynamicAnchor) is declared more than once — anchor names are "
            f"unique across the effective schema"
        )


def _schema_nodes(root: dict[str, Any], positioned: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Every node validation against ``root`` can evaluate as a schema, each once.

    ``root``'s subschemas (:func:`_subschemas`, passed as ``positioned`` when the caller has
    them), closed over the target of every ``#/…`` pointer ref among them: jsonschema evaluates
    a pointer's target as a schema wherever it sits (under an unknown or a data keyword too)
    and follows the refs inside it, so the target's subschemas join the set, to a fixpoint (a
    node already in the set is not walked again, so cyclic refs end). Each pointer is resolved
    against ``root``; one that does not resolve raises :class:`SchemaValidationError`. A target
    outside the subschema positions — which ``root``'s own ``check_schema`` does not cover — is
    checked once as a draft 2020-12 schema, so the walk stays linear in the schema's size and
    its ref count.
    """
    checked = {id(node) for node in (positioned if positioned is not None else _subschemas(root))}
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
    """The node the ``#/…`` pointer ``ref`` names in ``root``; a missing node raises :class:`SchemaValidationError`."""
    node: Any = root
    for raw in unquote(ref[2:]).split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and token in node:
            node = node[token]
        elif isinstance(node, list) and token.isdecimal() and int(token) < len(node):
            node = node[int(token)]
        else:
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


# Draft 2020-12 applicators whose subschemas apply to the SAME instance as their parent: a ref
# cycle through only these (and ``$ref`` itself) never reaches a smaller part of the document.
# Applicators into child instances (``properties``, ``items``, ``contains`` …) end a cycle.
_IN_PLACE_SINGLE = ("not", "if", "then", "else")
_IN_PLACE_LISTS = ("allOf", "anyOf", "oneOf")
_IN_PLACE_MAPS = ("dependentSchemas",)


def _in_place_edges(
    root: dict[str, Any], node: dict[str, Any], anchors: dict[str, dict[str, Any]]
) -> Iterator[tuple[dict[str, Any], str | None]]:
    """Yield ``(child, ref)`` for every object subschema ``node`` applies to its own instance.

    ``ref`` is the ``$ref`` string for a ref edge and ``None`` for an applicator member; boolean
    subschemas carry no further edges and are not yielded.
    """
    children: list[tuple[Any, str | None]] = [(node.get(key), None) for key in _IN_PLACE_SINGLE]
    children += [(member, None) for key in _IN_PLACE_LISTS for member in node.get(key) or []]
    children += [(member, None) for key in _IN_PLACE_MAPS for member in (node.get(key) or {}).values()]
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#"):
        if ref == "#":
            children.append((root, ref))
        elif ref.startswith("#/"):
            children.append((_resolve_pointer(root, ref), ref))
        else:
            children.append((anchors.get(ref[1:]), ref))
    for child, edge_ref in children:
        if isinstance(child, dict):
            yield child, edge_ref


def _refuse_in_place_cycles(
    root: dict[str, Any], nodes: list[dict[str, Any]], anchors: dict[str, dict[str, Any]]
) -> None:
    """Refuse a ref cycle among same-instance edges, whether or not validation reaches it.

    Validating an instance against a node on such a cycle returns to that node for the SAME
    instance, so it never ends; a cycle no ref reaches is refused all the same.

    A recursive DATA schema (``{"child": {"$ref": "#"}}`` under ``properties``) is not a cycle
    here: the edge into ``properties`` moves to a child instance, so each turn validates a
    smaller part of the document.
    """
    state: dict[int, int] = {}  # 1 = on the current path, 2 = fully explored
    for start in nodes:
        if id(start) in state:
            continue
        state[id(start)] = 1
        stack: list[tuple[dict[str, Any], Iterator[tuple[dict[str, Any], str | None]], str | None]] = [
            (start, iter(_in_place_edges(root, start, anchors)), None)
        ]
        while stack:
            node, edges, _entered_by = stack[-1]
            edge = next(edges, None)
            if edge is None:
                state[id(node)] = 2
                stack.pop()
                continue
            child, ref = edge
            if state.get(id(child)) == 1:
                closing = ref or next((entered for _n, _e, entered in reversed(stack) if entered), None)
                raise SchemaValidationError(
                    f"$ref {closing!r} forms a cycle of references that never descends into the document"
                )
            if id(child) not in state:
                state[id(child)] = 1
                stack.append((child, iter(_in_place_edges(root, child, anchors)), ref))


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


def _iter_refs(node: Any):
    """Yield the ``$ref`` string of every node of the schema document's :func:`_schema_nodes`."""
    if not isinstance(node, dict):
        return
    for schema_node in _schema_nodes(node):
        ref = schema_node.get("$ref")
        if isinstance(ref, str):
            yield ref


def _uses_key(node: Any, key: str) -> bool:
    """Whether ``key`` appears as a dict key anywhere in the schema document."""
    if isinstance(node, dict):
        return key in node or any(_uses_key(v, key) for v in node.values())
    if isinstance(node, list):
        return any(_uses_key(item, key) for item in node)
    return False


def _anchor_targets(positioned: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Each ``$anchor`` / ``$dynamicAnchor`` name declared by the ``positioned`` subschemas, to its subschema."""
    return {anchor.name: node for node in positioned for anchor in DRAFT202012.anchors_in(node)}


def _anchor_exists(node: Any, anchor: str) -> bool:
    """Whether a subschema of ``node`` declares ``anchor`` as a ``$anchor`` or ``$dynamicAnchor``.

    Only :func:`_subschemas` positions: the ``referencing`` library registers an anchor from
    there alone, so an anchor sitting only inside a pointer target answers no ``#name`` ref.
    """
    return anchor in _anchor_targets(list(_subschemas(node)))


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
    except referencing.exceptions.Unresolvable as exc:
        raise ValueValidationError(f"{prefix}the state schema carries an unresolvable $ref: {exc}") from exc


def _document_reasons(validator: Draft202012Validator, doc: dict[str, Any]) -> list[ValidationReason]:
    """EVERY way ``doc`` fails ``validator``'s effective schema, ordered by JSON path.

    The read-only sibling of :func:`_validate_document`: where the write path raises on the first
    failure, this collects them all through ``validator.iter_errors`` so a caller reports the whole
    misfit at once. Each reason carries the offending node's ``json_path`` and the validator's
    message; an empty list means the document fits. An unresolvable ``$ref`` surfaced during
    iteration is raised loudly as a :class:`ValueValidationError`, never swallowed.
    """
    try:
        errors = list(validator.iter_errors(doc))
    except referencing.exceptions.Unresolvable as exc:
        raise ValueValidationError(f"the state schema carries an unresolvable $ref: {exc}") from exc
    errors.sort(key=lambda error: (error.json_path, error.message))
    return [ValidationReason(path=error.json_path, message=error.message) for error in errors]


class RecordValidator:
    """Validates record documents against one schema, as a write to a declared state would.

    For a caller with no store bound (a test, a check before a deploy): give it the schema a
    record is validated WHOLE against — a declared state's effective schema (the base schema
    with its attachments composed in) — and call :meth:`validate` per document. The schema is
    checked once at construction by the same rules a declaration is (object-rooted, at least
    one property, a valid draft 2020-12 schema, no ``$schema`` naming another dialect, only
    resolvable local ``$ref``, no ``$dynamicRef``, no ``$id`` below the root, unique ``$anchor`` / ``$dynamicAnchor``
    names, no cycle of references that never descends into the document), and refused loudly with
    :class:`~tai42_contract.states.errors.SchemaValidationError`; the compiled validator is
    then reused for every document. A by-id schema is resolved by the caller first.
    """

    def __init__(self, schema: dict[str, Any]) -> None:
        """Check ``schema`` by the declaration rules, then compile its draft 2020-12 validator once."""
        _validate_schema(schema)
        self._validator = Draft202012Validator(schema)

    def validate(self, data: Any) -> RecordValidation:
        """EVERY way ``data`` misfits the schema, ordered by JSON path; ``fits`` when there is none.

        A non-object ``data`` raises :class:`~tai42_contract.states.errors.ValueValidationError`,
        as does an unresolvable ``$ref`` met while validating.
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
    "_anchor_exists",
    "_anchor_targets",
    "_canonical",
    "_check_pointer_target",
    "_document_reasons",
    "_is_narrowing",
    "_iter_refs",
    "_refuse_in_place_cycles",
    "_resolve_pointer",
    "_schema_nodes",
    "_subschemas",
    "_uses_key",
    "_validate_document",
    "_validate_one_document",
    "_validate_refs",
    "_validate_schema",
]
