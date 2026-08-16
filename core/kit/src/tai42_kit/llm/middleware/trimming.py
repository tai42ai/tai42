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
    """The token budget cannot fit the system prompt plus the newest human message.

    Trimming would drop the very turn being answered, leaving the model to reply
    from the system prompt alone — an unservable overflow. Carries the budget and
    measured token totals plus the env key that sets the budget, never the
    message text.
    """


class TrimmingMiddleware(AgentMiddleware):
    """Middleware that trims conversation history to fit within token limits.

    Invariant: the trimmed output always retains the newest human message of the
    input; a budget too small to keep it raises :class:`TrimmingBudgetTooSmallError`
    rather than silently answering without the current turn.
    """

    def __init__(
        self,
        token_counter: Callable[[Sequence[BaseMessage]], int] = count_tokens_approximately,
        **trim_kwargs,
    ) -> None:
        super().__init__()
        self.token_counter = token_counter
        settings = trimming_middleware_settings()
        self.trim_kwargs = settings.model_dump(exclude_none=True)
        self.trim_kwargs.update(trim_kwargs)

    def before_model(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        messages = state["messages"]
        for msg in messages:
            if msg.id is None:
                msg.id = str(uuid.uuid4())

        trimmed = trim_messages(
            messages,
            token_counter=self.token_counter,
            **self.trim_kwargs,
        )

        budget = self.trim_kwargs["max_tokens"]
        newest_human = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)
        if newest_human is not None and not any(m.id == newest_human.id for m in trimmed):
            first_system = next((m for m in messages if isinstance(m, SystemMessage)), None)
            survivors: list[BaseMessage] = [first_system] if first_system is not None else []
            survivors.append(newest_human)
            required = self.token_counter(survivors)
            total = self.token_counter(messages)
            raise TrimmingBudgetTooSmallError(
                f"Trimming budget of {budget} tokens cannot fit the system prompt plus the "
                f"newest human message ({required} tokens needed) out of {total} input tokens; "
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
