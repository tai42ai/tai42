"""The caller-ask landing seam: a caller ask about to park where the door declared NO landing fails.

The guarantee lives at the ask park path (``resolve_async_continuation``): it is the ONE place every
door and every nesting depth funnels through. A caller ask under an absent landing raises the typed
run failure (``RunTerminalFailed``) BEFORE any state is written; a user ask, a present landing, and a
door that declared nothing are all left to the ordinary park path.
"""

from __future__ import annotations

import pytest
from tai42_contract.interactions import CallerAskLanding, RunTerminalFailed, declare_caller_ask_landing

from tai42_skeleton.interactions.ask.park import resolve_async_continuation


def test_caller_ask_under_absent_landing_fails_typed_before_any_state() -> None:
    # The seam raises the TYPED run failure naming the route and the caller-ask tool — not a
    # RuntimeError the ordinary park path would raise for a missing driver, and not a tool-level
    # error a model could read.
    with (
        declare_caller_ask_landing(CallerAskLanding(can_land=False, label="chat")),
        pytest.raises(RunTerminalFailed) as excinfo,
    ):
        resolve_async_continuation("caller")
    outcome = excinfo.value.outcome
    assert outcome["route"] == "chat"
    assert outcome["tool"] == "ask"
    assert "reply/resume" in outcome["message"]


def test_user_ask_under_absent_landing_is_not_refused() -> None:
    # A user ask is delivered out of band and resumed by its own answer; it needs no caller landing.
    # The landing check does not fire, so the next prerequisite (a bound resuming driver) is what
    # raises — a plain RuntimeError, never the typed run failure.
    with (
        declare_caller_ask_landing(CallerAskLanding(can_land=False, label="chat")),
        pytest.raises(RuntimeError, match="resuming driver"),
    ):
        resolve_async_continuation("user")


def test_caller_ask_with_present_landing_passes_the_landing_check() -> None:
    # Landing present: the caller-ask landing check passes and the seam proceeds to its other
    # prerequisites (the driver), which raise a plain RuntimeError — proving the ask was NOT refused
    # on landing.
    with (
        declare_caller_ask_landing(CallerAskLanding(can_land=True, label="chat")),
        pytest.raises(RuntimeError, match="resuming driver"),
    ):
        resolve_async_continuation("caller")


def test_caller_ask_with_no_declared_landing_is_not_refused() -> None:
    # A door that declared nothing makes no claim: a caller ask is not refused on landing (the driver
    # prerequisite raises instead).
    with pytest.raises(RuntimeError, match="resuming driver"):
        resolve_async_continuation("caller")
