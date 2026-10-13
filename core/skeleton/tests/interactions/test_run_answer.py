"""The run answer a direct door hands back for each visit outcome kind."""

from __future__ import annotations

import pytest
from tai42_contract.interactions import (
    ParkedEntry,
    RunAsksAnswer,
    RunNoneAnswer,
    RunParkedAnswer,
    RunResultAnswer,
    SuspendedInteraction,
    VisitOutcome,
)

from tai42_skeleton.interactions.visit import run_answer

_SENTINEL = SuspendedInteraction(interaction_id="i-1", interaction_ids=["i-1"], caller_interaction_ids=["i-1"])


def test_a_result_carries_the_value_whatever_its_shape():
    value = {"kind": "asks", "asks": [{"id": "i-9"}]}
    outcome = VisitOutcome(action="started", kind="result", result=value)

    assert run_answer(outcome) == RunResultAnswer(result=value)


def test_caller_asks_carry_their_entries_and_the_sentinel():
    entry = ParkedEntry(id="i-1", status="asking", to="caller", question="?")
    outcome = VisitOutcome(action="started", kind="asks", asks=[entry], suspended=_SENTINEL)

    assert run_answer(outcome) == RunAsksAnswer(asks=[entry], suspended=_SENTINEL)


def test_a_user_park_carries_the_sentinel_alone():
    outcome = VisitOutcome(action="started", kind="parked", suspended=_SENTINEL)

    assert run_answer(outcome) == RunParkedAnswer(suspended=_SENTINEL)


def test_nothing_ran_is_none():
    assert run_answer(VisitOutcome(action="none", kind="none")) == RunNoneAnswer()


@pytest.mark.parametrize("kind", ["asks", "parked"])
def test_a_park_outcome_without_its_sentinel_is_a_defect(kind: str):
    outcome = VisitOutcome(action="started", kind=kind)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match=f"visit outcome of kind '{kind}' carries no suspended sentinel"):
        run_answer(outcome)
