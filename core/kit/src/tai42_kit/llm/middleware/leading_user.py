"""Middleware ensuring an outgoing request opens with a user message for a provider that needs it."""

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage

#: Inert first user turn added to the outgoing request when its first non-system message is
#: an assistant message (an operator-initiated thread can legitimately open with one). The
#: text carries no instruction, so it cannot steer the model.
_CONVERSATION_START_MARKER = "[conversation start]"


def lead_with_user(messages: list[AnyMessage]) -> list[AnyMessage] | None:
    """The messages with a marker user turn ahead of a leading assistant message, or ``None``.

    The marker sits after any leading system messages. ``None`` means the first non-system
    message is not an assistant message (or there is none): send the input unchanged. Never
    mutates the input messages.
    """
    first = next((i for i, m in enumerate(messages) if not isinstance(m, SystemMessage)), None)
    if first is None or not isinstance(messages[first], AIMessage):
        return None
    return [*messages[:first], HumanMessage(content=_CONVERSATION_START_MARKER), *messages[first:]]


class LeadingUserMiddleware(AgentMiddleware):
    """Ensure the outgoing request's first non-system message is a user message.

    Request-scoped: the marker is added to the outgoing request only and never written to the
    thread. It runs at the model call, so a trim that dropped the first user turn is covered.
    """

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelCallResult[Any]:
        """Lead ``request.messages`` with a user turn before the synchronous model call."""
        led = lead_with_user(request.messages)
        if led is not None:
            request = request.override(messages=led)
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelCallResult[Any]:
        """Lead ``request.messages`` with a user turn before the async model call."""
        led = lead_with_user(request.messages)
        if led is not None:
            request = request.override(messages=led)
        return await handler(request)
