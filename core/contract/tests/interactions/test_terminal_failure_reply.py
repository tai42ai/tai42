"""Tests for the participant-facing ``reply`` a target's terminal failure may carry.

``RunTerminalFailed`` and ``RunFailed`` carry an optional ``reply`` validated by the one shared
:func:`validate_reply_text`, to the same bound as a conversation route's ``error_reply_text``.
"""

from __future__ import annotations

import inspect
import pickle

import pytest
from pydantic import ValidationError

from tai42_contract.conversations import NON_BLANK_PATTERN as CONVERSATIONS_NON_BLANK_PATTERN
from tai42_contract.conversations import ApiConversationRoute
from tai42_contract.interactions import (
    NON_BLANK_PATTERN,
    REJECTION_NOTICE_MAX_CHARS,
    REPLY_TEXT_MAX_CHARS,
    RunFailed,
    RunTerminalFailed,
    validate_reply_text,
)

_OUTCOME = {"reason": "option_unavailable"}

_INVALID_REPLIES = [
    pytest.param("", id="empty"),
    pytest.param("   \n\t", id="blank"),
    pytest.param("x" * (REPLY_TEXT_MAX_CHARS + 1), id="too-long"),
    pytest.param(5, id="non-string"),
    pytest.param(b"bytes", id="bytes"),
]


def test_the_reply_bound_is_the_route_error_reply_bound():
    assert REPLY_TEXT_MAX_CHARS == 2000
    assert REJECTION_NOTICE_MAX_CHARS == REPLY_TEXT_MAX_CHARS
    # One definition, exported from both packages.
    assert CONVERSATIONS_NON_BLANK_PATTERN is NON_BLANK_PATTERN
    field_schema = ApiConversationRoute.model_json_schema()["properties"]["error_reply_text"]
    text_branch = next(branch for branch in field_schema["anyOf"] if branch.get("type") == "string")
    assert text_branch == {"type": "string", "minLength": 1, "maxLength": 2000, "pattern": r"\S"}


def test_validate_reply_text_returns_the_text_unchanged():
    text = "  Sorry, that option is no longer available.  "
    assert validate_reply_text(text) == text
    assert validate_reply_text("x" * REPLY_TEXT_MAX_CHARS) == "x" * REPLY_TEXT_MAX_CHARS


@pytest.mark.parametrize("value", _INVALID_REPLIES)
def test_validate_reply_text_refuses_an_invalid_value(value: object):
    with pytest.raises(ValidationError):
        validate_reply_text(value)


def test_a_terminal_failure_carries_no_reply_by_default():
    failure = RunTerminalFailed(_OUTCOME)
    assert failure.outcome == _OUTCOME
    assert failure.reply is None


def test_a_terminal_failure_carries_a_valid_reply():
    failure = RunTerminalFailed(_OUTCOME, reply="Sorry, that option is no longer available.")
    assert failure.outcome == _OUTCOME
    assert failure.reply == "Sorry, that option is no longer available."


def test_the_terminal_failure_reply_is_keyword_only():
    parameter = inspect.signature(RunTerminalFailed.__init__).parameters["reply"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is None
    with pytest.raises(TypeError):
        RunTerminalFailed(_OUTCOME, "Sorry.")  # type: ignore[misc]


@pytest.mark.parametrize("value", _INVALID_REPLIES)
def test_a_terminal_failure_refuses_an_invalid_reply_at_the_raise_site(value: object):
    with pytest.raises(ValidationError):
        RunTerminalFailed(_OUTCOME, reply=value)  # type: ignore[arg-type]


def test_a_terminal_failure_round_trips_through_pickle():
    # A worker or a supervisor may carry the exception across a process seam.
    restored = pickle.loads(pickle.dumps(RunTerminalFailed(_OUTCOME, reply="R")))  # noqa: S301 -- the test's own payload
    assert isinstance(restored, RunTerminalFailed)
    assert restored.outcome == _OUTCOME
    assert restored.reply == "R"


def test_a_run_failed_carries_no_reply_by_default():
    assert RunFailed(outcome=_OUTCOME).reply is None


def test_a_run_failed_carries_a_valid_reply_and_round_trips_its_json():
    failed = RunFailed(outcome=_OUTCOME, reply="Sorry, that option is no longer available.")
    assert failed.reply == "Sorry, that option is no longer available."
    assert RunFailed.model_validate_json(failed.model_dump_json()) == failed


@pytest.mark.parametrize("value", _INVALID_REPLIES)
def test_a_run_failed_refuses_an_invalid_reply(value: object):
    with pytest.raises(ValidationError):
        RunFailed(outcome=_OUTCOME, reply=value)  # type: ignore[arg-type]
