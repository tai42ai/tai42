"""SystemPurgeMiddleware.before_model: remove stored system messages from state
before the model call, no-op on a system-free history.
"""

import pytest

pytest.importorskip("langgraph")

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, RemoveMessage, SystemMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime

from tai42_kit.llm.middleware.system_purge import SystemPurgeMiddleware

# before_model ignores the runtime; a default-constructed Runtime satisfies the
# parameter type without a live graph execution context.
_RUNTIME: Runtime[None] = Runtime()


def test_stored_system_messages_are_removed():
    mw = SystemPurgeMiddleware()
    system = SystemMessage("rules", id="s")
    human = HumanMessage("hi", id="h")
    ai = AIMessage("there", id="a")
    update = mw.before_model({"messages": [system, human, ai]}, _RUNTIME)

    assert update is not None
    out = update["messages"]
    assert isinstance(out[0], RemoveMessage)
    assert out[0].id == REMOVE_ALL_MESSAGES
    # The non-system messages survive in order; the system message is gone.
    assert out[1:] == [human, ai]


def test_interior_system_message_is_removed():
    mw = SystemPurgeMiddleware()
    human = HumanMessage("hi", id="h")
    system = SystemMessage("rules", id="s")
    ai = AIMessage("there", id="a")
    update = mw.before_model({"messages": [human, system, ai]}, _RUNTIME)

    assert update is not None
    assert update["messages"][1:] == [human, ai]


def test_system_free_history_is_untouched():
    mw = SystemPurgeMiddleware()
    msgs: list[AnyMessage] = [HumanMessage("hi", id="1"), AIMessage("there", id="2")]
    assert mw.before_model({"messages": msgs}, _RUNTIME) is None


def test_empty_history_is_untouched():
    mw = SystemPurgeMiddleware()
    assert mw.before_model({"messages": []}, _RUNTIME) is None


def test_missing_ids_backfilled_before_rewrite():
    mw = SystemPurgeMiddleware()
    system = SystemMessage("rules")
    human = HumanMessage("hi")
    update = mw.before_model({"messages": [system, human]}, _RUNTIME)
    assert update is not None
    # Re-adding under REMOVE_ALL needs stable ids, so every kept message got one.
    assert human.id is not None


async def test_abefore_model_reuses_sync_path():
    mw = SystemPurgeMiddleware()
    system = SystemMessage("rules", id="s")
    human = HumanMessage("hi", id="h")
    update = await mw.abefore_model({"messages": [system, human]})
    assert update is not None
    assert update["messages"][1:] == [human]
