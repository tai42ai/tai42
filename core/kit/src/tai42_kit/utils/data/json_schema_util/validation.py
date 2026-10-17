"""Faithful draft-2020-12 JSON-Schema validation.

Validates a Python value against a JSON Schema with the ``jsonschema`` library:
every keyword is enforced. The schema is checked first against the JSON Schema
rules (:func:`~tai42_kit.utils.data.json_schema_util.schema_rules.check_schema_rules`:
its meta-schema, and every reference resolving inside it), so a malformed schema
fails loudly instead of silently mis-validating. References resolve through
:data:`SCHEMA_REFS`, so no schema is ever fetched.
"""

from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

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
    (e.g. ``$.items[0].name``) and ``offending_value`` is the value found there.
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


def validate_against_json_schema(instance: Any, schema: dict[str, Any]) -> None:
    """Validate ``instance`` against ``schema`` (JSON Schema draft 2020-12).

    Returns ``None`` when the instance conforms. Otherwise raises loudly — never
    silently passing or degrading:

    * ``InvalidJsonSchemaError`` if ``schema`` itself breaks the JSON Schema rules
      (:func:`check_json_schema`, checked first, so a broken schema can never
      mis-validate an instance into a false pass), and
    * ``JsonSchemaValidationError`` for a mismatch, carrying the JSON-path and
      offending value of the reported failure.

    Every JSON-Schema keyword is enforced (the faithful path that the
    signature-bound converters cannot fully express).
    """
    check_json_schema(schema)

    error = best_match(Draft202012Validator(schema, registry=SCHEMA_REFS).iter_errors(instance))
    if error is not None:
        raise JsonSchemaValidationError(
            f"value does not match schema at {error.json_path}: {error.message}",
            json_path=error.json_path,
            offending_value=error.instance,
        ) from error
