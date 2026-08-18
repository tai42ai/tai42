import logging
import uuid
from collections.abc import Callable, Sequence
from typing import Any, cast

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import BaseMessage, HumanMessage, RemoveMessage, SystemMessage, trim_messages
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime

from tai42_kit.llm.settings import trimming_middleware_settings

logger = logging.getLogger(__name__)

_MAX_TOKENS_ENV = "TRIMMING_MIDDLEWARE_MAX_TOKENS"


class TrimmingBudgetTooSmallError(Exception):
    """The token budget cannot fit the per-run system prompt plus the newest human
    message.

    Trimming would drop the very turn being answered, leaving the model to reply
    from the system prompt alone — an unservable overflow. Carries the budget and
    measured token totals plus the env key that sets the budget, never the
    message text.
    """


class TrimmingMiddleware(AgentMiddleware):
    """Middleware that trims conversation history to fit within token limits.

    ``system_prompt`` is the per-run system prompt the graph applies at the
    model-call boundary; it never appears in graph state, yet it is part of every
    outgoing request. With ``include_system=True`` its tokens count against
    ``max_tokens``, so the history is trimmed to the budget minus the prompt;
    with ``include_system=False`` the budget covers the history alone.

    Invariant: the trimmed output always retains the newest human message of the
    input; a budget too small to keep it raises :class:`TrimmingBudgetTooSmallError`
    rather than silently answering without the current turn.
    """

    def __init__(
        self,
        system_prompt: SystemMessage | None = None,
        token_counter: Callable[[Sequence[BaseMessage]], int] = count_tokens_approximately,
        **trim_kwargs,
    ) -> None:
        super().__init__()
        self.system_prompt = system_prompt
        self.token_counter = token_counter
        settings = trimming_middleware_settings()
        self.trim_kwargs = settings.model_dump(exclude_none=True)
        self.trim_kwargs.update(trim_kwargs)

    def before_model(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        messages = state["messages"]
        for msg in messages:
            if msg.id is None:
                msg.id = str(uuid.uuid4())

        budget = self.trim_kwargs["max_tokens"]
        system_tokens = 0
        if self.system_prompt is not None and self.trim_kwargs["include_system"]:
            system_tokens = self.token_counter([self.system_prompt])
        history_budget = budget - system_tokens

        if history_budget > 0:
            trimmed = trim_messages(
                messages,
                token_counter=self.token_counter,
                **{**self.trim_kwargs, "max_tokens": history_budget},
            )
        else:
            # The system prompt alone exhausts the budget; no history can be kept.
            trimmed = []

        newest_human = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)
        if newest_human is not None and not any(m.id == newest_human.id for m in trimmed):
            required = system_tokens + self.token_counter([newest_human])
            total = system_tokens + self.token_counter(messages)
            counted = (
                "the per-run system prompt plus the newest human message"
                if system_tokens
                else "the newest human message"
            )
            raise TrimmingBudgetTooSmallError(
                f"Trimming budget of {budget} tokens cannot fit {counted} "
                f"({required} tokens needed) out of {total} input tokens; "
                f"raise {_MAX_TOKENS_ENV}."
            )

        if trimmed == messages:
            return None

        logger.warning(
            "Trimming dropped %d message(s) to fit the %d-token budget (%s)",
            len(messages) - len(trimmed),
            budget,
            _MAX_TOKENS_ENV,
        )

        return {
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                *trimmed,
            ]
        }

    async def abefore_model(self, state: AgentState, runtime: Runtime | None = None) -> dict[str, Any] | None:
        # Trimming is pure CPU (no I/O); reuse the sync implementation.
        return self.before_model(state, cast(Runtime, runtime))
