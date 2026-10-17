"""The one answer check every interactions door reaches.

``check_answer(question, answer)`` validates an answer against a
:class:`~tai42_contract.interactions.QuestionFormat` (the answer format plus its payload),
raising :class:`~tai42_contract.interactions.AnswerMismatchError` on a mismatch and returning
``None`` when the answer conforms. The authenticated answer door, the external/typed callback
door, a resumed run's ``visit`` and plugins all call this through the
``tai42_app.interactions.check_answer`` facet, so every surface applies identical rules per
format.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import jsonschema
from jsonschema.exceptions import best_match
from jsonschema.validators import validator_for
from tai42_contract.interactions import AnswerFormat, AnswerMismatchError, QuestionFormat
from tai42_kit.interactions.form_vocabulary import hidden_fields

from tai42_skeleton.interactions.form_shapes import FORM_FORMAT_CHECKER, scan_form_shapes
from tai42_skeleton.interactions.schema_rules import answer_validator


def _schema_error_field(exc: Exception) -> str | None:
    """The failing ANSWER field's dotted path for a field-located ``jsonschema.ValidationError``, or ``None``.

    The path is like ``count`` or ``a.b``; ``None`` when the fault has no answer-field location.
    Only ``jsonschema.ValidationError`` locates a fault in the answer: its ``.json_path``
    (``$``-rooted, e.g. ``$.count``) names the field. ``SchemaError`` also carries a
    ``.json_path``, but it points INTO the stored schema (e.g. ``properties.x.type``) — a
    location no answering human owns — so it is never surfaced; a malformed schema, a root-level
    ValidationError with ``json_path == "$"``, and ``RecursionError`` all yield ``None``.
    """
    json_path = exc.json_path if isinstance(exc, jsonschema.ValidationError) else None
    if isinstance(json_path, str) and json_path not in ("", "$"):
        # Drop the ``$`` root and a leading ``.`` so a top-level field reads as
        # ``count`` rather than ``$.count``; a nested path keeps its dotted shape.
        return json_path[1:].removeprefix(".")
    return None


def _schema_error_message(exc: Exception) -> str:
    """The mismatch message for a schema failure, naming the failing ANSWER field when the error locates one.

    Uses ``_schema_error_field`` so a human on any surface can tell WHICH field failed; a
    pathless fault falls back to the bare message.
    """
    message = getattr(exc, "message", None) or str(exc)
    field = _schema_error_field(exc)
    if field is not None:
        return f"answer does not match schema at {field}: {message}"
    return f"answer does not match schema: {message}"


# Failures of validating/parsing untrusted input convert to a loud mismatch; any exception
# outside these sets is a server bug and propagates. RecursionError covers recursive schemas /
# deeply-nested answers blowing up the validator.
_SCHEMA_VALIDATION_ERRORS = (jsonschema.ValidationError, jsonschema.SchemaError, RecursionError)


def schema_mismatch(answer: Any, schema: dict) -> tuple[str, str | None] | None:
    """Validate ``answer`` against ``schema``; return ``(message, field)`` on a validation failure, else ``None``.

    ``message`` is the mismatch text, ``field`` the failing answer field's dotted path (``None``
    for a root-level or otherwise non-locatable fault). Returns ``None`` when the answer conforms.
    """
    try:
        # ``jsonschema.validate``'s own steps (the class the schema's ``$schema`` names, its
        # meta-check, the best-matching fault), with references resolved through
        # ``SCHEMA_REFS``, which fetches no schema.
        validator = validator_for(schema, default=jsonschema.Draft202012Validator)
        validator.check_schema(schema)
        error = best_match(answer_validator(schema, validator, format_checker=FORM_FORMAT_CHECKER).iter_errors(answer))
        if error is not None:
            raise error
    except _SCHEMA_VALIDATION_ERRORS as exc:
        return _schema_error_message(exc), _schema_error_field(exc)
    return None


def _check_text(_question: QuestionFormat, answer: Any) -> Any:
    """A TEXT answer must be a string."""
    if not isinstance(answer, str):
        raise AnswerMismatchError("answer must be a string")
    return answer


def _check_confirm(_question: QuestionFormat, answer: Any) -> Any:
    """A CONFIRM answer must be a boolean."""
    if not isinstance(answer, bool):
        raise AnswerMismatchError("answer must be a boolean")
    return answer


def _check_select(question: QuestionFormat, answer: Any) -> Any:
    """A SELECT answer must be one of the question's offered options."""
    options = (question.format_payload or {}).get("options", [])
    if answer not in options:
        raise AnswerMismatchError(f"answer must be one of {options}")
    return answer


def _check_form(question: QuestionFormat, answer: Any) -> Any:
    """Validate a FORM answer through the one form-shape check; RETURN the answer the consumer receives.

    The returned object is the submitted answer with every field HIDDEN by its
    ``visibleWhen`` predicate (evaluated on the submitted values) REMOVED — the documented
    meaning of a conditional field: a hidden field is not required, not validated, and a
    value sent for it is dropped rather than faulted. The shapes (the effective schema with
    per-send option lists and reaction-fed choices typed only, then the date constraints) are
    judged by :func:`~tai42_skeleton.interactions.form_shapes.scan_form_shapes`: the first failing
    field is refused with its plain sentence and its name, the same words every surface shows
    inside the form; a fault no declared field owns (on the answer object itself, or under a key
    only ``additionalProperties``/``patternProperties`` govern) is refused with the schema's message
    and its location.
    """
    if not isinstance(answer, dict):
        raise AnswerMismatchError("answer must be an object")
    payload = question.format_payload or {}
    schema = payload.get("schema")
    if not isinstance(schema, dict):
        raise AnswerMismatchError("question schema is invalid: missing or non-object schema")
    try:
        # The scan evaluates every ``visibleWhen`` on the SUBMITTED values, exactly as the
        # returned answer drops the hidden fields below.
        scan = scan_form_shapes(question, answer)
    except ValueError as exc:
        raise AnswerMismatchError(str(exc)) from exc
    if scan.errors:
        first = scan.errors[0]
        raise AnswerMismatchError(first.message, field=first.field)
    if scan.unowned_faults:
        fault = scan.unowned_faults[0]
        raise AnswerMismatchError(_schema_error_message(fault), field=_schema_error_field(fault))
    properties = schema.get("properties")
    hidden = hidden_fields(properties, answer) if isinstance(properties, dict) else set()
    return {name: value for name, value in answer.items() if name not in hidden}


def _check_schema_only(question: QuestionFormat, answer: Any) -> Any:
    """A FREE or EXTERNAL answer is any JSON value, checked ONLY against a given ``schema``.

    Verbatim when the question declared no schema; otherwise the answer must conform to it.
    """
    schema = (question.format_payload or {}).get("schema")
    if schema is None:
        return answer
    mismatch = schema_mismatch(answer, schema)
    if mismatch is not None:
        message, field = mismatch
        raise AnswerMismatchError(message, field=field)
    return answer


# Per-format answer checks, keyed by ``AnswerFormat`` — one contract each. Each RETURNS the
# answer the consumer receives (unchanged for every format but FORM, which drops the fields
# hidden by their conditional predicate).
_ANSWER_CHECKS: dict[AnswerFormat, Callable[[QuestionFormat, Any], Any]] = {
    AnswerFormat.TEXT: _check_text,
    AnswerFormat.CONFIRM: _check_confirm,
    AnswerFormat.SELECT: _check_select,
    AnswerFormat.FORM: _check_form,
    AnswerFormat.FREE: _check_schema_only,
    AnswerFormat.EXTERNAL: _check_schema_only,
}


def check_answer(question: QuestionFormat, answer: Any) -> Any:
    """Validate ``answer`` against ``question``; raise ``AnswerMismatchError`` on a mismatch.

    RETURNS the answer the consumer receives when it conforms — identical to the input for
    every format but FORM, which returns the answer with conditionally-hidden fields removed
    (the one place this cross-cutting rule lives, so every answer door records the same
    effective answer). An answer format with no check registered is a server bug, never a
    client error.
    """
    check = _ANSWER_CHECKS.get(question.answer_format)
    if check is None:
        raise RuntimeError(f"unhandled answer_format: {question.answer_format}")
    return check(question, answer)
