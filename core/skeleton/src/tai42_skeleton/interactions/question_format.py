"""The answer-check view of an ask, built from the ask door's own format mapping.

``question_format(answer_format, ...)`` answers "what would an ask with these arguments check its
answer against": the same format parse, the same per-format argument rule and the same answer
payload the live ``ask`` door stores, minus EXTERNAL's delivery keys. A caller holding only a
declared format (an engine node, a plugin) checks an answer through
``tai42_app.interactions.check_answer`` with it and agrees with the door by construction.
"""

from __future__ import annotations

from typing import Any, TypedDict

from pydantic import TypeAdapter
from tai42_contract.interactions import QuestionFormat, check_format_payload

from tai42_skeleton.interactions.ask.format_args import check_format_arguments, parse_answer_format
from tai42_skeleton.interactions.ask.payload import answer_payload

__all__ = ["question_format"]


class QuestionArguments(TypedDict):
    """The format arguments of an ask, typed as the ``ask`` tool declares them."""

    options: list[str] | None
    schema: dict[str, Any] | None
    data: dict[str, Any] | None
    pages: list[dict[str, Any]] | None
    reactions: dict[str, Any] | None


_ARGUMENTS: TypeAdapter[QuestionArguments] = TypeAdapter(QuestionArguments)


def question_format(
    answer_format: str,
    *,
    options: list[str] | None = None,
    schema: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    pages: list[dict[str, Any]] | None = None,
    reactions: dict[str, Any] | None = None,
) -> QuestionFormat:
    """The :class:`QuestionFormat` an ask with these arguments checks its answer against.

    Raises ``ValueError`` (a pydantic ``ValidationError`` for a value of the wrong shape) for an
    unknown format, an argument not valid for the format, or a payload the format refuses.
    """
    fmt = parse_answer_format(answer_format)
    arguments = _ARGUMENTS.validate_python(
        {"options": options, "schema": schema, "data": data, "pages": pages, "reactions": reactions}
    )
    check_format_arguments(
        fmt,
        options=arguments["options"],
        data=arguments["data"],
        pages=arguments["pages"],
        reactions=arguments["reactions"],
    )
    payload = answer_payload(
        fmt,
        arguments["options"],
        arguments["schema"],
        data=arguments["data"],
        pages=arguments["pages"],
        reactions=arguments["reactions"],
    )
    check_format_payload(fmt, payload, durable=False)
    return QuestionFormat(answer_format=fmt, format_payload=payload)
