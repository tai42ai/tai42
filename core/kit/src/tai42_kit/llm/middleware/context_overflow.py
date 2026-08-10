from collections.abc import Sequence
from copy import deepcopy
from typing import Any, cast

from langchain.agents.middleware import (
    AgentMiddleware,
    AgentState,
    ContextEditingMiddleware,
    SummarizationMiddleware,
)
from langchain.agents.middleware.context_editing import ClearToolUsesEdit
from langchain_core.messages import AnyMessage
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.graph.message import Messages, add_messages
from langgraph.runtime import Runtime

from tai42_kit.llm.middleware.trimming import TrimmingMiddleware
from tai42_kit.llm.models import get_llm
from tai42_kit.llm.settings import (
    context_editing_settings,
    context_overflow_settings,
    llm_provider_settings,
    llm_settings,
    summarization_middleware_settings,
)

# Fixed execution order when several strategies are composed: reclaim tokens
# with cheap structural edits first, then fall back to LLM summarization.
_METHOD_ORDER = {"context_editing": 0, "summarization": 1, "trimming": 2}


def _build_middleware(method: str) -> AgentMiddleware:
    match method:
        case "trimming":
            return TrimmingMiddleware()

        case "summarization":
            settings = summarization_middleware_settings()
            # Summaries are written by the main agent LLM (same provider + config).
            model = get_llm(llm_provider_settings().llm, **llm_settings().model_dump(exclude_none=True))
            kwargs: dict[str, Any] = {
                "model": model,
                "trigger": ("tokens", settings.trigger_tokens),
                "keep": ("messages", settings.keep_messages),
                "trim_tokens_to_summarize": settings.trim_tokens_to_summarize,
            }
            if settings.summary_prompt:
                kwargs["summary_prompt"] = settings.summary_prompt
            return SummarizationMiddleware(**kwargs)

        case "context_editing":
            settings = context_editing_settings()
            return ContextEditingMiddleware(
                edits=[
                    ClearToolUsesEdit(
                        trigger=settings.trigger_tokens,
                        keep=settings.keep_tool_results,
                        clear_at_least=settings.clear_at_least,
                        clear_tool_inputs=settings.clear_tool_inputs,
                        exclude_tools=tuple(settings.exclude_tools),
                    )
                ]
            )

    raise ValueError(f"Unsupported context-overflow method: '{method}'")


def context_overflow_middlewares() -> list[AgentMiddleware]:
    """Build the configured context-overflow middleware(s).

    Reads ``CONTEXT_OVERFLOW_METHODS`` and returns the corresponding
    middlewares ordered by :data:`_METHOD_ORDER`, ready to spread into a
    ``create_agent`` ``middleware`` list (which runs every middleware hook
    automatically). For a raw ``StateGraph`` use :func:`areduce_context`.
    """
    methods = sorted(context_overflow_settings().methods, key=_METHOD_ORDER.__getitem__)
    return [_build_middleware(method) for method in methods]


async def areduce_context(
    messages: Sequence[AnyMessage],
    *,
    middlewares: Sequence[AgentMiddleware] | None = None,
    state: dict[str, Any] | None = None,
) -> list[AnyMessage] | None:
    """Apply the configured context-overflow strategies to ``messages``.

    For custom graphs that don't run ``create_agent``'s middleware pipeline.
    The two middleware hook families are reduced to one state mutation:

    - ``before_model`` strategies (trimming, summarization) are invoked via
      ``abefore_model`` and their message updates folded in.
    - ``context_editing`` (a ``wrap_model_call`` strategy) is applied as a
      persistent state edit via its public ``ContextEdit.apply``.

    Note the deliberate semantic difference for ``context_editing``: under
    ``create_agent`` it runs in ``wrap_model_call`` and the cleared messages are
    transient (the edit is discarded after each model call, full tool outputs
    stay in checkpoint state). Here the edit is persisted to graph state, so
    cleared tool outputs are dropped permanently — appropriate for a raw graph
    that has no per-call wrapper, and consistent with the "clear-tool-uses"
    intent of reclaiming the context window for good.

    Returns the reduced message list, or ``None`` when nothing changed (so the
    caller can skip writing state).
    """
    middlewares = middlewares if middlewares is not None else context_overflow_middlewares()
    base_state = state or {}
    # Our middlewares ignore runtime; create_agent supplies it, a raw graph does not.
    no_runtime = cast(Runtime[Any], None)

    current: list[AnyMessage] = list(messages)
    changed = False
    for middleware in middlewares:
        if isinstance(middleware, ContextEditingMiddleware):
            edited = deepcopy(current)
            for edit in middleware.edits:
                edit.apply(edited, count_tokens=count_tokens_approximately)
            if edited != current:
                current = edited
                changed = True
            continue

        hook_state = cast(AgentState, {**base_state, "messages": current})
        update = await middleware.abefore_model(hook_state, no_runtime)
        if update and update.get("messages"):
            current = cast(list[AnyMessage], add_messages(cast(Messages, current), update["messages"]))
            changed = True

    return current if changed else None
