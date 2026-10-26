"""The one form-shape check: every FORM field whose value breaks its declared shape, and its words.

:func:`form_shape_errors` (the ``tai42_app.interactions.form_shape_errors`` facet) judges a form's
values against the SAME effective schema the answer door applies — per-send option lists stamped,
reaction-fed choices typed only, fields hidden by their ``visibleWhen`` dropped — plus the platform
date constraints, and returns one :class:`~tai42_contract.interactions.FormShapeError` per failing
field: a typed kind, the bound it names and the platform's plain sentence
(:func:`~tai42_skeleton.interactions.form_shape_words.shape_sentence`, the one table of the
platform's words for a broken shape). Every surface calls it inside the form
before the submit leaves it, and the answer door refuses a FORM answer with its first error, so the
door and every in-form check have one owner of the rules and of the words.

The typed string formats are asserted by :data:`FORM_FORMAT_CHECKER`. python-jsonschema treats
``format`` as a bare annotation unless a ``FormatChecker`` carrying a checker for it is passed, and
its default set needs packages the skeleton does not declare, so the checker carries exactly the
form formats: each value's shape is the platform's one regex (:data:`FORMAT_PATTERNS`, shared with
every surface's client-side check) and a ``date``/``time``/``date-time`` must also name a real
calendar value (by the stdlib). Every other format stays an annotation.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Collection, Mapping
from typing import Any

import jsonschema
from tai42_contract.interactions import AnswerFormat, FormShapeError, FormShapeKind, QuestionFormat
from tai42_kit.interactions.form_vocabulary import FORMAT_PATTERNS, hidden_fields

from tai42_skeleton.interactions.form_answer_rules import date_constraint_errors, effective_answer_schema
from tai42_skeleton.interactions.form_schema import check_form_schema_rules
from tai42_skeleton.interactions.form_shape_words import shape_sentence
from tai42_skeleton.interactions.schema_rules import answer_validator

_FORMAT_RES = {name: re.compile(pattern) for name, pattern in FORMAT_PATTERNS.items()}


def _shape_matches(form_format: str, value: str) -> bool:
    return _FORMAT_RES[form_format].fullmatch(value) is not None


def _is_date(value: object) -> bool:
    """A ``date`` value is ``YYYY-MM-DD`` (zero-padded) naming a real calendar date."""
    if not isinstance(value, str):
        return True
    if not _shape_matches("date", value):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _is_time(value: object) -> bool:
    """A ``time`` value is ``HH:MM`` or ``HH:MM:SS`` naming a real wall-clock time."""
    if not isinstance(value, str):
        return True
    if not _shape_matches("time", value):
        return False
    try:
        datetime.time.fromisoformat(value)
    except ValueError:
        return False
    return True


def _is_date_time(value: object) -> bool:
    """A ``date-time`` value is an RFC 3339 timestamp with a mandatory ``Z``/``±HH:MM`` offset."""
    if not isinstance(value, str):
        return True
    if not _shape_matches("date-time", value):
        return False
    try:
        # RFC 3339 permits a lowercase ``t``/``z``; the only letters a matched value can carry
        # are those two designators, so upper-casing lets ``fromisoformat`` (which wants the
        # canonical forms) validate the calendar without loosening the shape the regex fixed.
        datetime.datetime.fromisoformat(value.upper())
    except ValueError:
        return False
    return True


def _is_email(value: object) -> bool:
    """An ``email`` value is one ``@`` between a non-empty local part and a dotted domain, no whitespace."""
    if not isinstance(value, str):
        return True
    return _shape_matches("email", value)


#: The format checker every answer check passes to jsonschema: exactly the form string formats.
FORM_FORMAT_CHECKER = jsonschema.FormatChecker(formats=())
FORM_FORMAT_CHECKER.checks("date")(_is_date)
FORM_FORMAT_CHECKER.checks("time")(_is_time)
FORM_FORMAT_CHECKER.checks("date-time")(_is_date_time)
FORM_FORMAT_CHECKER.checks("email")(_is_email)


# A jsonschema keyword -> the kind it breaks and the ``params`` key its bound rides under.
_KEYWORD_KINDS: dict[str, tuple[FormShapeKind, str | None]] = {
    "type": (FormShapeKind.TYPE, "type"),
    "pattern": (FormShapeKind.PATTERN, "pattern"),
    "format": (FormShapeKind.FORMAT, "format"),
    "minLength": (FormShapeKind.MIN_LENGTH, "minLength"),
    "maxLength": (FormShapeKind.MAX_LENGTH, "maxLength"),
    "minimum": (FormShapeKind.MINIMUM, "minimum"),
    "maximum": (FormShapeKind.MAXIMUM, "maximum"),
    "minItems": (FormShapeKind.MIN_ITEMS, "minItems"),
    "maxItems": (FormShapeKind.MAX_ITEMS, "maxItems"),
    "enum": (FormShapeKind.ENUM, None),
}


def _error(field: str, kind: FormShapeKind, params: dict[str, Any]) -> FormShapeError:
    return FormShapeError(field=field, kind=kind, params=params, message=shape_sentence(kind, params))


def _keyword_error(field: str, error: jsonschema.ValidationError) -> FormShapeError:
    # The shape error for one jsonschema fault located on ``field`` (or inside it).
    keyword = str(error.validator)
    mapped = _KEYWORD_KINDS.get(keyword)
    if mapped is None:
        return _error(field, FormShapeKind.OTHER, {"keyword": keyword})
    kind, param = mapped
    return _error(field, kind, {param: error.validator_value} if param is not None else {})


class FormShapeScan:
    """One pass of the form-shape check: the per-field errors and the faults no form field owns.

    ``errors`` is one :class:`FormShapeError` per failing field in schema order; ``unowned_faults``
    the jsonschema faults no declared property owns — one located on the answer object itself (an
    unknown property under ``additionalProperties: false``, a root-level keyword) or under an answer
    key the schema governs only through ``additionalProperties``/``patternProperties``. The answer
    door refuses those too.
    """

    def __init__(self, errors: list[FormShapeError], unowned_faults: list[jsonschema.ValidationError]) -> None:
        """Hold the per-field ``errors`` and the ``unowned_faults`` of one scan."""
        self.errors = errors
        self.unowned_faults = unowned_faults


def _form_schema(question: QuestionFormat) -> tuple[dict[str, Any], dict[str, Any]]:
    # The question's payload and its object schema; a non-FORM question or a missing schema is a caller bug.
    if question.answer_format is not AnswerFormat.FORM:
        raise ValueError(f"form_shape_errors needs a FORM question, got {question.answer_format.value}")
    payload = question.format_payload or {}
    schema = payload.get("schema")
    if not isinstance(schema, dict):
        raise ValueError("form_shape_errors needs a FORM question carrying an object schema")  # noqa: TRY004 raised type is intentional (invariant/state/validation taxonomy); TypeError would change behaviour
    return payload, schema


def _first_fault_per_field(
    faults: list[jsonschema.ValidationError], declared: Collection[str], in_scope: list[str]
) -> tuple[dict[str, FormShapeError], list[jsonschema.ValidationError]]:
    # Each in-scope field's FIRST jsonschema fault as its shape error, and the faults no declared
    # property owns (on the answer object itself, or under an undeclared answer key). A declared
    # field outside ``in_scope`` is another step's, so its faults are that step's to report.
    first: dict[str, FormShapeError] = {}
    unowned: list[jsonschema.ValidationError] = []
    for fault in faults:
        path = list(fault.absolute_path)
        field = str(path[0]) if path else None
        if field is None or field not in declared:
            unowned.append(fault)
        elif field not in first and field in in_scope:
            first[field] = _keyword_error(field, fault)
    return first, unowned


def scan_form_shapes(
    question: QuestionFormat,
    values: Mapping[str, Any],
    *,
    fields: Collection[str] | None = None,
    partial: bool = False,
) -> FormShapeScan:
    """Run the form-shape check once and return its per-field errors and its field-less faults.

    See :func:`form_shape_errors` for the arguments. A schema jsonschema cannot use (a malformed
    stored schema) or an answer nested past the recursion limit raises ``ValueError``.
    """
    payload, schema = _form_schema(question)
    properties = schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    hidden = hidden_fields(properties, values)
    visible = {name: value for name, value in values.items() if name not in hidden}
    reactions = payload.get("reactions")
    choices = reactions.get("choices", []) if isinstance(reactions, dict) else []
    effective = effective_answer_schema(schema, payload.get("data"), choices)
    in_scope = [str(name) for name in properties if fields is None or name in fields]
    required = effective.get("required")
    demanded = (
        set()
        if partial or not isinstance(required, list)
        else {str(name) for name in required if name not in hidden and str(name) in in_scope}
    )
    # ``required`` is judged here (one error per missing field, in schema order), so the schema
    # jsonschema walks carries none.
    walked = {key: value for key, value in effective.items() if key != "required"}
    try:
        # The same rules check the form ask ran on the schema, so a schema asked is a schema checked.
        check_form_schema_rules(walked)
    except ValueError as exc:
        raise ValueError(f"question schema is invalid: {exc}") from exc
    try:
        validator = answer_validator(walked, jsonschema.Draft202012Validator, format_checker=FORM_FORMAT_CHECKER)
        faults = list(validator.iter_errors(visible))
    except RecursionError as exc:
        raise ValueError("answer nests too deeply to check") from exc
    first, unowned = _first_fault_per_field(faults, [str(name) for name in properties], in_scope)
    for field, kind, params in date_constraint_errors(schema, visible):
        if field in in_scope and field not in first:
            first[field] = _error(field, kind, params)
    errors: list[FormShapeError] = []
    for name in in_scope:
        if name in demanded and name not in visible:
            errors.append(_error(name, FormShapeKind.REQUIRED, {}))
        elif name in first:
            errors.append(first[name])
    return FormShapeScan(errors, unowned)


def form_shape_errors(
    question: QuestionFormat,
    values: Mapping[str, Any],
    *,
    fields: Collection[str] | None = None,
    partial: bool = False,
) -> list[FormShapeError]:
    """Every FORM field whose value in ``values`` breaks its declared shape, one error per field.

    ``question`` is a FORM :class:`~tai42_contract.interactions.QuestionFormat`. The values are
    judged against the answer door's own effective schema (per-send option lists, reaction-fed
    choices typed only, fields hidden by their ``visibleWhen`` — evaluated on ``values`` — neither
    demanded nor checked) and then the platform date constraints; each failing field yields its
    FIRST broken shape in schema order. ``fields`` restricts the check to the named top-level
    properties (a step of a stepped form; a range END on it still reads its START from ``values``);
    ``partial=True`` demands nothing (no ``required`` error) — absent fields are never checked.
    Pure — no store read. Raises ``ValueError`` for a non-FORM question, a question with no object
    schema, or a schema jsonschema cannot use.
    """
    return scan_form_shapes(question, values, fields=fields, partial=partial).errors
