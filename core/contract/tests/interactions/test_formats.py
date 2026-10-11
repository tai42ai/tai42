"""The answer-format vocabulary: the formats a callback link completes alone."""

from __future__ import annotations

from tai42_contract.interactions import CALLBACK_LINK_FORMATS, AnswerFormat


def test_the_callback_link_formats_are_confirm_and_external() -> None:
    assert frozenset({AnswerFormat.CONFIRM, AnswerFormat.EXTERNAL}) == CALLBACK_LINK_FORMATS


def test_a_plain_string_format_reads_membership_without_a_cast() -> None:
    assert "confirm" in CALLBACK_LINK_FORMATS
    assert "external" in CALLBACK_LINK_FORMATS
    assert "text" not in CALLBACK_LINK_FORMATS
    assert "form" not in CALLBACK_LINK_FORMATS
