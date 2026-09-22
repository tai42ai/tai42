"""The schedule-subject carrier: stamped at creation (where "this is a schedule" is
known), popped and re-established as the ``schedule`` state context at the fire."""

from __future__ import annotations

import pytest
from tai42_contract.states import StateBinding, StateSubject

from tai42_kit.backend.callback import prepare_backend_kwargs
from tai42_kit.utils.schedule_subject import (
    SCHEDULE_CONTRACT_ARG,
    SCHEDULE_EXECUTION_FINGERPRINT_ARG,
    SCHEDULE_EXECUTION_KEY_ARG,
    SCHEDULE_NAME_KEY,
    SCHEDULE_STATE_BINDING_ARG,
    SCHEDULE_STATE_BINDING_REQUEST_KEY,
    SCHEDULE_SUBJECT_ARG,
    ReservedScheduleKeyError,
    assert_no_reserved_schedule_keys,
    pop_schedule_state_binding,
    pop_schedule_subject,
    schedule_create_fire,
    schedule_state_context,
)
from tai42_kit.utils.state_context import current_state_context

_BINDING = {"states": [{"state": "status", "subject_expr": {"content": ".subject.key"}, "templates": ["summary"]}]}


def _func(subject=None, backend_tool_name=None, **_: object) -> None:
    """A stand-in dispatch target whose signature carries the kwargs the wrapper strips."""


_SUBJECT = {"target_kind": "tool", "target_name": "assistant", "kind": "person", "key": "p-1"}


async def test_scheduled_prepare_stamps_a_parseable_subject() -> None:
    with schedule_create_fire():
        kwargs = await prepare_backend_kwargs(
            _func, "backend_tool_name", "greet", {"subject": _SUBJECT}, scheduled=True
        )
    stamped = kwargs[SCHEDULE_SUBJECT_ARG]
    assert StateSubject.model_validate(stamped) == StateSubject.model_validate(_SUBJECT)
    # The subject argument stays for the tool (a flow reads ``.subject``, a state tool
    # takes it as an override) — the stamp is an additional door signal, not a move.
    assert kwargs["subject"] == _SUBJECT


async def test_scheduled_prepare_outside_the_create_fire_is_refused() -> None:
    # A ``<tool>_schedule_task`` branch named directly at the run-tool/MCP edge reaches the scheduled
    # preparer with no create fire on the stack — refused loudly so its reserved keys never reach a fire.
    with pytest.raises(ReservedScheduleKeyError, match="schedule-create door"):
        await prepare_backend_kwargs(_func, "backend_tool_name", "greet", {"subject": _SUBJECT}, scheduled=True)


async def test_unscheduled_prepare_stamps_nothing() -> None:
    kwargs = await prepare_backend_kwargs(_func, "backend_tool_name", "greet", {"subject": _SUBJECT})
    assert SCHEDULE_SUBJECT_ARG not in kwargs


async def test_scheduled_prepare_ignores_a_non_full_subject() -> None:
    # A partial ``{kind, key}`` is not a full subject the fire could re-key on; it stays a
    # plain argument and no schedule-subject is stamped.
    with schedule_create_fire():
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


# -- the door-layer state binding rides the SAME kit seam --------------------
async def test_scheduled_prepare_stamps_the_binding_and_pops_the_raw_key() -> None:
    with schedule_create_fire():
        kwargs = await prepare_backend_kwargs(
            _func, "backend_tool_name", "greet", {"state_binding": _BINDING, "message": "hi"}, scheduled=True
        )
    # The binding is stamped under the reserved arg; UNLIKE the subject, the raw key is POPPED
    # so the base tool never sees it (tools stay pure).
    assert kwargs[SCHEDULE_STATE_BINDING_ARG] == _BINDING
    assert "state_binding" not in kwargs
    assert kwargs["message"] == "hi"


async def test_unscheduled_prepare_refuses_a_reserved_state_binding_key() -> None:
    # A background task tool's caller never supplies the reserved request-field binding; the platform
    # stamps it only on the create door's recurring (scheduled) path, so one on a task job is a forgery
    # refused loudly — never a silently forwarded key.
    with pytest.raises(ReservedScheduleKeyError, match="state_binding"):
        await prepare_backend_kwargs(_func, "backend_tool_name", "greet", {"state_binding": _BINDING}, scheduled=False)


@pytest.mark.parametrize(
    "reserved_key",
    [
        SCHEDULE_SUBJECT_ARG,
        SCHEDULE_STATE_BINDING_ARG,
        SCHEDULE_EXECUTION_KEY_ARG,
        SCHEDULE_EXECUTION_FINGERPRINT_ARG,
        SCHEDULE_CONTRACT_ARG,
        SCHEDULE_STATE_BINDING_REQUEST_KEY,
    ],
)
async def test_unscheduled_prepare_refuses_each_forged_reserved_key(reserved_key: str) -> None:
    # Every reserved door key a caller could forge on a background task job is refused at the shared
    # kit chokepoint BEFORE the ambient fire is stamped, so it can never survive to ``backend_fire``.
    with pytest.raises(ReservedScheduleKeyError, match="reserved schedule"):
        await prepare_backend_kwargs(
            _func, "backend_tool_name", "greet", {reserved_key: {"x": 1}, "message": "hi"}, scheduled=False
        )


async def test_unscheduled_prepare_allows_the_exempt_cadence_name() -> None:
    # The cadence name is the caller's own key (its revocation handle), the single exemption; a task job
    # carrying it is not refused.
    kwargs = await prepare_backend_kwargs(
        _func, "backend_tool_name", "greet", {SCHEDULE_NAME_KEY: "nightly", "message": "hi"}, scheduled=False
    )
    assert kwargs[SCHEDULE_NAME_KEY] == "nightly"


def test_assert_no_reserved_schedule_keys_names_the_offending_key() -> None:
    with pytest.raises(ReservedScheduleKeyError, match=SCHEDULE_EXECUTION_KEY_ARG):
        assert_no_reserved_schedule_keys({SCHEDULE_EXECUTION_KEY_ARG: "victim", "message": "hi"})
    # A clean mapping and the exempt cadence name both pass without raising.
    assert_no_reserved_schedule_keys({"message": "hi", SCHEDULE_NAME_KEY: "nightly"})


def test_pop_schedule_state_binding_strips_and_parses() -> None:
    kwargs = {SCHEDULE_STATE_BINDING_ARG: _BINDING, "other": 1}
    binding = pop_schedule_state_binding(kwargs)
    assert binding == StateBinding.model_validate(_BINDING)
    assert kwargs == {"other": 1}


def test_pop_schedule_state_binding_raises_on_a_malformed_value() -> None:
    with pytest.raises(ValueError, match=r"[Ss]tate"):
        pop_schedule_state_binding({SCHEDULE_STATE_BINDING_ARG: {"states": [{"no_subject": True}]}})


def test_worker_wrapper_strips_both_reserved_keys_and_deposits_the_subject() -> None:
    # The worker wrapper pops BOTH reserved kwargs (so neither reaches the tool) and deposits the
    # subject door; the door-layer binding is applied by ``fire_schedule_door`` around the started
    # tool, so the wrapper strips it here but leaves the ambient dispatch context to that seam.
    kwargs = {SCHEDULE_SUBJECT_ARG: _SUBJECT, SCHEDULE_STATE_BINDING_ARG: _BINDING, "message": "hi"}
    with schedule_state_context(kwargs):
        ctx = current_state_context()
        assert ctx is not None
        assert ctx.door == "schedule"
        assert ctx.candidates.by_kind == {"person": "p-1"}
    assert kwargs == {"message": "hi"}
    assert current_state_context() is None
