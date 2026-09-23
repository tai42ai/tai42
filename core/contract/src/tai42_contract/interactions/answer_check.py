"""The answer-check contract: the question format an answer is judged against, and its mismatch error.

``check_answer`` (the skeleton facet ``tai42_app.interactions.check_answer``) validates an
answer against a :class:`QuestionFormat` — the answer format plus its format payload, with no
stored request needed — raising :class:`AnswerMismatchError` when the answer does not fit. The
answer door, the callback door, the resumed-run ``visit`` and plugins all reach the one check
through that facet; the types they share live here.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from tai42_contract.interactions.models.formats import AnswerFormat


class QuestionFormat(BaseModel):
    """The shape an answer is checked against: the answer format and its format payload.

    ``format_payload`` is the same per-format payload the durable question carries (a
    SELECT's ``options``, a FORM's ``schema``/``data``/``pages``, an EXTERNAL's or FREE's
    optional ``schema``); ``None`` when the format carries none. Deliberately NOT the whole
    stored request — a caller with only a declared node format can build one directly.
    """

    answer_format: AnswerFormat
    format_payload: dict[str, Any] | None = None


class AnswerMismatchError(Exception):
    """Raised when an answer does not fit the question's :class:`QuestionFormat`.

    ``field`` is the failing answer field's dotted path when the fault locates to one (a form
    schema mismatch), else ``None`` — a surface can pin the error on the right control with it.
    """

    def __init__(self, message: str, *, field: str | None = None) -> None:
        """Build the error with its ``message`` and the optional failing-field dotted ``path``."""
        super().__init__(message)
        self.field = field
