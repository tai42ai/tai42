"""Static validity of a state's JSON-Schema and its stored document.

The pure draft 2020-12 validators the template document parser and the states service
both lean on: an object-rooted schema check, ``$ref`` resolvability, full-document
validation against an effective schema, and the conservative narrowing comparison two
schemas are compared with. Depends only on the standard library and
``jsonschema``/``referencing``.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import unquote

import jsonschema
import referencing.exceptions
from jsonschema import Draft202012Validator
from tai42_contract.states.errors import SchemaValidationError, ValueValidationError


def _canonical(value: Any) -> str:
    """A byte-stable canonical form for comparing two field schemas for equality."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _validate_schema(schema: Any) -> None:
    """Accept any VALID JSON Schema (draft 2020-12) that is object-rooted with ≥1
    property; refuse everything else loudly. Nesting to any depth is the point — the
    record document is validated WHOLE against this schema on every write."""
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
    """Refuse ``$ref``s ``check_schema`` cannot vouch for (SYNTAX-only): a remote ref, a
    dangling local one, and ``$dynamicRef`` are all declare-time refusals. Local support:
    ``#`` (root), ``#/json/pointer`` (resolved against the document), and ``#anchor``."""
    if _uses_key(schema, "$dynamicRef"):
        raise SchemaValidationError("$dynamicRef is not supported — use $ref with root-level $defs")
    for ref in _iter_refs(schema):
        if not ref.startswith("#"):
            raise SchemaValidationError(f"remote $ref {ref!r} is not supported — inline the schema or use $defs")
        if ref == "#":
            continue
        if ref.startswith("#/"):
            node: Any = schema
            for raw in unquote(ref[2:]).split("/"):
                token = raw.replace("~1", "/").replace("~0", "~")
                if isinstance(node, dict) and token in node:
                    node = node[token]
                elif isinstance(node, list) and token.isdigit() and int(token) < len(node):
                    node = node[int(token)]
                else:
                    raise SchemaValidationError(f"$ref {ref!r} does not resolve — {token!r} is missing")
            continue
        anchor = ref[1:]
        if not _anchor_exists(schema, anchor):
            raise SchemaValidationError(f"$ref {ref!r} does not resolve — no $anchor {anchor!r} in the schema")


def _iter_refs(node: Any):
    """Yield every ``$ref`` string value anywhere in the schema document."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            yield ref
        for value in node.values():
            yield from _iter_refs(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_refs(item)


def _uses_key(node: Any, key: str) -> bool:
    """Whether ``key`` appears as a dict key anywhere in the schema document."""
    if isinstance(node, dict):
        return key in node or any(_uses_key(v, key) for v in node.values())
    if isinstance(node, list):
        return any(_uses_key(item, key) for item in node)
    return False


def _anchor_exists(node: Any, anchor: str) -> bool:
    if isinstance(node, dict):
        if node.get("$anchor") == anchor:
            return True
        return any(_anchor_exists(v, anchor) for v in node.values())
    if isinstance(node, list):
        return any(_anchor_exists(item, anchor) for item in node)
    return False


def _validate_document(schema: dict[str, Any], doc: dict[str, Any]) -> None:
    """Validate the FULL record document against the effective schema; the error names the
    offending JSON path. Loud on the first failure."""
    try:
        Draft202012Validator(schema).validate(doc)
    except jsonschema.ValidationError as exc:
        raise ValueValidationError(f"record invalid under the state schema at {exc.json_path}: {exc.message}") from exc
    except referencing.exceptions.Unresolvable as exc:
        raise ValueValidationError(f"the state schema carries an unresolvable $ref: {exc}") from exc


def _is_narrowing(old_schema: dict[str, Any], new_schema: dict[str, Any]) -> bool:
    """Whether ``new_schema`` removes or changes any top-level property of ``old_schema`` —
    OR changes any ROOT keyword outside ``properties``. Deliberately conservative: any
    property-subtree edit or root-keyword edit registers as narrowing."""
    old_props = old_schema.get("properties", {}) if isinstance(old_schema, dict) else {}
    new_props = new_schema.get("properties", {})
    for fname, fschema in old_props.items():
        if fname not in new_props or _canonical(new_props[fname]) != _canonical(fschema):
            return True
    old_root = {k: v for k, v in old_schema.items() if k != "properties"} if isinstance(old_schema, dict) else {}
    new_root = {k: v for k, v in new_schema.items() if k != "properties"}
    return _canonical(old_root) != _canonical(new_root)


__all__ = [
    "_anchor_exists",
    "_canonical",
    "_is_narrowing",
    "_iter_refs",
    "_uses_key",
    "_validate_document",
    "_validate_refs",
    "_validate_schema",
]
