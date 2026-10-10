"""LLM middleware that shapes the message list before a model call."""

from langchain.agents.middleware import AgentMiddleware

from tai42_kit.llm.middleware.leading_user import LeadingUserMiddleware
from tai42_kit.llm.middleware.rolling_cache_mark import RollingCacheMarkMiddleware
from tai42_kit.llm.models import cache_mark_key, requires_leading_user_turn


def history_middlewares(provider: str) -> list[AgentMiddleware]:
    """The request-shaping middlewares ``provider`` needs on an agent's history.

    A :class:`LeadingUserMiddleware` when :func:`requires_leading_user_turn` says so, then a
    :class:`RollingCacheMarkMiddleware` keyed on :func:`cache_mark_key` when the provider
    takes a per-block cache mark. An unknown provider raises ``ValueError``.
    """
    stack: list[AgentMiddleware] = []
    if requires_leading_user_turn(provider):
        stack.append(LeadingUserMiddleware())
    key = cache_mark_key(provider)
    if key:
        stack.append(RollingCacheMarkMiddleware(mark_key=key))
    return stack


__all__ = ["history_middlewares"]
