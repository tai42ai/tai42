"""The ``set_conversation_mode`` builtin: a thin shim over ``set_current_thread_mode`` that
takes only ``mode`` and returns only ``{"mode"}``.
"""

from __future__ import annotations

import pytest
from fastmcp.utilities.types import get_cached_typeadapter
from tai42_contract.conversations import ConversationMode

from tai42_skeleton.conversations.mode import NoBridgeTurnError
from tai42_skeleton.tools.builtin import set_conversation_mode as builtin_mode


async def test_builtin_returns_only_the_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_set(mode: str) -> ConversationMode:
        calls.append(mode)
        return "manual"

    monkeypatch.setattr(builtin_mode, "set_current_thread_mode", fake_set)

    result = await builtin_mode.set_conversation_mode("manual")

    assert result == {"mode": "manual"}
    assert set(result) == {"mode"}
    assert calls == ["manual"]


async def test_builtin_propagates_the_outside_turn_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_set(mode: str) -> ConversationMode:
        raise NoBridgeTurnError("set_conversation_mode was called outside a bridge turn")

    monkeypatch.setattr(builtin_mode, "set_current_thread_mode", fake_set)

    with pytest.raises(NoBridgeTurnError, match="outside a bridge turn"):
        await builtin_mode.set_conversation_mode("manual")


async def test_builtin_propagates_a_bad_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_set(mode: str) -> ConversationMode:
        raise ValueError("conversation mode must be one of ['agent', 'manual']")

    monkeypatch.setattr(builtin_mode, "set_current_thread_mode", fake_set)

    with pytest.raises(ValueError, match="conversation mode must be one of"):
        await builtin_mode.set_conversation_mode("auto")


def test_builtin_input_schema_is_one_required_mode_string() -> None:
    schema = get_cached_typeadapter(builtin_mode.set_conversation_mode).json_schema()
    props = schema["properties"]
    assert set(props) == {"mode"}
    assert props["mode"]["type"] == "string"
    assert set(schema["required"]) == {"mode"}
