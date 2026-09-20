"""JSON-Schema utilities.

Exposes a schema → pydantic model converter, a schema → ``TypedDict`` converter, a faithful
draft-2020-12 validator, and the platform int64 / msgpack integer-range guard.

The converters map a JSON-Schema fragment to a Python annotation and cannot
express every construct (``oneOf`` collapses to a plain ``Union``; ``not`` and
conditional subschemas fall back to ``Any``); ``validate_against_json_schema`` is
the faithful path that enforces every keyword directly.
"""

from tai42_kit.utils.data.json_schema_util.int_bounds import (
    INT64_MAX,
    INT64_MIN,
    MSGPACK_INT_MAX,
    MSGPACK_INT_MIN,
    find_oversized_int,
    inject_int64_bounds,
)
from tai42_kit.utils.data.json_schema_util.pydantic_model import json_schema_to_pydantic_model
from tai42_kit.utils.data.json_schema_util.typed_dict import json_schema_to_typed_dict
from tai42_kit.utils.data.json_schema_util.validation import (
    InvalidJsonSchemaError,
    JsonSchemaValidationError,
    check_json_schema,
    validate_against_json_schema,
)

__all__ = [
    "INT64_MAX",
    "INT64_MIN",
    "MSGPACK_INT_MAX",
    "MSGPACK_INT_MIN",
    "InvalidJsonSchemaError",
    "JsonSchemaValidationError",
    "check_json_schema",
    "find_oversized_int",
    "inject_int64_bounds",
    "json_schema_to_pydantic_model",
    "json_schema_to_typed_dict",
    "validate_against_json_schema",
]
