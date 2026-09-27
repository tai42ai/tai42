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

import datetime
import re
from collections.abc import Callable
from typing import Any

import jsonschema
from tai42_contract.interactions import AnswerFormat, AnswerMismatchError, QuestionFormat

from tai42_skeleton.interactions.form_schema import effective_answer_schema

# The channel-form string formats, asserted by the answer door. python-jsonschema treats
# ``format`` as a bare annotation unless a ``FormatChecker`` carrying a checker for that format
# is passed to ``validate``; the default checker set needs ``rfc3339-validator`` for ``time`` and
# ``date-time`` — a package the skeleton does not declare — so these explicit stdlib checkers
# (shape by regex, calendar by the stdlib) are registered on a checker limited to exactly these
# three formats. Every other format stays an annotation, matching the channel-form subset.
_DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
_TIME_RE = re.compile(r"\A\d{2}:\d{2}(:\d{2})?\Z")
_DATE_TIME_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})\Z")


def _is_date(value: object) -> bool:
    """A ``date`` value is ``YYYY-MM-DD`` (zero-padded) naming a real calendar date."""
    if not isinstance(value, str):
        return True
    if not _DATE_RE.match(value):
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
    if not _TIME_RE.match(value):
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
    if not _DATE_TIME_RE.match(value):
        return False
    try:
        # RFC 3339 permits a lowercase ``t``/``z``; the only letters a matched value can carry
        # are those two designators, so upper-casing lets ``fromisoformat`` (which wants the
        # canonical forms) validate the calendar without loosening the shape the regex fixed.
        datetime.datetime.fromisoformat(value.upper())
    except ValueError:
        return False
    return True


_FORM_FORMAT_CHECKER = jsonschema.FormatChecker(formats=())
_FORM_FORMAT_CHECKER.checks("date")(_is_date)
_FORM_FORMAT_CHECKER.checks("time")(_is_time)
_FORM_FORMAT_CHECKER.checks("date-time")(_is_date_time)


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
        jsonschema.validate(answer, schema, format_checker=_FORM_FORMAT_CHECKER)
    except _SCHEMA_VALIDATION_ERRORS as exc:
        return _schema_error_message(exc), _schema_error_field(exc)
    return None


def _check_text(_question: QuestionFormat, answer: Any) -> None:
    """A TEXT answer must be a string."""
    if not isinstance(answer, str):
        raise AnswerMismatchError("answer must be a string")


def _check_confirm(_question: QuestionFormat, answer: Any) -> None:
    """A CONFIRM answer must be a boolean."""
    if not isinstance(answer, bool):
        raise AnswerMismatchError("answer must be a boolean")


def _check_select(question: QuestionFormat, answer: Any) -> None:
    """A SELECT answer must be one of the question's offered options."""
    options = (question.format_payload or {}).get("options", [])
    if answer not in options:
        raise AnswerMismatchError(f"answer must be one of {options}")


def _check_form(question: QuestionFormat, answer: Any) -> None:
    """A FORM answer must be an object conforming to the question's stored schema."""
    if not isinstance(answer, dict):
        raise AnswerMismatchError("answer must be an object")
    payload = question.format_payload or {}
    schema = payload.get("schema")
    if not isinstance(schema, dict):
        raise AnswerMismatchError("question schema is invalid: missing or non-object schema")
    # Per-send option lists replace a property's enum for THIS send, so the answer is
    # judged against the choices the human was shown (the union of all pages' fields).
    schema = effective_answer_schema(schema, payload.get("data"))
    mismatch = schema_mismatch(answer, schema)
    if mismatch is not None:
        message, field = mismatch
        raise AnswerMismatchError(message, field=field)


def _check_schema_only(question: QuestionFormat, answer: Any) -> None:
    """A FREE or EXTERNAL answer is any JSON value, checked ONLY against a given ``schema``.

    Verbatim when the question declared no schema; otherwise the answer must conform to it.
    """
    schema = (question.format_payload or {}).get("schema")
    if schema is None:
        return
    mismatch = schema_mismatch(answer, schema)
    if mismatch is not None:
        message, field = mismatch
        raise AnswerMismatchError(message, field=field)


# Per-format answer checks, keyed by ``AnswerFormat`` — one contract each.
_ANSWER_CHECKS: dict[AnswerFormat, Callable[[QuestionFormat, Any], None]] = {
    AnswerFormat.TEXT: _check_text,
    AnswerFormat.CONFIRM: _check_confirm,
    AnswerFormat.SELECT: _check_select,
    AnswerFormat.FORM: _check_form,
    AnswerFormat.FREE: _check_schema_only,
    AnswerFormat.EXTERNAL: _check_schema_only,
}


def check_answer(question: QuestionFormat, answer: Any) -> None:
    """Validate ``answer`` against ``question``; raise ``AnswerMismatchError`` on a mismatch.

    Returns ``None`` when the answer conforms. An answer format with no check registered is a
    server bug, never a client error.
    """
    check = _ANSWER_CHECKS.get(question.answer_format)
    if check is None:
        raise RuntimeError(f"unhandled answer_format: {question.answer_format}")
    check(question, answer)
