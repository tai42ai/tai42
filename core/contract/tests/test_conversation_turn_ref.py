"""Contract tests for the ambient conversation-turn seam and the pending-message projection.

Pin the ContextVar deposit's default (``None``), the set/reset round-trip, the nested token
discipline (an inner deposit restores the OUTER value), the copy-on-task inheritance, the frozen
:class:`ConversationTurnRef`, and the frozen :class:`PendingMessage` — the shared, logic-free
channel a tool body or an engine node reads to learn which turn it serves and whether a newer
message is waiting.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from tai42_contract.app.facets import PendingMessage
from tai42_contract.conversations import (
    ConversationTurnRef,
    current_conversation_turn,
    reset_conversation_turn,
    set_conversation_turn,
)


def _ref(message_id: str = "m-1") -> ConversationTurnRef:
    return ConversationTurnRef(thread_id="t-1", message_id=message_id, route_name="chat-line")


def test_default_is_none_outside_any_turn():
    assert current_conversation_turn() is None


def test_set_then_reset_round_trips():
    token = set_conversation_turn(_ref())
    try:
        ref = current_conversation_turn()
        assert ref is not None
        assert ref.thread_id == "t-1"
        assert ref.message_id == "m-1"
        assert ref.route_name == "chat-line"
    finally:
        reset_conversation_turn(token)
    assert current_conversation_turn() is None


def test_nested_deposit_restores_outer_on_reset():
    outer = set_conversation_turn(_ref("outer"))
    try:
        inner = set_conversation_turn(_ref("inner"))
        try:
            current = current_conversation_turn()
            assert current is not None
            assert current.message_id == "inner"
        finally:
            reset_conversation_turn(inner)
        restored = current_conversation_turn()
        assert restored is not None
        assert restored.message_id == "outer"
    finally:
        reset_conversation_turn(outer)
    assert current_conversation_turn() is None


def test_deposit_inherited_by_a_task_on_a_copy():
    async def run() -> str | None:
        token = set_conversation_turn(_ref("m-copy"))
        try:
            task = asyncio.ensure_future(_read_after_yield())
        finally:
            reset_conversation_turn(token)
        return await task

    async def _read_after_yield() -> str | None:
        await asyncio.sleep(0)
        ref = current_conversation_turn()
        return ref.message_id if ref is not None else None

    assert asyncio.run(run()) == "m-copy"


def test_turn_ref_is_frozen():
    ref = _ref()
    with pytest.raises(ValidationError):
        ref.message_id = "m-2"  # type: ignore[misc]


def test_turn_ref_round_trips():
    ref = _ref()
    assert ConversationTurnRef.model_validate_json(ref.model_dump_json()) == ref


def test_pending_message_carries_the_projection():
    pending = PendingMessage(message_id="m-2", text="hello again", accepted_at=1234.5)
    assert pending.message_id == "m-2"
    assert pending.text == "hello again"
    assert pending.accepted_at == 1234.5


def test_pending_message_is_frozen():
    pending = PendingMessage(message_id="m-2", text="hi", accepted_at=1.0)
    with pytest.raises(ValidationError):
        pending.text = "changed"  # type: ignore[misc]


def test_pending_message_requires_a_non_blank_id():
    with pytest.raises(ValidationError):
        PendingMessage(message_id="", text="hi", accepted_at=1.0)


def test_pending_message_round_trips():
    pending = PendingMessage(message_id="m-2", text="hi", accepted_at=2.0)
    assert PendingMessage.model_validate_json(pending.model_dump_json()) == pending
