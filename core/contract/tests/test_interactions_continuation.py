"""Tests for the driver-continuation context an async ``ask_user`` reads: the
default (no resuming driver bound), the set/get/reset round-trip, nested restore,
and isolation across concurrent asyncio tasks. The completion continuation (the
deferred-response delivery tool) is a generic twin with the same discipline, and
the single-owner adoption guard reads the resume binding to decide whether a
returned park may become the CALLER's own park — adopting it, or CHAINING the nested
call so the run parks on the call and the nested terminal re-enters it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest

from tai42_contract.errors import ErrorKind, error_kind
from tai42_contract.interactions import (
    CHAINED_PARK_CONTEXT_KEY,
    CHAINED_PARK_KEY_PREFIX,
    CHAINED_PARK_TOKEN_KEY,
    PARK_COMPLETION_FAILED,
    PARK_COMPLETION_REPARKED,
    PARK_COMPLETION_SUCCEEDED,
    PARK_COMPLETION_THREAD_KEY,
    SUSPENDED_INTERACTION_MARKER_KEY,
    NestedParkOwnershipError,
    assert_park_adoptable,
    attach_chained_park,
    chained_park_claims,
    chained_park_context,
    fire_continuation_abandoned,
    get_park_completion,
    get_resume_continuation_tool,
    is_chained_park_key,
    new_chained_park_key,
    read_suspended_interaction_marker,
    register_continuation_abandonment_handler,
    repark_notice,
    reset_park_completion,
    reset_resume_continuation_tool,
    resolve_park_adoption,
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


def test_abandonment_handlers_fire_with_the_interaction_id(monkeypatch: pytest.MonkeyPatch):
    # The abandonment seam invokes every registered handler with the abandoned interaction id.
    from tai42_contract.interactions import continuation as cont

    monkeypatch.setattr(cont, "_continuation_abandonment_handlers", [])
    seen_a: list[str] = []
    seen_b: list[str] = []

    async def _a(interaction_id: str) -> None:
        seen_a.append(interaction_id)

    async def _b(interaction_id: str) -> None:
        seen_b.append(interaction_id)

    register_continuation_abandonment_handler(_a)
    register_continuation_abandonment_handler(_b)
    # Idempotent by identity: re-registering the SAME callable adds no duplicate.
    register_continuation_abandonment_handler(_a)
    assert vars(cont)["_continuation_abandonment_handlers"] == [_a, _b]

    asyncio.run(fire_continuation_abandoned("i1"))
    assert seen_a == ["i1"]
    assert seen_b == ["i1"]


def test_abandonment_fire_is_best_effort_per_handler(monkeypatch: pytest.MonkeyPatch):
    # One handler that raises is logged and swallowed, so it never starves the others or aborts
    # the fire.
    from tai42_contract.interactions import continuation as cont

    monkeypatch.setattr(cont, "_continuation_abandonment_handlers", [])
    seen: list[str] = []

    async def _poison(interaction_id: str) -> None:
        raise RuntimeError("boom")

    async def _healthy(interaction_id: str) -> None:
        seen.append(interaction_id)

    register_continuation_abandonment_handler(_poison)
    register_continuation_abandonment_handler(_healthy)
    asyncio.run(fire_continuation_abandoned("i1"))
    assert seen == ["i1"]


def test_abandonment_fire_with_no_handlers_is_a_noop(monkeypatch: pytest.MonkeyPatch):
    from tai42_contract.interactions import continuation as cont

    monkeypatch.setattr(cont, "_continuation_abandonment_handlers", [])
    asyncio.run(fire_continuation_abandoned("i1"))  # no handlers registered — no error


def test_execution_identity_bridge_capture_and_bind(monkeypatch: pytest.MonkeyPatch):
    # The bridge is two host-filled slots: an accessor a park reads to capture the current identity,
    # and a binder the out-of-band fire uses to run under it. With a host accessor/binder registered,
    # capture returns the host value and the binder wraps the fire; with neither, capture is
    # (None, "") and the bind is a no-op (the fire runs unbound).
    import contextlib as _contextlib

    from tai42_contract.interactions import (
        bound_execution_identity_for_fire,
        current_execution_identity,
        register_execution_identity_accessor,
        register_execution_identity_binder,
    )
    from tai42_contract.interactions import continuation as cont

    monkeypatch.setattr(cont, "_execution_identity_accessor", None)
    monkeypatch.setattr(cont, "_execution_identity_binder", None)

    # No host registered: capture yields none and the bind binds nothing.
    assert current_execution_identity() == (None, "")

    async def _no_host_fire() -> bool:
        async with bound_execution_identity_for_fire(None, ""):
            return True

    assert asyncio.run(_no_host_fire()) is True

    register_execution_identity_accessor(lambda: ("user-x", "fp-x"))
    assert current_execution_identity() == ("user-x", "fp-x")

    bound: list[tuple[str, str]] = []

    @_contextlib.asynccontextmanager
    async def _binder(key: str, fingerprint: str) -> AsyncGenerator[None]:
        bound.append((key, fingerprint))
        yield

    register_execution_identity_binder(_binder)

    async def _bound_fire() -> None:
        # A None key stays unbound even with a binder registered.
        async with bound_execution_identity_for_fire(None, ""):
            pass
        async with bound_execution_identity_for_fire("user-x", "fp-x"):
            pass

    asyncio.run(_bound_fire())
    assert bound == [("user-x", "fp-x")]


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
def test_a_malformed_marker_reads_as_no_park(content: object):
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
    assert marker is not None
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


# --- chained parks ----------------------------------------------------------------------


def _chain(key: str, wrapped: tuple[str | None, dict[str, object] | None] = (None, None)):
    """Bind a chained completion for one nested dispatch, as a caller-side binder does."""
    return set_park_completion(_CHAIN_TOOL, chained_park_context(key, wrapped))


_CHAIN_TOOL = "deliver_chained_park"


def test_a_chained_key_is_namespaced_and_unique_per_call():
    first, second = new_chained_park_key(), new_chained_park_key()
    assert first != second
    assert first.startswith(CHAINED_PARK_KEY_PREFIX)
    # A key naming a CALL is distinguishable from a platform interaction id, which is what
    # lets a driver indexing both apply chain-only policy to the right one.
    assert is_chained_park_key(first)
    assert not is_chained_park_key("i1")


def test_a_chained_context_carries_the_key_and_embeds_the_wrapped_binding():
    wrapped = ("deliver_agent_completion", {"thread_id": "thread-1"})
    context = chained_park_context("tai42:chained-park:k1", wrapped)
    assert context[CHAINED_PARK_TOKEN_KEY] == "tai42:chained-park:k1"
    # The replaced binding survives WHOLE inside the composition — tool and context both.
    assert context[CHAINED_PARK_CONTEXT_KEY] == {
        "tool": "deliver_agent_completion",
        "context": {"thread_id": "thread-1"},
    }


def test_a_chained_context_hoists_the_reserved_thread_key():
    # The platform's park-by-thread index reads the ONE reserved field off whatever context is
    # bound; a chained dispatch replaces the caller's context, so the field is carried up.
    wrapped = ("deliver_tool_completion", {PARK_COMPLETION_THREAD_KEY: "thread-1", "extra_field": "r"})
    context = chained_park_context("tai42:chained-park:k1", wrapped)
    assert context[PARK_COMPLETION_THREAD_KEY] == "thread-1"
    # Everything else stays opaque, embedded rather than hoisted.
    assert "extra_field" not in context
    assert context[CHAINED_PARK_CONTEXT_KEY]["context"]["extra_field"] == "r"


def test_a_chained_context_wrapping_nothing_hoists_nothing():
    context = chained_park_context("tai42:chained-park:k1", (None, None))
    assert context == {CHAINED_PARK_TOKEN_KEY: "tai42:chained-park:k1", CHAINED_PARK_CONTEXT_KEY: None}


def test_a_chained_context_is_json_serializable():
    # Every completion context round-trips a durable resume path, so the composition must too.
    context = chained_park_context("tai42:chained-park:k1", ("deliver", {"delivery_thread_id": "t"}))
    assert json.loads(json.dumps(context)) == context


def test_this_runs_own_park_is_adopted_by_its_interaction_id():
    resume = set_resume_continuation_tool("agent_resume")
    completion = _chain("tai42:chained-park:k1")
    try:
        # The ask was raised under THIS run's binding — a chained dispatch around it changes
        # nothing: the run parks on its own interaction, under its own owner, and the platform
        # resumes it directly.
        assert resolve_park_adoption("agent_resume", interaction_id="i1", tool_name="ask") == ("i1", "agent_resume")
    finally:
        reset_park_completion(completion)
        reset_resume_continuation_tool(resume)


def test_a_nested_runs_park_is_chained_when_the_dispatch_was_chained():
    resume = set_resume_continuation_tool("agent_resume")
    completion = _chain("tai42:chained-park:k1")
    try:
        # The nested run keeps its own park and its own resume; this run parks on the CALL —
        # a park owned by its OWN resume continuation, so the claim point downstream agrees.
        claimed = resolve_park_adoption("nested_driver_resume", interaction_id="i-nested", tool_name="run_target")
    finally:
        reset_park_completion(completion)
        reset_resume_continuation_tool(resume)
    assert claimed == ("tai42:chained-park:k1", "agent_resume")


def test_an_ownerless_nested_park_is_chained_too():
    # A nested RUN parked at its tool face names no adoptable owner; it is still chainable,
    # because the chain never adopts its park — it waits on the run's terminal.
    resume = set_resume_continuation_tool("agent_resume")
    completion = _chain("tai42:chained-park:k1")
    try:
        assert resolve_park_adoption(None, interaction_id="i-nested", tool_name="inner_agent") == (
            "tai42:chained-park:k1",
            "agent_resume",
        )
    finally:
        reset_park_completion(completion)
        reset_resume_continuation_tool(resume)


def test_an_unchained_dispatch_still_refuses_a_nested_runs_park():
    # Nothing chained the call, so there is no way to wait on the nested terminal: the loud
    # refusal is unchanged, and it now names chaining as the other way out.
    resume = set_resume_continuation_tool("agent_resume")
    try:
        with pytest.raises(NestedParkOwnershipError) as caught:
            resolve_park_adoption("nested_driver_resume", interaction_id="i1", tool_name="run_target")
    finally:
        reset_resume_continuation_tool(resume)
    message = str(caught.value)
    # The refusal stays store-name-free: it never echoes the expected owner (a public
    # continuation name would hand a caller the exact string to name for a claim).
    assert "'nested_driver_resume'" not in message
    assert "raised under a different run's resume binding" in message
    # The diagnostics name the door that was shut: nothing chained this call.
    assert "nothing chained this call" in message
    assert error_kind(caught.value) is ErrorKind.CONFLICT


def test_a_non_chain_completion_binding_does_not_chain():
    # A plain delivery binding (the conversation door's) is not a chain: its context carries no
    # chained key, so a nested run's park is refused exactly as with nothing bound.
    resume = set_resume_continuation_tool("agent_resume")
    completion = set_park_completion("deliver_agent_completion", {"thread_id": "thread-1"})
    try:
        with pytest.raises(NestedParkOwnershipError):
            resolve_park_adoption("nested_driver_resume", interaction_id="i1", tool_name="run_target")
    finally:
        reset_park_completion(completion)
        reset_resume_continuation_tool(resume)


def test_a_chained_adoption_is_recorded_in_the_open_claims_ledger():
    resume = set_resume_continuation_tool("agent_resume")
    try:
        with chained_park_claims() as claims:
            completion = _chain("tai42:chained-park:k1")
            try:
                resolve_park_adoption("nested_driver_resume", interaction_id="i1", tool_name="run_target")
            finally:
                reset_park_completion(completion)
            # The driver reads the ledger when its drive stops: a claim it never turned into a
            # park is a dead chain it must detach.
            assert claims == {"tai42:chained-park:k1"}
    finally:
        reset_resume_continuation_tool(resume)


def test_an_adopted_own_park_records_no_claim():
    resume = set_resume_continuation_tool("agent_resume")
    try:
        with chained_park_claims() as claims:
            completion = _chain("tai42:chained-park:k1")
            try:
                resolve_park_adoption("agent_resume", interaction_id="i1", tool_name="ask")
            finally:
                reset_park_completion(completion)
            # Nothing was chained, so nothing needs detaching — no key is written for a chain
            # the run never parked on.
            assert claims == set()
    finally:
        reset_resume_continuation_tool(resume)


def test_a_claim_recorded_in_a_child_task_is_visible_to_the_opener():
    async def scenario() -> None:
        resume = set_resume_continuation_tool("agent_resume")
        try:
            with chained_park_claims() as claims:

                async def dispatch(key: str) -> None:
                    # Every nested dispatch runs in a child context (its own binding), so the
                    # ledger has to be shared by identity, not by contextvar value.
                    completion = _chain(key)
                    try:
                        resolve_park_adoption("nested_driver_resume", interaction_id="i", tool_name="run_target")
                    finally:
                        reset_park_completion(completion)

                await asyncio.gather(dispatch("tai42:chained-park:a"), dispatch("tai42:chained-park:b"))
                assert claims == {"tai42:chained-park:a", "tai42:chained-park:b"}
        finally:
            reset_resume_continuation_tool(resume)

    asyncio.run(scenario())


def test_the_claims_ledger_is_closed_outside_a_drive():
    # No ledger open (any code outside a driver's drive): a chained adoption still resolves,
    # it simply records nothing.
    resume = set_resume_continuation_tool("agent_resume")
    completion = _chain("tai42:chained-park:k1")
    try:
        assert resolve_park_adoption("nested_driver_resume", interaction_id="i1", tool_name="run_target") == (
            "tai42:chained-park:k1",
            "agent_resume",
        )
    finally:
        reset_park_completion(completion)
        reset_resume_continuation_tool(resume)


def test_a_repark_notice_fires_only_under_a_chained_binding():
    deadline = datetime(2030, 1, 1, tzinfo=UTC)
    # Nothing bound.
    assert repark_notice(deadline) is None
    # A plain delivery binding is not a chain: its delivery tool has no horizon to refresh.
    completion = set_park_completion("deliver_agent_completion", {"thread_id": "thread-1"})
    try:
        assert repark_notice(deadline) is None
    finally:
        reset_park_completion(completion)


def test_a_repark_notice_carries_the_context_the_new_deadline_and_the_status():
    deadline = datetime(2030, 1, 1, tzinfo=UTC)
    completion = _chain("tai42:chained-park:k1", ("deliver_tool_completion", {PARK_COMPLETION_THREAD_KEY: "thread-1"}))
    try:
        notice = repark_notice(deadline)
    finally:
        reset_park_completion(completion)
    assert notice is not None
    tool, payload = notice
    assert tool == _CHAIN_TOOL
    assert payload[CHAINED_PARK_TOKEN_KEY] == "tai42:chained-park:k1"
    assert payload[PARK_COMPLETION_THREAD_KEY] == "thread-1"
    assert payload["expiry_at"] == deadline.isoformat()
    # The one NON-terminal status: the run did not finish, it re-parked on a new ask.
    assert payload["status"] == PARK_COMPLETION_REPARKED
    assert PARK_COMPLETION_REPARKED not in {PARK_COMPLETION_SUCCEEDED, PARK_COMPLETION_FAILED}


def test_a_repark_notice_renders_a_deadline_less_park_as_none():
    completion = _chain("tai42:chained-park:k1")
    try:
        notice = repark_notice(None)
    finally:
        reset_park_completion(completion)
    assert notice is not None
    assert notice[1]["expiry_at"] is None


def test_attaching_a_claim_drops_it_from_the_ledger():
    resume = set_resume_continuation_tool("agent_resume")
    try:
        with chained_park_claims() as claims:
            completion = _chain("tai42:chained-park:k1")
            try:
                key, _owner = resolve_park_adoption("nested_driver_resume", interaction_id="i1", tool_name="run_target")
            finally:
                reset_park_completion(completion)
            # The driver recorded a park on the key, so it is attached, not dead.
            attach_chained_park(key)
            assert claims == set()
    finally:
        reset_resume_continuation_tool(resume)


def test_attaching_an_unclaimed_key_outside_a_ledger_is_a_no_op():
    # A driver that persists a park with no ledger open (a park face that never chains) still
    # calls this on every key it writes.
    attach_chained_park("tai42:chained-park:never-claimed")
    with chained_park_claims() as claims:
        attach_chained_park("i1")
        assert claims == set()
