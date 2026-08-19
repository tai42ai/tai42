from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import AnyMessage


def _is_mark(block: Any) -> bool:
    """A content block carrying a prompt-caching breakpoint (a ``cache_control`` key)."""
    return isinstance(block, dict) and "cache_control" in block


def _strip_blocks(content: list[Any]) -> list[Any] | str:
    """Drop the ``cache_control`` key from every marked block in ``content``.

    A block reduced to a bare ``{"type": "text", "text": ...}`` collapses to its
    plain string; a whole content list left as a single string collapses to that
    string, keeping the wire form minimal.
    """
    stripped: list[Any] = []
    for block in content:
        if _is_mark(block):
            block = {key: value for key, value in block.items() if key != "cache_control"}
            if set(block) == {"type", "text"} and block["type"] == "text":
                block = block["text"]
        stripped.append(block)
    if len(stripped) == 1 and isinstance(stripped[0], str):
        return stripped[0]
    return stripped


def roll_cache_marks(messages: list[AnyMessage]) -> list[AnyMessage] | None:
    """Keep the ``cache_control`` mark only on the last message that carries one,
    stripping it from every earlier message.

    Returns a new message list (earlier marked messages replaced by stripped copies,
    the rest shared by reference) or ``None`` when history carries zero or one mark
    — nothing to roll. Never mutates the input messages: the checkpointed state
    objects are left untouched so the rewrite stays confined to the outgoing request.

    Both consumers rely on the ``None`` return meaning "nothing to roll, send the
    input unchanged": :class:`RollingCacheMarkMiddleware` skips its ``request.override``
    on ``None``, and a raw graph node whose ``wrap_model_call`` middleware never runs
    calls this directly and keeps its own message list on ``None``.
    """
    last_marked = None
    for index, message in enumerate(messages):
        content = message.content
        if isinstance(content, list) and any(_is_mark(block) for block in content):
            last_marked = index
    if last_marked is None:
        return None

    rolled = list(messages)
    changed = False
    for index in range(last_marked):
        message = messages[index]
        content = message.content
        if not isinstance(content, list) or not any(_is_mark(block) for block in content):
            continue
        rolled[index] = message.model_copy(update={"content": _strip_blocks(content)})
        changed = True
    return rolled if changed else None


class RollingCacheMarkMiddleware(AgentMiddleware):
    """Keep exactly one rolling user-side ``cache_control`` breakpoint at the model call.

    Per-turn marking on a checkpointed thread persists a ``cache_control`` block into
    history on every turn, and the provider caps breakpoints per request (Anthropic:
    4), so an unbounded thread would exceed the cap. This strips every ``cache_control``
    key but the newest before the provider call, so a growing history always sends one
    user-side breakpoint and the whole stable prefix stays cacheable.

    The rewrite is request-scoped through ``wrap_model_call`` — it never persists into
    checkpoint state, so history keeps its per-turn marks and each turn re-rolls them.
    The system message's own mark is per-run configuration on ``request.system_message``,
    not in ``request.messages``, so it is untouched. Inert when history carries zero or
    one mark.
    """

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelCallResult[Any]:
        rolled = roll_cache_marks(request.messages)
        if rolled is not None:
            request = request.override(messages=rolled)
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelCallResult[Any]:
        rolled = roll_cache_marks(request.messages)
        if rolled is not None:
            request = request.override(messages=rolled)
        return await handler(request)
