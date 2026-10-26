"""Faithful draft-2020-12 JSON-Schema validation.

Validates a Python value against a JSON Schema with the ``jsonschema`` library:
every keyword is enforced. The schema is checked first against the JSON Schema
rules (:func:`~tai42_kit.utils.data.json_schema_util.schema_rules.check_schema_rules`:
its meta-schema, and every reference resolving inside it), so a malformed schema
fails loudly instead of silently mis-validating. References resolve through
:data:`SCHEMA_REFS`, so no schema is ever fetched. A schema that JSON text can spell
exactly is checked and compiled on its first use, and the validator is kept (for the 256
most recently used schemas) for every later instance; any other schema is checked and
compiled on each call.
"""

import json
from functools import lru_cache
from typing import Any, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError, best_match

from tai42_kit.utils.data.json_schema_util.schema_rules import SCHEMA_REFS, check_schema_rules


class JsonSchemaError(Exception):
    """Base for ``validate_against_json_schema`` failures."""


class InvalidJsonSchemaError(JsonSchemaError):
    """The supplied schema does not hold the draft-2020-12 JSON Schema rules.

    Raised by the rules check before any instance is examined, so a
    malformed schema fails loudly instead of silently mis-validating.
    """


class JsonSchemaValidationError(JsonSchemaError):
    """The instance does not conform to the (valid) schema.

    ``json_path`` is the ``jsonschema`` JSONPath of the reported offending node
    (e.g. ``$.items[0].name``) and ``offending_value`` is the value found there, or
    ``None`` when the check ran with ``redact_value=True``.
    """

    def __init__(self, message: str, *, json_path: str, offending_value: Any) -> None:
        """Build the error with the offending node's ``json_path`` and its ``offending_value``."""
        self.json_path = json_path
        self.offending_value = offending_value
        super().__init__(message)


def check_json_schema(schema: dict[str, Any]) -> None:
    """Verify ``schema`` holds the JSON Schema rules (draft 2020-12).

    Returns ``None`` when it does; raises ``InvalidJsonSchemaError`` loudly, naming the
    top-level property the fault sits under, when it breaks the draft-2020-12 meta-schema,
    carries a number that is not finite, holds itself, takes a JSON Schema meta-schema's identifier
    as its root ``$id``, is not one resource (an ``$id`` below the root, an anchor name declared
    twice, or, under a root ``$id``, a ``$dynamicAnchor`` below the root whose name the meta-schema
    subschema a reference reaches binds dynamically, or the reference names as that meta-schema's
    ``$dynamicAnchor``), or holds a ``$ref``/``$dynamicRef`` that does not
    resolve inside it (:data:`SCHEMA_REFS`; no schema is fetched), indexes an array with a pointer
    segment that is no RFC 6901 index, points at a value that is not a schema, or forms a cycle of
    references that never descends into the instance
    (:func:`~tai42_kit.utils.data.json_schema_util.schema_rules.check_schema_rules`). Every
    subschema an instance check could evaluate is checked, whether or not a given instance reaches
    it. Use this to reject a malformed schema at configuration time, before any instance exists to
    validate.
    """
    try:
        check_schema_rules(schema, Draft202012Validator, subject="JSON schema")
    except ValueError as exc:
        raise InvalidJsonSchemaError(str(exc)) from exc


def _scalar_text(value: Any) -> str | None:
    if isinstance(value, str):
        return repr(value)
    if value is None or isinstance(value, (bool, int, float)):
        return json.dumps(value)
    return None


def _rule_shown(value: Any) -> str:
    """The schema rule's own value for a value-free refusal: a scalar or a list of scalars, else nothing."""
    text = _scalar_text(value)
    if text is not None:
        return f": {text}"
    if isinstance(value, list):
        items = [_scalar_text(item) for item in cast("list[Any]", value)]
        if items and all(item is not None for item in items):
            return ": " + ", ".join(cast("list[str]", items))
    return ""


def _redacted_refusal(error: ValidationError) -> JsonSchemaValidationError:
    if error.validator is None:
        message = f"value does not match schema at {error.json_path}: refused by the schema (the value is not shown)"
    else:
        message = (
            f"value does not match schema at {error.json_path}: breaks the schema's "
            f"'{error.validator}' rule{_rule_shown(error.validator_value)} (the value is not shown)"
        )
    return JsonSchemaValidationError(message, json_path=error.json_path, offending_value=None)


@lru_cache(maxsize=256)
def _compiled_from_text(schema_text: str) -> Draft202012Validator:
    # Keyed by the schema's canonical JSON text; the validator is built over its own parsed copy,
    # so no caller's later change to its dict reaches it. A refused schema raises and is not kept.
    schema = json.loads(schema_text)
    check_json_schema(schema)
    return Draft202012Validator(schema, registry=SCHEMA_REFS)


def _compiled(schema: dict[str, Any]) -> Draft202012Validator:
    """The checked validator for ``schema``: the kept one when JSON text spells ``schema`` exactly.

    Any other schema the rules admit (a ``Decimal`` bound, say) has no exact JSON text to key it,
    so it is checked and compiled for this call alone.
    """
    try:
        text = json.dumps(schema, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        text = None
    if text is not None and json.loads(text) == schema:
        return _compiled_from_text(text)
    check_json_schema(schema)
    return Draft202012Validator(schema, registry=SCHEMA_REFS)


def validate_against_json_schema(instance: Any, schema: dict[str, Any], *, redact_value: bool = False) -> None:
    """Validate ``instance`` against ``schema`` (JSON Schema draft 2020-12).

    Returns ``None`` when the instance conforms. Otherwise raises loudly — never
    silently passing or degrading:

    * ``InvalidJsonSchemaError`` if ``schema`` itself breaks the JSON Schema rules
      (:func:`check_json_schema`, checked first, so a broken schema can never
      mis-validate an instance into a false pass), and
    * ``JsonSchemaValidationError`` for a mismatch, carrying the JSON-path and
      offending value of the reported failure.

    With ``redact_value=True`` the mismatch is reported by its JSON path and the schema
    rule it breaks, never by the instance: the message holds no part of the value,
    ``offending_value`` is ``None`` and nothing is chained.

    Every JSON-Schema keyword is enforced (the faithful path that the
    signature-bound converters cannot fully express). The rules check and the compiled
    validator are kept per schema, so a schema validated again is not checked again.
    """
    error = best_match(_compiled(schema).iter_errors(instance))
    if error is None:
        return
    if redact_value:
        raise _redacted_refusal(error)
    raise JsonSchemaValidationError(
        f"value does not match schema at {error.json_path}: {error.message}",
        json_path=error.json_path,
        offending_value=error.instance,
    ) from error
