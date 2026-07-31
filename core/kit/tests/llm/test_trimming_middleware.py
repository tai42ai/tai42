"""TrimmingMiddleware.before_model: id backfill, the all-trimmed placeholder,
and the no-change short-circuit. Plus the _build_middleware unsupported-method
guard.
"""

import pytest

pytest.importorskip("langgraph")

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, RemoveMessage, SystemMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime

from tai42_kit.llm.middleware import context_overflow as co
from tai42_kit.llm.middleware import trimming as tr
from tai42_kit.llm.middleware.trimming import TrimmingMiddleware

# before_model ignores the runtime (noqa ARG002); a default-constructed Runtime
# satisfies the parameter type without needing a live graph execution context.
_RUNTIME: Runtime[None] = Runtime()


def test_before_model_backfills_missing_ids_and_trims():
    mw = TrimmingMiddleware(max_tokens=15)
    msgs: list[AnyMessage] = [
        HumanMessage("aaaa " * 30),
        AIMessage("bbbb " * 30),
        HumanMessage("cccc " * 30),
    ]
    # None of these have ids yet.
    assert all(m.id is None for m in msgs)
    update = mw.before_model({"messages": msgs}, _RUNTIME)
    # Every message got an id assigned in place.
    assert all(m.id is not None for m in msgs)
    # A trim happened -> a full-clear directive plus the trimmed tail.
    assert update is not None
    assert isinstance(update["messages"][0], RemoveMessage)
    assert update["messages"][0].id == REMOVE_ALL_MESSAGES


def test_before_model_no_change_returns_none():
    mw = TrimmingMiddleware(max_tokens=100_000)
    msgs: list[AnyMessage] = [HumanMessage("hi", id="1")]
    # History already fits -> trimming is a no-op, signalled by None.
    assert mw.before_model({"messages": msgs}, _RUNTIME) is None


def test_before_model_all_trimmed_substitutes_placeholder(monkeypatch):
    mw = TrimmingMiddleware(max_tokens=10)
    monkeypatch.setattr(tr, "trim_messages", lambda messages, **kw: [])
    msgs: list[AnyMessage] = [HumanMessage("x", id="1")]
    update = mw.before_model({"messages": msgs}, _RUNTIME)
    assert update is not None
    placeholder = update["messages"][1]
    assert isinstance(placeholder, SystemMessage)
    assert "No valid messages" in placeholder.content


async def test_abefore_model_reuses_sync_path():
    mw = TrimmingMiddleware(max_tokens=100_000)
    msgs: list[AnyMessage] = [HumanMessage("hi", id="1")]
    assert await mw.abefore_model({"messages": msgs}) is None


def test_build_middleware_rejects_unknown_method():
    with pytest.raises(ValueError, match="Unsupported context-overflow method"):
        co._build_middleware("bogus")
