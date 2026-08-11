import uuid
from typing import Any, cast

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime

#: Inert first user turn prepended when the outgoing history's first non-system
#: message is an assistant message. Strict-ordering providers (the Anthropic
#: Messages API) require the first non-system message to have role ``user`` and
#: reject a leading assistant message; an operator-initiated thread can
#: legitimately open with one. The text carries no instruction, so it cannot steer
#: the model.
_CONVERSATION_START_MARKER = "[conversation start]"


class LeadingUserMiddleware(AgentMiddleware):
    """Ensure the outgoing history's first non-system message is a user message.

    A no-op on the common user-first history. When the first non-system message is
    an ``AIMessage``, a single :data:`_CONVERSATION_START_MARKER` user message is
    inserted ahead of it (after any leading system messages), so strict-ordering
    providers accept the turn. Idempotent: the inserted marker leads on the next
    turn and the check short-circuits.
    """

    def before_model(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        messages = state["messages"]
        first = next((i for i, m in enumerate(messages) if not isinstance(m, SystemMessage)), None)
        if first is None or not isinstance(messages[first], AIMessage):
            return None

        for msg in messages:
            if msg.id is None:
                msg.id = str(uuid.uuid4())

        marker = HumanMessage(content=_CONVERSATION_START_MARKER, id=str(uuid.uuid4()))
        return {
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                *messages[:first],
                marker,
                *messages[first:],
            ]
        }

    async def abefore_model(self, state: AgentState, runtime: Runtime | None = None) -> dict[str, Any] | None:
        # Pure list rewrite (no I/O); reuse the sync implementation.
        return self.before_model(state, cast(Runtime, runtime))
