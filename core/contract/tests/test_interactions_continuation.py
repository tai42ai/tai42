"""Tests for the driver-continuation context an async ``ask_user`` reads: the
default (no resuming driver bound), the set/get/reset round-trip, nested restore,
and isolation across concurrent asyncio tasks. The completion continuation (the
deferred-response delivery tool) is a generic twin with the same discipline, and
the single-owner adoption guard reads the resume binding to decide whether a
returned park may become the CALLER's own park.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tai42_contract.errors import ErrorKind, error_kind
from tai42_contract.interactions import (
    SUSPENDED_INTERACTION_MARKER_KEY,
    NestedParkOwnershipError,
    assert_park_adoptable,
    get_park_completion,
    get_resume_continuation_tool,
    read_suspended_interaction_marker,
    reset_park_completion,
    reset_resume_continuation_tool,
    set_park_completion,
    set_resume_continuation_tool,
    suspended_interaction_marker,
)


def test_default_is_none():
    # No resuming driver bound: an async ask raised here has no continuation tool.
    assert get_resume_continuation_tool() is None


def test_set_get_reset_round_trip():
    token = set_resume_continuation_tool("resume_tool")
    assert get_resume_continuation_tool() == "resume_tool"
    reset_resume_continuation_tool(token)
    assert get_resume_continuation_tool() is None


def test_reset_restores_previous_value():
    outer = set_resume_continuation_tool("outer_tool")
    inner = set_resume_continuation_tool("inner_tool")
    assert get_resume_continuation_tool() == "inner_tool"
    reset_resume_continuation_tool(inner)
    # Resetting the inner token restores the outer binding, not the default.
    assert get_resume_continuation_tool() == "outer_tool"
    reset_resume_continuation_tool(outer)
    assert get_resume_continuation_tool() is None


def test_completion_default_is_none():
    # No completion delivery bound: a resumed run's driver fires nothing.
    assert get_park_completion() == (None, None)


def test_completion_set_get_reset_round_trip():
    token = set_park_completion("conversation_deliver")
    # A bare bind carries no context; the tool name alone is bound.
    assert get_park_completion() == ("conversation_deliver", None)
    reset_park_completion(token)
    assert get_park_completion() == (None, None)


def test_completion_carries_opaque_context():
    # The opaque context binds and reads back alongside the tool name, untouched.
    context = {"delivery_thread_id": "thread-1"}
    token = set_park_completion("deliver_tool_completion", context)
    assert get_park_completion() == ("deliver_tool_completion", context)
    reset_park_completion(token)
    assert get_park_completion() == (None, None)


def test_completion_is_independent_of_the_resume_continuation():
    # The two continuations are separate contextvars: binding one never affects the other.
    completion = set_park_completion("conversation_deliver")
    assert get_resume_continuation_tool() is None
    resume = set_resume_continuation_tool("agent_resume")
    assert get_park_completion() == ("conversation_deliver", None)
    reset_resume_continuation_tool(resume)
    reset_park_completion(completion)


def test_isolation_across_tasks():
    async def scenario() -> None:
        set_resume_continuation_tool("parent_tool")
        seen: dict[str, str | None] = {}

        async def worker(name: str) -> None:
            # A task starts from a copy of the parent context, then binds its own
            # driver continuation without disturbing siblings or the parent.
            assert get_resume_continuation_tool() == "parent_tool"
            set_resume_continuation_tool(name)
            await asyncio.sleep(0)
            seen[name] = get_resume_continuation_tool()

        await asyncio.gather(worker("a"), worker("b"))
        assert seen == {"a": "a", "b": "b"}
        assert get_resume_continuation_tool() == "parent_tool"

    asyncio.run(scenario())


def test_a_park_raised_under_the_bound_continuation_is_adoptable():
    # The caller's own binding raised the ask, so the caller is the park's resume owner and
    # may record it as its own suspended state.
    token = set_resume_continuation_tool("agent_resume")
    try:
        assert_park_adoptable("agent_resume", interaction_id="i1", tool_name="ask")
    finally:
        reset_resume_continuation_tool(token)


def test_a_nested_drivers_park_is_refused_by_the_caller():
    # A nested driver bound its OWN continuation for the ask, so the platform resumes THAT
    # driver — a caller adopting the park would wait on a resume never fired at it.
    token = set_resume_continuation_tool("agent_resume")
    try:
        with pytest.raises(NestedParkOwnershipError) as caught:
            assert_park_adoptable("nested_driver_resume", interaction_id="i1", tool_name="run_target")
    finally:
        reset_resume_continuation_tool(token)
    message = str(caught.value)
    # The refusal names the offending tool and the park, so the operator can locate it.
    assert "run_target" in message
    assert "i1" in message
    # But it names NEITHER owner: echoing the bound continuation would hand a model the exact
    # string to name for a claim, and the guard is a nesting discriminator, not an owner report.
    assert "'nested_driver_resume'" not in message
    assert "agent_resume" not in message
    assert error_kind(caught.value) is ErrorKind.CONFLICT


def test_an_ownerless_park_is_refused_by_a_bound_caller():
    # A park surfaced with NO owner belongs to a nested RUN driving its own resume (an agent
    # run parked at its tool face); a bound caller may not adopt it either.
    token = set_resume_continuation_tool("agent_resume")
    try:
        with pytest.raises(NestedParkOwnershipError) as caught:
            assert_park_adoptable(None, interaction_id="i1", tool_name="inner_agent")
    finally:
        reset_resume_continuation_tool(token)
    message = str(caught.value)
    # The ownerless case gets its OWN sentence: there is no owning driver to name, so the
    # message never claims the park is "owned by None".
    assert "no adoptable owner" in message
    assert "None" not in message
    # And it never names the bound continuation either.
    assert "agent_resume" not in message


def test_an_ownerless_park_is_refused_for_an_unbound_caller_too():
    # ``None`` is not adoptable by ANY caller, bound or not: an ask that parks always stamps
    # the continuation it was raised under, so an ownerless park is one no ask minted for this
    # caller. An unbound caller has no continuation the platform could fire either way, so a
    # park it claimed could only hang.
    assert get_resume_continuation_tool() is None
    with pytest.raises(NestedParkOwnershipError):
        assert_park_adoptable(None, interaction_id="i1", tool_name="ask")


def test_an_owned_park_is_refused_when_nothing_is_bound_here():
    # A real park reaching a caller that binds no resume path at all: still refused as a park
    # raised under a different run's binding.
    assert get_resume_continuation_tool() is None
    with pytest.raises(NestedParkOwnershipError, match="different run's resume binding"):
        assert_park_adoptable("nested_driver_resume", interaction_id="i1", tool_name="run_target")


def test_the_refusal_leads_with_the_remedy_and_trails_the_diagnostics():
    # A tool error is read by a MODEL: the actionable sentence comes first and the identifiers
    # ride at the end, so a truncated read still carries what to do about it.
    token = set_resume_continuation_tool("agent_resume")
    try:
        with pytest.raises(NestedParkOwnershipError) as caught:
            assert_park_adoptable("nested_driver_resume", interaction_id="i1", tool_name="run_target")
    finally:
        reset_resume_continuation_tool(token)
    message = str(caught.value)
    assert message.startswith("Tool 'run_target' cannot be used inside this run")
    assert message.index("conversation route") < message.index("[interaction i1")
    assert message.rstrip().endswith("]")


def test_the_wire_marker_carries_the_park_owner():
    # The marker is the WIRE form a claimer reads off a serialized tool result, so the owner
    # has to ride it — the sentinel object never reaches that seam.
    payload = suspended_interaction_marker("i1", None, "agent_resume")[SUSPENDED_INTERACTION_MARKER_KEY]
    assert payload == {"interaction_id": "i1", "expiry_at": None, "resume_owner": "agent_resume"}
    # And it survives the JSON round trip the tool-output serialization puts it through.
    assert read_suspended_interaction_marker(json.dumps({SUSPENDED_INTERACTION_MARKER_KEY: payload})) == payload


@pytest.mark.parametrize(
    "content",
    [
        {SUSPENDED_INTERACTION_MARKER_KEY: {}},
        {SUSPENDED_INTERACTION_MARKER_KEY: "hello"},
        {SUSPENDED_INTERACTION_MARKER_KEY: {"resume_owner": "agent_resume"}},
        {SUSPENDED_INTERACTION_MARKER_KEY: {"interaction_id": 5, "expiry_at": None}},
        json.dumps({SUSPENDED_INTERACTION_MARKER_KEY: {"expiry_at": None}}),
    ],
    ids=["empty-payload", "bare-string", "owner-only", "non-str-id", "json-no-id"],
)
def test_a_malformed_marker_reads_as_no_park(content):
    # The reserved key is present but the payload is not a well-formed marker — content a model
    # shaped, or a corrupt wire form. The reader shape-checks the payload and returns None rather
    # than a dict a caller would KeyError on and abort the run over.
    assert read_suspended_interaction_marker(content) is None


def test_a_legacy_two_key_marker_still_reads():
    # A wire form written before the resume_owner field carries only interaction_id + expiry_at;
    # it is still a well-formed park and reads back (its ownerless payload is refused downstream).
    legacy = {SUSPENDED_INTERACTION_MARKER_KEY: {"interaction_id": "i1", "expiry_at": None}}
    marker = read_suspended_interaction_marker(json.dumps(legacy))
    assert marker == {"interaction_id": "i1", "expiry_at": None}
    assert marker.get("resume_owner") is None


def test_a_marker_built_without_an_owner_is_not_adoptable():
    # The owner argument defaults to None so an un-updated minter fails CLOSED: whatever it
    # built names no owner, and no bound driver may claim it.
    marker = read_suspended_interaction_marker(suspended_interaction_marker("i1", None))
    assert marker is not None
    assert marker["resume_owner"] is None
    token = set_resume_continuation_tool("agent_resume")
    try:
        with pytest.raises(NestedParkOwnershipError):
            assert_park_adoptable(marker["resume_owner"], interaction_id=marker["interaction_id"], tool_name="t")
    finally:
        reset_resume_continuation_tool(token)


def test_a_blank_owner_reads_as_no_owner():
    # A blank owner names no registered tool the platform could dispatch: it takes the ownerless
    # (nested/foreign) branch rather than being reported as an owner.
    token = set_resume_continuation_tool("agent_resume")
    try:
        with pytest.raises(NestedParkOwnershipError, match="no adoptable owner"):
            assert_park_adoptable("   ", interaction_id="i1", tool_name="relay")
    finally:
        reset_resume_continuation_tool(token)
