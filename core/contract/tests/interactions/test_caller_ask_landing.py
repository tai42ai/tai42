"""The caller-ask landing fact: the ambient declaration, the tool-set predicate, and the FAILED payload.

These are the contract-owned, door-agnostic pieces every door and the skeleton's ask park seam read.
The pieces are proven here on their own, with no door and no engine, so another consumer can rely on
them as they are.
"""

from __future__ import annotations

from tai42_contract.interactions import (
    CALLER_ASK_TOOL,
    CallerAskLanding,
    binds_caller_ask,
    caller_ask_no_landing_outcome,
    current_caller_ask_landing,
    declare_caller_ask_landing,
)


def test_binds_caller_ask_true_when_the_tool_is_named():
    assert binds_caller_ask([CALLER_ASK_TOOL]) is True
    assert binds_caller_ask({"echo", CALLER_ASK_TOOL}) is True


def test_binds_caller_ask_false_when_absent_or_unresolved():
    # An empty set names no tool; ``None`` is an unresolved run-time set; a string/bytes is not a
    # tool-name iterable — each is the truthful "does not bind it", never a guess.
    assert binds_caller_ask([]) is False
    assert binds_caller_ask(None) is False
    assert binds_caller_ask(CALLER_ASK_TOOL) is False
    assert binds_caller_ask(CALLER_ASK_TOOL.encode()) is False


def test_caller_ask_no_landing_outcome_names_the_door_and_the_tool():
    outcome = caller_ask_no_landing_outcome("chat")
    assert outcome["tai42:caller_ask_no_landing"] is True
    assert outcome["route"] == "chat"
    assert outcome["tool"] == CALLER_ASK_TOOL
    assert "reply/resume" in outcome["message"]
    assert "chat" in outcome["message"]


def test_caller_ask_landing_model_round_trips_through_json():
    landing = CallerAskLanding(can_land=True, label="chat")
    assert CallerAskLanding.model_validate_json(landing.model_dump_json()) == landing


def test_current_landing_is_none_without_a_declaration():
    assert current_caller_ask_landing() is None


def test_declare_sets_the_ambient_landing_and_resets_on_exit():
    landing = CallerAskLanding(can_land=False, label="chat")
    with declare_caller_ask_landing(landing):
        assert current_caller_ask_landing() == landing
    assert current_caller_ask_landing() is None


def test_declare_none_makes_no_claim():
    with declare_caller_ask_landing(None):
        assert current_caller_ask_landing() is None


def test_nested_declarations_restore_the_outer_value():
    outer = CallerAskLanding(can_land=True, label="outer")
    inner = CallerAskLanding(can_land=False, label="inner")
    with declare_caller_ask_landing(outer):
        with declare_caller_ask_landing(inner):
            assert current_caller_ask_landing() == inner
        assert current_caller_ask_landing() == outer
    assert current_caller_ask_landing() is None
