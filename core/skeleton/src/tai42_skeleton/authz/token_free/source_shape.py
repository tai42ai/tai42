"""The pre-lexical raw-text gates over a jq condition.

Refuse the control characters libjq cannot read faithfully, and refuse the character classes
where this module's lexer and jq's could disagree about where a token begins and ends.
"""

from __future__ import annotations

from .errors import _refusal

# The printable-ASCII band the source-shape gate accepts, both ends inclusive.
_PRINTABLE_ASCII_START = " "
_PRINTABLE_ASCII_END = "~"


def _assert_no_control_characters(condition_text: str) -> None:
    """Refuse the control characters that make libjq read a DIFFERENT source than the raw text.

    Runs BEFORE the compile gate hands the text to libjq. A NUL is the sharp case:
    it terminates the C string libjq lexes, so libjq compiles only the prefix while the
    scan reasons over the whole text — the compile gate would then answer about a program
    that is never the one analyzed.

    The whitespace-formatting characters an unrendered template legitimately carries —
    tab, newline, carriage return — libjq reads faithfully and are left to
    :func:`_assert_source_shape` (after the compile gate), so such a template still reads
    as "does not compile" rather than a character complaint.
    """
    for position, char in enumerate(condition_text):
        if char in "\t\n\r":
            continue
        if char < _PRINTABLE_ASCII_START or char == "\x7f":
            raise _refusal(
                condition_text,
                position,
                f"condition contains the control character {char!r}; remove it",
            )


def _assert_source_shape(condition_text: str) -> None:
    """Assert ``condition_text`` uses the character subset where this module's lexer and jq's cannot disagree.

    The subset is printable ASCII (space through ``~``), on a single line, with no ``#`` anywhere.

    **PRE-LEXICAL, and must stay that way** — reading raw characters only. A gate
    expressed over tokens would inherit the very lexing assumptions it exists to test.

    Outside this subset a disagreement that parses SUCCESSFULLY INTO A DIFFERENT PROGRAM
    certifies a program jq never runs while jq runs one never analyzed: a fail-OPEN.
    Refusing the whole class removes that shape. The control characters libjq cannot read
    faithfully are refused ahead of the compile gate by :func:`_assert_no_control_characters`.
    """
    for position, char in enumerate(condition_text):
        if char == "#":
            raise _refusal(
                condition_text,
                position,
                "condition contains '#', which opens a jq comment whose extent the token-free scan does not "
                "adjudicate; delete the comment (a '#' inside a string literal is refused too, and has to be "
                "rewritten out)",
            )
        if char in "\n\r":
            raise _refusal(
                condition_text,
                position,
                "condition spans more than one line; write it on a single line",
            )
        if char == "\t":
            raise _refusal(
                condition_text,
                position,
                "condition contains a tab; separate tokens with spaces",
            )
        if char > _PRINTABLE_ASCII_END:
            raise _refusal(
                condition_text,
                position,
                f"condition contains the non-ASCII character {char!r}; the token-free scan reads printable ASCII "
                "only, so rewrite the condition without it",
            )
