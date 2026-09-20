"""Contract tests for the overlap policy, the supersede signal, and the answer overlap outcomes.

Pin the :class:`OverlapPolicy` defaults, its field bounds, its cross-field validator both ways,
its serialization round-trip and its carriage on :class:`ConversationRouteCreate`; the
:class:`TurnSupersededError` payload; and the :class:`ConversationAnswer` ``merged``/``superseded``
outcomes with their ``successor_id`` rules — the shapes the skeleton store, the CLI and the Studio
consume.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tai42_contract.conversations import (
    ConversationAnswer,
    ConversationRouteCreate,
    OverlapPolicy,
    TurnSupersededError,
)
from tai42_contract.conversations.overlap import SETTLE_SECONDS_MAX

# --------------------------------------------------------------------------- defaults


def test_default_policy_is_continue_one_no_window():
    policy = OverlapPolicy()
    assert policy.running == "continue"
    assert policy.deliver == "one"
    assert policy.settle_seconds == 0


def test_default_equality_between_two_default_policies():
    assert OverlapPolicy() == OverlapPolicy()


def test_policy_is_frozen():
    policy = OverlapPolicy()
    with pytest.raises(ValidationError):
        policy.running = "cancel"  # type: ignore[misc]


# ------------------------------------------------------------------------------ bounds


@pytest.mark.parametrize("running", ["continue", "cancel"])
def test_every_running_value_accepted(running: str):
    assert OverlapPolicy(running=running, deliver="all").running == running  # type: ignore[arg-type]


@pytest.mark.parametrize("deliver", ["one", "all"])
def test_every_deliver_value_accepted(deliver: str):
    assert OverlapPolicy(deliver=deliver).deliver == deliver  # type: ignore[arg-type]


def test_running_rejects_an_unknown_value():
    with pytest.raises(ValidationError):
        OverlapPolicy(running="pause")  # type: ignore[arg-type]


def test_deliver_rejects_an_unknown_value():
    with pytest.raises(ValidationError):
        OverlapPolicy(deliver="some")  # type: ignore[arg-type]


@pytest.mark.parametrize("settle", [0, 1, 15, SETTLE_SECONDS_MAX])
def test_settle_seconds_within_bounds_accepted(settle: int):
    # Every in-bounds value rides a policy that gives the window a reason (deliver=all).
    assert OverlapPolicy(deliver="all", settle_seconds=settle).settle_seconds == settle


def test_settle_seconds_below_zero_rejected():
    with pytest.raises(ValidationError):
        OverlapPolicy(deliver="all", settle_seconds=-1)


def test_settle_seconds_above_max_rejected():
    with pytest.raises(ValidationError):
        OverlapPolicy(deliver="all", settle_seconds=SETTLE_SECONDS_MAX + 1)


# ----------------------------------------------------------------- cross-field validator


def test_window_under_continue_one_is_refused():
    # A settle window with continue+one would delay every turn for nothing.
    with pytest.raises(ValidationError, match="settle_seconds"):
        OverlapPolicy(running="continue", deliver="one", settle_seconds=5)


def test_window_allowed_when_delivering_all():
    policy = OverlapPolicy(running="continue", deliver="all", settle_seconds=5)
    assert policy.settle_seconds == 5


def test_window_allowed_when_cancelling():
    policy = OverlapPolicy(running="cancel", deliver="one", settle_seconds=5)
    assert policy.settle_seconds == 5


def test_no_window_under_continue_one_is_fine():
    # The default combination with no window runs one turn per message and leaves the payload
    # unchanged.
    assert OverlapPolicy(running="continue", deliver="one", settle_seconds=0).settle_seconds == 0


# -------------------------------------------------------------------- serialization round-trip


@pytest.mark.parametrize(
    "policy",
    [
        OverlapPolicy(),
        OverlapPolicy(running="cancel", deliver="all", settle_seconds=10),
        OverlapPolicy(running="cancel", deliver="one", settle_seconds=3),
        OverlapPolicy(running="continue", deliver="all", settle_seconds=0),
    ],
)
def test_policy_round_trips_through_dump_and_validate(policy: OverlapPolicy):
    assert OverlapPolicy.model_validate(policy.model_dump()) == policy
    assert OverlapPolicy.model_validate_json(policy.model_dump_json()) == policy


# --------------------------------------------------------------------- carriage on the route


def _route_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "route_name": "chat-line",
        "door": "api",
        "target_kind": "agent",
        "target_name": "assistant",
        "execution_key": "u-acme",
    }
    base.update(overrides)
    return base


def test_route_defaults_to_the_default_policy():
    route = ConversationRouteCreate(**_route_kwargs())  # type: ignore[arg-type]
    assert route.overlap == OverlapPolicy()


def test_route_carries_an_explicit_policy_and_round_trips():
    policy = OverlapPolicy(running="cancel", deliver="all", settle_seconds=7)
    route = ConversationRouteCreate(**_route_kwargs(overlap=policy))  # type: ignore[arg-type]
    assert route.overlap == policy
    restored = ConversationRouteCreate.model_validate_json(route.model_dump_json())
    assert restored.overlap == policy


def test_route_with_no_overlap_key_validates_to_the_default():
    # A route body with no ``overlap`` key validates to the default policy — pydantic fills it in.
    body = _route_kwargs()
    route = ConversationRouteCreate.model_validate(body)
    assert route.overlap == OverlapPolicy()


def test_route_rejects_an_invalid_nested_policy():
    with pytest.raises(ValidationError):
        ConversationRouteCreate(**_route_kwargs(overlap={"deliver": "one", "settle_seconds": 5}))  # type: ignore[arg-type]


# --------------------------------------------------------------------- TurnSupersededError


def test_supersede_error_carries_the_successor_id():
    err = TurnSupersededError("m-123")
    assert err.successor_id == "m-123"


def test_supersede_error_message_names_the_successor():
    err = TurnSupersededError("m-123")
    assert "m-123" in str(err)


def test_supersede_error_derives_from_base_exception_not_exception():
    # The hand-over signal must slip past every ``except Exception`` between a yielding body and
    # the arms that resolve the turn, so it derives from BaseException and is NOT an Exception —
    # the same discipline asyncio.CancelledError follows.
    assert issubclass(TurnSupersededError, BaseException)
    assert not issubclass(TurnSupersededError, Exception)


def test_supersede_error_is_not_caught_by_except_exception():
    # Behavioural proof: an ``except Exception`` arm lets the signal through untouched, so only
    # the two by-name arms in the skeleton resolve the hand-over.
    def _guarded_by_except_exception() -> None:
        try:
            raise TurnSupersededError("m-9")
        except Exception:
            pytest.fail("except Exception must not catch the supersede signal")

    with pytest.raises(TurnSupersededError) as caught:
        _guarded_by_except_exception()
    assert caught.value.successor_id == "m-9"


# -------------------------------------------------------------- AnswerStatus + successor_id


def _answer_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {"message_id": "m-1", "thread_id": "t-1", "status": "answered", "answer": "hi"}
    base.update(overrides)
    return base


def test_answered_answer_forbids_a_successor_id():
    with pytest.raises(ValidationError, match="successor_id"):
        ConversationAnswer(**_answer_kwargs(successor_id="m-2"))  # type: ignore[arg-type]


def test_silent_answer_forbids_a_successor_id():
    with pytest.raises(ValidationError, match="successor_id"):
        ConversationAnswer(**_answer_kwargs(status="silent", answer=None, successor_id="m-2"))  # type: ignore[arg-type]


def test_answered_answer_defaults_successor_id_to_none():
    answer = ConversationAnswer(**_answer_kwargs())  # type: ignore[arg-type]
    assert answer.successor_id is None


@pytest.mark.parametrize("status", ["merged", "superseded"])
def test_overlap_outcome_requires_a_non_blank_successor_id(status: str):
    with pytest.raises(ValidationError, match="successor_id"):
        ConversationAnswer(message_id="m-1", thread_id="t-1", status=status, successor_id=None)  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="successor_id"):
        ConversationAnswer(message_id="m-1", thread_id="t-1", status=status, successor_id="   ")  # type: ignore[arg-type]


@pytest.mark.parametrize("status", ["merged", "superseded"])
def test_overlap_outcome_is_answerless(status: str):
    with pytest.raises(ValidationError, match="answer text"):
        ConversationAnswer(message_id="m-1", thread_id="t-1", status=status, answer="hi", successor_id="m-2")  # type: ignore[arg-type]


@pytest.mark.parametrize("status", ["merged", "superseded"])
def test_overlap_outcome_carries_no_parts(status: str):
    with pytest.raises(ValidationError, match="no parts"):
        ConversationAnswer(
            message_id="m-1",
            thread_id="t-1",
            status=status,  # type: ignore[arg-type]
            parts=[{"message": "hi"}],  # type: ignore[list-item]
            successor_id="m-2",
        )


@pytest.mark.parametrize("status", ["merged", "superseded"])
def test_overlap_outcome_round_trips(status: str):
    answer = ConversationAnswer(message_id="m-1", thread_id="t-1", status=status, successor_id="m-2")  # type: ignore[arg-type]
    assert answer.status == status
    assert answer.successor_id == "m-2"
    assert answer.answer is None
    restored = ConversationAnswer.model_validate_json(answer.model_dump_json())
    assert restored == answer
