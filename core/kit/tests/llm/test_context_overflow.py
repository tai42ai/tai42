import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("langgraph")

from langchain.agents.middleware import (
    ContextEditingMiddleware,
    SummarizationMiddleware,
)
from langchain.agents.middleware.context_editing import ClearToolUsesEdit
from langchain.agents.middleware.summarization import DEFAULT_SUMMARY_PROMPT
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from tai42_kit.llm.middleware import context_overflow as co
from tai42_kit.llm.middleware.context_overflow import (
    areduce_context,
    context_overflow_middlewares,
)
from tai42_kit.llm.middleware.trimming import TrimmingMiddleware


def _summary_model(text: str = "SUMMARY") -> MagicMock:
    """A fake chat model whose async invoke returns a fixed summary."""
    model = MagicMock()

    async def ainvoke(*_args, **_kwargs):
        return SimpleNamespace(text=text)

    model.ainvoke = ainvoke
    return model


@contextmanager
def _patch_summary_build(*, main_config=None, provider="openai", get_llm=None):
    """Patch the summary-LLM build chain used by the 'summarization' factory case.

    Summaries are written by the main agent LLM:
    get_llm(llm_provider_settings().llm, **llm_settings().model_dump()). Yields the
    get_llm mock so callers can assert on the resolved (provider, **kwargs).
    """
    get_llm = get_llm if get_llm is not None else MagicMock(return_value=_summary_model())
    main = SimpleNamespace(model_dump=lambda exclude_none: dict(main_config or {"model": "gpt-4o", "temperature": 0.0}))
    prov = SimpleNamespace(llm=provider)
    with (
        patch.object(co, "get_llm", get_llm),
        patch.object(co, "llm_settings", return_value=main),
        patch.object(co, "llm_provider_settings", return_value=prov),
    ):
        yield get_llm


def _tool_heavy_history() -> list:
    return [
        HumanMessage("hi", id="1"),
        AIMessage("", id="2", tool_calls=[{"id": "t1", "name": "foo", "args": {}}]),
        ToolMessage("X" * 400, id="3", tool_call_id="t1"),
        AIMessage("", id="4", tool_calls=[{"id": "t2", "name": "foo", "args": {}}]),
        ToolMessage("Y" * 400, id="5", tool_call_id="t2"),
        HumanMessage("thanks", id="6"),
    ]


class TestMiddlewareFactory:
    def _settings(self, methods):
        return SimpleNamespace(methods=methods)

    def test_default_method_builds_trimming(self):
        with patch.object(co, "context_overflow_settings", return_value=self._settings(["trimming"])):
            mws = context_overflow_middlewares()
        assert [type(m).__name__ for m in mws] == ["TrimmingMiddleware"]

    def test_composed_methods_ordered_editing_before_summarization(self):
        # Input order is reversed; factory must reorder to canonical order.
        settings = self._settings(["summarization", "context_editing"])
        with (
            patch.object(co, "context_overflow_settings", return_value=settings),
            _patch_summary_build(),
        ):
            mws = context_overflow_middlewares()
        assert [type(m).__name__ for m in mws] == [
            "ContextEditingMiddleware",
            "SummarizationMiddleware",
        ]

    def _summarization_settings(self, summary_prompt):
        return SimpleNamespace(
            trigger_tokens=5000,
            keep_messages=20,
            trim_tokens_to_summarize=4000,
            summary_prompt=summary_prompt,
        )

    def test_summary_prompt_falls_back_to_default_when_unset(self):
        with (
            patch.object(co, "context_overflow_settings", return_value=self._settings(["summarization"])),
            patch.object(co, "summarization_middleware_settings", return_value=self._summarization_settings(None)),
            _patch_summary_build(),
        ):
            mws = context_overflow_middlewares()
        assert isinstance(mws[0], SummarizationMiddleware)
        assert mws[0].summary_prompt == DEFAULT_SUMMARY_PROMPT

    def test_summary_prompt_passed_through_when_set(self):
        smw = self._summarization_settings("CUSTOM PROMPT")
        with (
            patch.object(co, "context_overflow_settings", return_value=self._settings(["summarization"])),
            patch.object(co, "summarization_middleware_settings", return_value=smw),
            _patch_summary_build(),
        ):
            mws = context_overflow_middlewares()
        assert isinstance(mws[0], SummarizationMiddleware)
        assert mws[0].summary_prompt == "CUSTOM PROMPT"


class TestAreduceContext:
    def test_no_change_returns_none(self):
        # Trigger far above this tiny history -> nothing to do.
        mw = ContextEditingMiddleware(edits=[ClearToolUsesEdit(trigger=1_000_000)])
        msgs = _tool_heavy_history()
        result = asyncio.run(areduce_context(msgs, middlewares=[mw]))
        assert result is None

    def test_context_editing_clears_oldest_tool_output(self):
        mw = ContextEditingMiddleware(edits=[ClearToolUsesEdit(trigger=20, keep=1)])
        msgs = _tool_heavy_history()
        result = asyncio.run(areduce_context(msgs, middlewares=[mw]))
        assert result is not None
        by_id = {m.id: m for m in result}
        assert by_id["3"].content == "[cleared]"  # oldest tool result cleared
        assert by_id["5"].content == "Y" * 400  # most recent tool result kept

    def test_trimming_drops_old_messages(self):
        mw = TrimmingMiddleware(max_tokens=15)
        msgs = [
            HumanMessage("aaaa " * 20, id=str(i)) if i % 2 == 0 else AIMessage("bbbb " * 20, id=str(i))
            for i in range(8)
        ]
        result = asyncio.run(areduce_context(msgs, middlewares=[mw]))
        assert result is not None
        assert len(result) < len(msgs)

    def test_summarization_replaces_history_with_summary(self):
        mw = SummarizationMiddleware(
            model=_summary_model("CONDENSED"),
            trigger=("tokens", 10),
            keep=("messages", 2),
        )
        msgs = [
            HumanMessage("hello " * 10, id=str(i)) if i % 2 == 0 else AIMessage("world " * 10, id=str(i))
            for i in range(8)
        ]
        result = asyncio.run(areduce_context(msgs, middlewares=[mw]))
        assert result is not None
        assert "CONDENSED" in result[0].content
        # recent messages preserved verbatim after the summary
        assert result[-1].id == "7"

    def test_composed_editing_then_summarization(self):
        editing = ContextEditingMiddleware(edits=[ClearToolUsesEdit(trigger=20, keep=1)])
        summarizing = SummarizationMiddleware(
            model=_summary_model("CONDENSED"),
            trigger=("tokens", 10),
            keep=("messages", 2),
        )
        result = asyncio.run(areduce_context(_tool_heavy_history(), middlewares=[editing, summarizing]))
        assert result is not None
        assert "CONDENSED" in result[0].content


class TestSummaryModelUsesMainLLM:
    """The 'summarization' factory case builds its summarizer from the main agent
    LLM (same provider + config) — no separate summary-LLM settings."""

    def test_summary_model_built_from_main_llm(self):
        with (
            patch.object(co, "context_overflow_settings", return_value=SimpleNamespace(methods=["summarization"])),
            patch.object(
                co,
                "summarization_middleware_settings",
                return_value=SimpleNamespace(
                    trigger_tokens=5000, keep_messages=20, trim_tokens_to_summarize=4000, summary_prompt=None
                ),
            ),
            _patch_summary_build(main_config={"model": "gpt-4o", "temperature": 0.0}, provider="openai") as get_llm,
        ):
            context_overflow_middlewares()
        get_llm.assert_called_once_with("openai", model="gpt-4o", temperature=0.0)
