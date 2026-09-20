"""LeadingUserMiddleware.before_model: prepend a marker when the first non-system
message is an assistant message, no-op on user-first and empty histories, and
insert the marker after leading system messages.
"""

import pytest

pytest.importorskip("langgraph")

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, RemoveMessage, SystemMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime

from tai42_kit.llm.middleware.leading_user import _CONVERSATION_START_MARKER, LeadingUserMiddleware

# before_model ignores the runtime; a default-constructed Runtime satisfies the
# parameter type without a live graph execution context.
_RUNTIME: Runtime[None] = Runtime()


def test_leading_assistant_prepends_marker():
    mw = LeadingUserMiddleware()
    ai = AIMessage("operator opener", id="a")
    human = HumanMessage("client reply", id="h")
    update = mw.before_model({"messages": [ai, human]}, _RUNTIME)

    assert update is not None
    out = update["messages"]
    assert isinstance(out[0], RemoveMessage)
    assert out[0].id == REMOVE_ALL_MESSAGES
    # A marker user message now leads, ahead of the original assistant message.
    assert isinstance(out[1], HumanMessage)
    assert out[1].content == _CONVERSATION_START_MARKER
    assert out[2] is ai
    assert out[3] is human


def test_user_first_is_untouched():
    mw = LeadingUserMiddleware()
    msgs: list[AnyMessage] = [HumanMessage("hi", id="1"), AIMessage("there", id="2")]
    # Already user-first -> no rewrite, signalled by None.
    assert mw.before_model({"messages": msgs}, _RUNTIME) is None


def test_system_then_assistant_inserts_after_system():
    mw = LeadingUserMiddleware()
    system = SystemMessage("rules", id="s")
    ai = AIMessage("operator opener", id="a")
    update = mw.before_model({"messages": [system, ai]}, _RUNTIME)

    assert update is not None
    out = update["messages"]
    assert isinstance(out[0], RemoveMessage)
    # The system message stays first; the marker sits between it and the assistant.
    assert out[1] is system
    assert isinstance(out[2], HumanMessage)
    assert out[2].content == _CONVERSATION_START_MARKER
    assert out[3] is ai


def test_empty_history_is_untouched():
    mw = LeadingUserMiddleware()
    assert mw.before_model({"messages": []}, _RUNTIME) is None


def test_leading_marker_is_idempotent():
    mw = LeadingUserMiddleware()
    marker = HumanMessage(_CONVERSATION_START_MARKER, id="m")
    ai = AIMessage("operator opener", id="a")
    # A history already led by the marker is user-first -> no second insertion.
    assert mw.before_model({"messages": [marker, ai]}, _RUNTIME) is None


def test_missing_ids_backfilled_before_rewrite():
    mw = LeadingUserMiddleware()
    ai = AIMessage("operator opener")
    human = HumanMessage("client reply")
    assert ai.id is None
    assert human.id is None
    update = mw.before_model({"messages": [ai, human]}, _RUNTIME)
    assert update is not None
    # Re-adding under REMOVE_ALL needs stable ids, so every message got one.
    assert ai.id is not None
    assert human.id is not None


async def test_abefore_model_reuses_sync_path():
    mw = LeadingUserMiddleware()
    msgs: list[AnyMessage] = [HumanMessage("hi", id="1")]
    assert await mw.abefore_model({"messages": msgs}) is None
