"""The mid-form reaction error family the contract's ``react`` facet raises."""

from __future__ import annotations

import pytest

import tai42_contract.interactions as interactions
from tai42_contract.interactions import (
    FORM_REACTION_REASON_MAX_CHARS,
    FormReactionClosedError,
    FormReactionError,
    FormReactionHandlerError,
    FormReactionRequestError,
)


def test_the_base_carries_its_reason_as_the_message():
    error = FormReactionError("the form could not refresh")
    assert error.reason == "the form could not refresh"
    assert str(error) == "the form could not refresh"


@pytest.mark.parametrize("cls", [FormReactionClosedError, FormReactionRequestError, FormReactionHandlerError])
def test_each_door_error_is_a_reaction_error_carrying_its_reason(cls: type[FormReactionError]):
    error = cls("reaction handler 'x' failed")
    assert isinstance(error, FormReactionError)
    assert error.reason == "reaction handler 'x' failed"
    assert str(error) == "reaction handler 'x' failed"


@pytest.mark.parametrize(
    "name",
    [
        "FORM_REACTION_REASON_MAX_CHARS",
        "FormReactionError",
        "FormReactionClosedError",
        "FormReactionRequestError",
        "FormReactionHandlerError",
    ],
)
def test_the_family_is_exported_from_the_interactions_package(name: str):
    assert name in interactions.__all__


def test_a_reason_at_the_bound_is_kept_whole():
    reason = "r" * FORM_REACTION_REASON_MAX_CHARS
    error = FormReactionHandlerError(reason)
    assert error.reason == reason
    assert str(error) == reason


def test_a_reason_past_the_bound_is_shortened_for_the_person_and_logged_whole(caplog: pytest.LogCaptureFixture):
    reason = "x" * (FORM_REACTION_REASON_MAX_CHARS + 50)
    with caplog.at_level("WARNING", logger="tai42_contract.interactions.models.reaction_errors"):
        error = FormReactionHandlerError(reason)
    assert len(error.reason) == FORM_REACTION_REASON_MAX_CHARS
    assert error.reason == "x" * (FORM_REACTION_REASON_MAX_CHARS - 1) + "\u2026"
    assert str(error) == reason
    (record,) = caplog.records
    assert reason in record.getMessage()
    assert "FormReactionHandlerError" in record.getMessage()
