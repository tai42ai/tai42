"""The per-format argument rule an ask and its answer-check view share.

``options`` are the SELECT answer set (required there) and, for TEXT, an OPTIONAL set of suggested
replies; ``data``/``pages`` enrich a FORM ask only, and a reacting form's ``reaction_tool`` +
``reactions`` triggers are FORM-only too. Every other combination is refused loudly, before any
state is written.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.interactions import AnswerFormat

__all__ = ["check_format_arguments", "parse_answer_format"]


def parse_answer_format(answer_format: str) -> AnswerFormat:
    """The :class:`AnswerFormat` named by ``answer_format``; ``ValueError`` for an unknown one."""
    try:
        return AnswerFormat(answer_format)
    except ValueError as exc:
        raise ValueError(f"unknown answer_format: {answer_format!r}") from exc


def check_format_arguments(
    fmt: AnswerFormat,
    *,
    options: Any,
    data: Any,
    pages: Any,
    reactions: Any,
    reaction_tool: str | None = None,
) -> None:
    """Refuse an argument that is not valid with ``fmt``; ``ValueError`` names the argument and the format."""
    if options is not None and fmt not in (AnswerFormat.SELECT, AnswerFormat.TEXT):
        raise ValueError(f"options are not valid with answer_format {fmt.value!r}")
    if (data is not None or pages is not None) and fmt is not AnswerFormat.FORM:
        raise ValueError(f"data and pages are not valid with answer_format {fmt.value!r}")
    if (reaction_tool is not None or reactions is not None) and fmt is not AnswerFormat.FORM:
        raise ValueError(f"reaction_tool and reactions are not valid with answer_format {fmt.value!r}")
