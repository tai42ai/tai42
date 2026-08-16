"""TrimmingMiddleware.before_model: id backfill, the newest-human invariant
(unservable-overflow raise + partial-trim warning), and the no-change
short-circuit. Plus the _build_middleware unsupported-method guard.
"""

import logging

import pytest

pytest.importorskip("langgraph")

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, RemoveMessage, SystemMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime

from tai42_kit.llm.middleware import context_overflow as co
from tai42_kit.llm.middleware import trimming as tr
from tai42_kit.llm.middleware.trimming import TrimmingBudgetTooSmallError, TrimmingMiddleware

# before_model ignores the runtime (noqa ARG002); a default-constructed Runtime
# satisfies the parameter type without needing a live graph execution context.
_RUNTIME: Runtime[None] = Runtime()


def test_before_model_backfills_missing_ids_and_partial_trims():
    mw = TrimmingMiddleware(max_tokens=40)
    msgs: list[AnyMessage] = [
        HumanMessage("a " * 40),
        AIMessage("b " * 40),
        HumanMessage("c " * 5),
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
    # The newest human message survives the trim.
    assert msgs[-1].id in {m.id for m in update["messages"][1:]}


def test_before_model_no_change_returns_none():
    mw = TrimmingMiddleware(max_tokens=100_000)
    msgs: list[AnyMessage] = [HumanMessage("hi", id="1")]
    # History already fits -> trimming is a no-op, signalled by None.
    assert mw.before_model({"messages": msgs}, _RUNTIME) is None


def test_under_budget_input_is_untouched_without_warning(caplog):
    mw = TrimmingMiddleware(max_tokens=100_000)
    msgs: list[AnyMessage] = [
        SystemMessage("you are a helper", id="s"),
        HumanMessage("hi", id="1"),
    ]
    with caplog.at_level(logging.WARNING, logger="tai42_kit.llm.middleware.trimming"):
        assert mw.before_model({"messages": msgs}, _RUNTIME) is None
    assert caplog.records == []


def test_partial_trim_keeps_newest_human_and_warns(caplog):
    mw = TrimmingMiddleware(max_tokens=30)
    msgs: list[AnyMessage] = [
        HumanMessage("a " * 40, id="1"),
        AIMessage("b " * 40, id="2"),
        HumanMessage("c " * 5, id="3"),
    ]
    with caplog.at_level(logging.WARNING, logger="tai42_kit.llm.middleware.trimming"):
        update = mw.before_model({"messages": msgs}, _RUNTIME)
    assert update is not None
    kept_ids = {m.id for m in update["messages"][1:]}
    # Newest human kept, older turns dropped.
    assert "3" in kept_ids
    assert "1" not in kept_ids
    assert "2" not in kept_ids
    # The partial trim is announced, naming the count and the budget.
    assert len(caplog.records) == 1
    warning = caplog.records[0].getMessage()
    assert "dropped 2 message" in warning
    assert "30-token budget" in warning
    assert "TRIMMING_MIDDLEWARE_MAX_TOKENS" in warning


def test_overflow_dropping_newest_human_raises_named_error():
    mw = TrimmingMiddleware(max_tokens=10)
    human_text = "UNIQUEHUMANTOKEN " * 20
    msgs: list[AnyMessage] = [
        SystemMessage("s " * 50, id="s"),
        HumanMessage(human_text, id="h"),
    ]
    with pytest.raises(TrimmingBudgetTooSmallError) as excinfo:
        mw.before_model({"messages": msgs}, _RUNTIME)
    message = str(excinfo.value)
    # Budget + env key present for an operator; the message text never leaks.
    assert "10" in message
    assert "TRIMMING_MIDDLEWARE_MAX_TOKENS" in message
    assert "UNIQUEHUMANTOKEN" not in message


def test_overflow_required_counts_only_first_system(monkeypatch):
    # include_system=True force-keeps only the first system message, so the
    # reported requirement is first-system + newest-human, never every system.
    from langchain_core.messages.utils import count_tokens_approximately

    mw = TrimmingMiddleware(max_tokens=10)
    msgs: list[AnyMessage] = [
        SystemMessage("first " * 50, id="s1"),
        SystemMessage("second " * 50, id="s2"),
        HumanMessage("UNIQUEHUMANTOKEN " * 20, id="h"),
    ]
    with pytest.raises(TrimmingBudgetTooSmallError) as excinfo:
        mw.before_model({"messages": msgs}, _RUNTIME)
    message = str(excinfo.value)
    first_only = count_tokens_approximately([msgs[0], msgs[2]])
    all_systems = count_tokens_approximately([msgs[0], msgs[1], msgs[2]])
    assert f"{first_only} tokens needed" in message
    assert f"{all_systems} tokens needed" not in message


def test_full_wipe_with_human_present_raises(monkeypatch):
    # The wipe-everything case is subsumed by the invariant raise: an empty
    # output while a human message was present is an unservable overflow.
    mw = TrimmingMiddleware(max_tokens=10)
    monkeypatch.setattr(tr, "trim_messages", lambda messages, **kw: [])
    msgs: list[AnyMessage] = [HumanMessage("x", id="1")]
    with pytest.raises(TrimmingBudgetTooSmallError):
        mw.before_model({"messages": msgs}, _RUNTIME)


def test_history_without_human_message_does_not_raise(caplog):
    # System-only / ai-only histories have no newest human to protect; the
    # invariant must not fire spuriously.
    mw = TrimmingMiddleware(max_tokens=100_000)
    system_only: list[AnyMessage] = [SystemMessage("you are a helper", id="s")]
    ai_only: list[AnyMessage] = [AIMessage("prior answer", id="a")]
    with caplog.at_level(logging.WARNING, logger="tai42_kit.llm.middleware.trimming"):
        assert mw.before_model({"messages": system_only}, _RUNTIME) is None
        assert mw.before_model({"messages": ai_only}, _RUNTIME) is None
    assert caplog.records == []


async def test_abefore_model_reuses_sync_path():
    mw = TrimmingMiddleware(max_tokens=100_000)
    msgs: list[AnyMessage] = [HumanMessage("hi", id="1")]
    assert await mw.abefore_model({"messages": msgs}) is None


def test_build_middleware_rejects_unknown_method():
    with pytest.raises(ValueError, match="Unsupported context-overflow method"):
        co._build_middleware("bogus")
