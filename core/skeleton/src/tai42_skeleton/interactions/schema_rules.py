"""The JSON Schema rules check an answer schema passes before a question carrying it is asked.

An answer door checks every answer against the question's schema with ``jsonschema``, which first
checks the schema itself against its dialect's meta-schema. A schema the answer check could not
use would refuse every answer, so it is refused when the question is asked instead, naming the
property it breaks under, by the same rules check
(:func:`~tai42_kit.utils.data.json_schema_util.check_schema_rules`) with the same validator
class the answer door uses.

Every validator the answer checks build resolves references through
:data:`~tai42_kit.utils.data.json_schema_util.SCHEMA_REFS`, which holds no schema and fetches
none: a reference resolves inside the schema itself (``#``, a ``#/…`` pointer, an anchor, an
absolute reference under the root's ``$id``) or into a JSON Schema meta-schema, and a pointer
indexes an array only with an RFC 6901 index (``0`` or digits without a leading zero). The schema is
one resource whose root ``$id`` names no meta-schema: no ``$id`` below the root, unique anchor names,
and, under a root ``$id``, no ``$dynamicAnchor`` below the root whose name the meta-schema subschema a
reference reaches binds dynamically, or the reference names as that meta-schema's ``$dynamicAnchor``.
"""

from __future__ import annotations

from typing import Any

import jsonschema
from jsonschema import FormatChecker
from jsonschema.protocols import Validator
from tai42_kit.utils.data.json_schema_util import SCHEMA_REFS, check_schema_rules, schema_validator_class


def answer_validator(schema: Any, validator: type[Validator], *, format_checker: FormatChecker) -> Validator:
    """A ``validator`` for ``schema`` whose references resolve through :data:`SCHEMA_REFS`."""
    return validator(schema, format_checker=format_checker, registry=SCHEMA_REFS)


def answer_schema_validator(schema: dict[str, Any]) -> type[Validator]:
    """The validator class a ``free``/``external`` answer check judges ``schema`` with.

    The one the schema's own ``$schema`` names, Draft 2020-12 when it names none. Raises
    ``ValueError`` when the ``$schema`` is not a URI string, which names no dialect at all.
    """
    return schema_validator_class(schema, jsonschema.Draft202012Validator, subject="answer schema")


def check_answer_schema_rules(schema: dict[str, Any]) -> None:
    """Refuse a ``free``/``external`` answer schema whose answers could never be checked.

    The answer door judges the schema with the class :func:`answer_schema_validator` selects; the
    same class judges the schema here (:func:`~tai42_kit.utils.data.json_schema_util.check_schema_rules`).
    """
    check_schema_rules(schema, answer_schema_validator(schema), subject="answer schema")
