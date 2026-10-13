"""Tests for the driver-continuation context an async ``ask`` reads: the
default (no resuming driver bound), the set/get/reset round-trip, nested restore,
and isolation across concurrent asyncio tasks. The completion continuation (the
run's :class:`DeliveryAddress`) is a generic twin with the same discipline, and the
chain routing a nested dispatch binds keeps its lineage per task.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from tai42_contract.interactions import (
    ChainedResume,
    DeliveryAddress,
    chained_lineage,
    get_chained_resume,
    get_park_completion,
    get_resume_continuation_tool,
    reset_chained_resume,
    reset_park_completion,
    reset_resume_continuation_tool,
    set_chained_resume,
    set_park_completion,
    set_resume_continuation_tool,
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


def test_completion_default_names_no_delivery():
    # No completion delivery bound: a resumed run's driver fires nothing.
    assert get_park_completion() == DeliveryAddress(tool=None, context=None, thread_id=None)


def test_completion_set_get_reset_round_trip():
    token = set_park_completion("conversation_deliver")
    # A bare bind carries no context and no thread; the tool name alone is bound.
    assert get_park_completion() == DeliveryAddress(tool="conversation_deliver", context=None, thread_id=None)
    reset_park_completion(token)
    assert get_park_completion().tool is None


def test_completion_carries_the_opaque_context_and_the_thread():
    context = {"route_name": "chat", "message_id": "m-1"}
    token = set_park_completion("deliver_tool_completion", context, thread_id="thread-1")
    try:
        address = get_park_completion()
    finally:
        reset_park_completion(token)
    assert address == DeliveryAddress(tool="deliver_tool_completion", context=context, thread_id="thread-1")
    # The bound context is a copy: a later mutation of the caller's mapping never reaches it.
    context["route_name"] = "other"
    assert address.context == {"route_name": "chat", "message_id": "m-1"}


@pytest.mark.parametrize("key", ["thread_id", "result", "completion_id", "status"])
def test_completion_context_may_not_carry_a_fire_payload_key(key: str):
    with pytest.raises(ValidationError, match=f"fire payload's own key\\(s\\) \\['{key}'\\]"):
        set_park_completion("deliver_tool_completion", {key: "x"})


def test_a_delivery_address_round_trips_its_json():
    address = DeliveryAddress(tool="deliver_tool_completion", context={"route_name": "chat"}, thread_id="thread-1")
    assert DeliveryAddress.model_validate_json(address.model_dump_json()) == address


@pytest.mark.parametrize(
    "stored",
    [
        {"tool": "deliver_tool_completion", "context": {"route_name": "chat"}},
        ["deliver_tool_completion", {"route_name": "chat"}],
        {"tool": "deliver_tool_completion", "context": None, "thread_id": None, "extra": 1},
    ],
    ids=["no-thread", "pair", "unknown-field"],
)
def test_a_delivery_address_of_any_other_shape_is_refused(stored: object):
    with pytest.raises(ValidationError):
        DeliveryAddress.model_validate(stored)


def test_completion_is_independent_of_the_resume_continuation():
    # The two continuations are separate contextvars: binding one never affects the other.
    completion = set_park_completion("conversation_deliver")
    assert get_resume_continuation_tool() is None
    resume = set_resume_continuation_tool("agent_resume")
    assert get_park_completion().tool == "conversation_deliver"
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


def test_chained_resume_seam_binds_gets_and_resets():
    # Unbound outside any chain.
    assert get_chained_resume() is None
    routing = ChainedResume(delivery_tool="deliver_chained_park", chain_key="tai42:chained-park:k1", asked_by=("main",))
    token = set_chained_resume(routing)
    try:
        bound = get_chained_resume()
        assert bound == routing
        assert bound is not None
        assert bound.delivery_tool == "deliver_chained_park"
        assert bound.chain_key == "tai42:chained-park:k1"
        assert bound.asked_by == ("main",)
    finally:
        reset_chained_resume(token)
    assert get_chained_resume() is None


def _routing(key: str) -> ChainedResume:
    return ChainedResume(delivery_tool="deliver_chained_park", chain_key=key, asked_by=("main",))


def test_the_chained_lineage_nests_the_enclosing_chain_keys_outermost_first():
    assert chained_lineage() == ()
    outer = set_chained_resume(_routing("tai42:chained-park:outer"))
    try:
        assert chained_lineage() == ("tai42:chained-park:outer",)
        inner = set_chained_resume(_routing("tai42:chained-park:inner"))
        try:
            assert chained_lineage() == ("tai42:chained-park:outer", "tai42:chained-park:inner")
            assert get_chained_resume() == _routing("tai42:chained-park:inner")
        finally:
            reset_chained_resume(inner)
        # The reset restores the outer frame, routing and lineage alike.
        assert chained_lineage() == ("tai42:chained-park:outer",)
        assert get_chained_resume() == _routing("tai42:chained-park:outer")
    finally:
        reset_chained_resume(outer)
    assert chained_lineage() == ()
    assert get_chained_resume() is None


def test_an_unchained_binding_starts_an_empty_lineage():
    outer = set_chained_resume(_routing("tai42:chained-park:outer"))
    try:
        cleared = set_chained_resume(None)
        try:
            assert chained_lineage() == ()
            assert get_chained_resume() is None
            # A chained dispatch under the cleared one starts its own lineage.
            nested = set_chained_resume(_routing("tai42:chained-park:fresh"))
            try:
                assert chained_lineage() == ("tai42:chained-park:fresh",)
            finally:
                reset_chained_resume(nested)
        finally:
            reset_chained_resume(cleared)
        assert chained_lineage() == ("tai42:chained-park:outer",)
    finally:
        reset_chained_resume(outer)


def test_re_binding_the_innermost_routing_does_not_repeat_its_key():
    # A driver re-binds, for its resume, the routing it captured at park time.
    outer = set_chained_resume(_routing("tai42:chained-park:outer"))
    try:
        inner = set_chained_resume(_routing("tai42:chained-park:inner"))
        try:
            again = set_chained_resume(_routing("tai42:chained-park:inner"))
            try:
                assert chained_lineage() == ("tai42:chained-park:outer", "tai42:chained-park:inner")
            finally:
                reset_chained_resume(again)
        finally:
            reset_chained_resume(inner)
    finally:
        reset_chained_resume(outer)


def test_the_chained_lineage_is_isolated_per_task():
    async def _nested(key: str) -> tuple[str, ...]:
        token = set_chained_resume(_routing(key))
        try:
            await asyncio.sleep(0)
            return chained_lineage()
        finally:
            reset_chained_resume(token)

    async def scenario() -> tuple[tuple[str, ...], tuple[str, ...]]:
        outer = set_chained_resume(_routing("tai42:chained-park:outer"))
        try:
            a, b = await asyncio.gather(_nested("tai42:chained-park:a"), _nested("tai42:chained-park:b"))
        finally:
            reset_chained_resume(outer)
        return a, b

    a, b = asyncio.run(scenario())
    assert a == ("tai42:chained-park:outer", "tai42:chained-park:a")
    assert b == ("tai42:chained-park:outer", "tai42:chained-park:b")


def test_run_failed_round_trips_json_and_is_frozen():
    from pydantic import ValidationError

    from tai42_contract.interactions import RunFailed

    failed = RunFailed(outcome={"status": "error", "error": "provider down", "nested": {"k": [1, 2]}})
    restored = RunFailed.model_validate_json(failed.model_dump_json())
    assert restored == failed
    assert restored.outcome == {"status": "error", "error": "provider down", "nested": {"k": [1, 2]}}
    with pytest.raises(ValidationError):
        failed.outcome = {}  # type: ignore[misc]


def test_run_failed_is_exported_beside_resume_buffered():
    from tai42_contract.interactions import RunFailed as InteractionsRunFailed
    from tai42_contract.interactions.models import RunFailed as ModelsRunFailed

    assert InteractionsRunFailed is ModelsRunFailed
