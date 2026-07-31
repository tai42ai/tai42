import logging
import uuid
from collections.abc import Callable
from typing import Any, cast

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import BaseMessage, RemoveMessage, SystemMessage, trim_messages
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime

from tai42_kit.llm.settings import trimming_middleware_settings

logger = logging.getLogger(__name__)


class TrimmingMiddleware(AgentMiddleware):
    """Middleware that trims conversation history to fit within token limits."""

    def __init__(
        self,
        token_counter: Callable[[list[BaseMessage]], int] = count_tokens_approximately,
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

        if trimmed:
            new_messages = trimmed
        else:
            # The token limit wiped the whole history; an agent cannot run on an
            # empty list, so substitute a placeholder. Logged so a too-low limit
            # is observable rather than silently masked.
            logger.warning("Trimming removed all messages; substituting a placeholder system message")
            new_messages = [SystemMessage(content="No valid messages after trimming.")]

        if new_messages == messages:
            return None

        return {
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                *new_messages,
            ]
        }

    async def abefore_model(self, state: AgentState, runtime: Runtime | None = None) -> dict[str, Any] | None:
        # Trimming is pure CPU (no I/O); reuse the sync implementation.
        return self.before_model(state, cast(Runtime, runtime))
