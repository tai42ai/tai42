"""Faithful draft-2020-12 JSON-Schema validation.

Validates a Python value against a JSON Schema with the ``jsonschema`` library:
every keyword is enforced. The schema is meta-schema-checked first, so a
malformed schema fails loudly instead of silently mis-validating.
"""

from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, best_match


class JsonSchemaError(Exception):
    """Base for ``validate_against_json_schema`` failures."""


class InvalidJsonSchemaError(JsonSchemaError):
    """The supplied schema is not a valid draft-2020-12 JSON Schema.

    Raised by the meta-schema check before any instance is examined, so a
    malformed schema fails loudly instead of silently mis-validating.
    """


class JsonSchemaValidationError(JsonSchemaError):
    """The instance does not conform to the (valid) schema.

    ``json_path`` is the ``jsonschema`` JSONPath of the reported offending node
    (e.g. ``$.items[0].name``) and ``offending_value`` is the value found there.
    """

    def __init__(self, message: str, *, json_path: str, offending_value: Any) -> None:
        self.json_path = json_path
        self.offending_value = offending_value
        super().__init__(message)


def check_json_schema(schema: dict[str, Any]) -> None:
    """Verify ``schema`` is a valid draft-2020-12 JSON Schema.

    Returns ``None`` when the schema is well-formed; raises
    ``InvalidJsonSchemaError`` loudly when it is not. Use this to reject a
    malformed schema at configuration time, before any instance exists to validate.
    """
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise InvalidJsonSchemaError(f"invalid JSON schema: {exc.message}") from exc


def validate_against_json_schema(instance: Any, schema: dict[str, Any]) -> None:
    """Validate ``instance`` against ``schema`` (JSON Schema draft 2020-12).

    Returns ``None`` when the instance conforms. Otherwise raises loudly — never
    silently passing or degrading:

    * ``InvalidJsonSchemaError`` if ``schema`` itself fails the draft-2020-12
      meta-schema (checked first, so a broken schema can never mis-validate an
      instance into a false pass), and
    * ``JsonSchemaValidationError`` for a mismatch, carrying the JSON-path and
      offending value of the reported failure.

    Every JSON-Schema keyword is enforced (the faithful path that the
    signature-bound converters cannot fully express).
    """
    check_json_schema(schema)

    validator = Draft202012Validator(schema)
    error = best_match(validator.iter_errors(instance))
    if error is not None:
        raise JsonSchemaValidationError(
            f"value does not match schema at {error.json_path}: {error.message}",
            json_path=error.json_path,
            offending_value=error.instance,
        ) from error
