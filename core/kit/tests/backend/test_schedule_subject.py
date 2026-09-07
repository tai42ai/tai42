"""The schedule-subject carrier: stamped at creation (where "this is a schedule" is
known), popped and re-established as the ``schedule`` state context at the fire."""

from __future__ import annotations

import pytest
from tai42_contract.states import StateSubject

from tai42_kit.backend.callback import prepare_backend_kwargs
from tai42_kit.utils.schedule_subject import (
    SCHEDULE_SUBJECT_ARG,
    pop_schedule_subject,
    schedule_state_context,
)
from tai42_kit.utils.state_context import current_state_context


def _func(subject=None, backend_tool_name=None, **_: object) -> None:
    """A stand-in dispatch target whose signature carries the kwargs the wrapper strips."""


_SUBJECT = {"target_kind": "tool", "target_name": "assistant", "kind": "person", "key": "p-1"}


async def test_scheduled_prepare_stamps_a_parseable_subject() -> None:
    kwargs = await prepare_backend_kwargs(_func, "backend_tool_name", "greet", {"subject": _SUBJECT}, scheduled=True)
    stamped = kwargs[SCHEDULE_SUBJECT_ARG]
    assert StateSubject.model_validate(stamped) == StateSubject.model_validate(_SUBJECT)
    # The subject argument stays for the tool (a flow reads ``.subject``, a state tool
    # takes it as an override) — the stamp is an additional door signal, not a move.
    assert kwargs["subject"] == _SUBJECT


async def test_unscheduled_prepare_stamps_nothing() -> None:
    kwargs = await prepare_backend_kwargs(_func, "backend_tool_name", "greet", {"subject": _SUBJECT})
    assert SCHEDULE_SUBJECT_ARG not in kwargs


async def test_scheduled_prepare_ignores_a_non_full_subject() -> None:
    # A partial ``{kind, key}`` is not a full subject the fire could re-key on; it stays a
    # plain argument and no schedule-subject is stamped.
    kwargs = await prepare_backend_kwargs(
        _func, "backend_tool_name", "greet", {"subject": {"kind": "person", "key": "p-1"}}, scheduled=True
    )
    assert SCHEDULE_SUBJECT_ARG not in kwargs


def test_pop_schedule_subject_strips_and_parses() -> None:
    kwargs = {SCHEDULE_SUBJECT_ARG: _SUBJECT, "other": 1}
    subject = pop_schedule_subject(kwargs)
    assert subject == StateSubject.model_validate(_SUBJECT)
    assert SCHEDULE_SUBJECT_ARG not in kwargs
    assert kwargs == {"other": 1}


def test_pop_schedule_subject_none_when_absent() -> None:
    assert pop_schedule_subject({"other": 1}) is None


def test_pop_schedule_subject_raises_on_a_malformed_value() -> None:
    with pytest.raises(ValueError, match="StateSubject"):
        pop_schedule_subject({SCHEDULE_SUBJECT_ARG: {"kind": "person"}})


def test_schedule_state_context_deposits_the_schedule_door() -> None:
    kwargs = {SCHEDULE_SUBJECT_ARG: _SUBJECT, "message": "hi"}
    with schedule_state_context(kwargs):
        ctx = current_state_context()
        assert ctx is not None
        assert ctx.door == "schedule"
        assert ctx.actor is None
        assert ctx.candidates.target_kind == "tool"
        assert ctx.candidates.target_name == "assistant"
        assert ctx.candidates.by_kind == {"person": "p-1"}
    # The arg is stripped from the fire kwargs; the context is torn down after the block.
    assert SCHEDULE_SUBJECT_ARG not in kwargs
    assert current_state_context() is None


def test_schedule_state_context_is_a_noop_without_a_subject() -> None:
    kwargs = {"message": "hi"}
    with schedule_state_context(kwargs):
        assert current_state_context() is None
