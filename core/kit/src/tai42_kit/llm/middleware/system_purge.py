import uuid
from typing import Any, cast

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import RemoveMessage, SystemMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime


class SystemPurgeMiddleware(AgentMiddleware):
    """Keep system messages out of the thread's checkpointed history.

    The system prompt is per-run model configuration — supplied via the graph
    factory (``create_agent(system_prompt=...)``) or prepended explicitly at the
    model call — never conversation state. A thread whose stored history carries
    a ``SystemMessage`` would replay it alongside the per-run prompt, and
    strict-ordering providers reject a history with multiple non-consecutive
    system messages; any stored system message is therefore removed from state
    before the model call. A no-op on a system-free history.
    """

    def before_model(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        messages = state["messages"]
        if not any(isinstance(message, SystemMessage) for message in messages):
            return None

        for message in messages:
            if message.id is None:
                message.id = str(uuid.uuid4())

        return {
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                *(message for message in messages if not isinstance(message, SystemMessage)),
            ]
        }

    async def abefore_model(self, state: AgentState, runtime: Runtime | None = None) -> dict[str, Any] | None:
        # Pure list rewrite (no I/O); reuse the sync implementation.
        return self.before_model(state, cast(Runtime, runtime))
