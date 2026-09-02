"""Completion-binding ownership across a NESTED tool dispatch.

The park-completion binding (``set_park_completion``) is the deferred-response DELIVERY ADDRESS
of one interaction, bound by the party that owns answering it — the conversation door binds it
around the turn it will answer. It rides a contextvar, so everything the turn dispatches inherits
it, and any OTHER driver that parks inside the turn captures the SAME address as its own: a flow
preset invoked as a tool inside an agent turn would post its own raw envelope into the guest's
thread while the agent's answer is orphaned, both fires racing for one delivery address.

THE OWNER OF THE INTERACTION OWNS DELIVERY. An agent that dispatches a tool is that owner: the
tool call is a STEP of the agent's turn, never a second answerer of it — whatever the tool
resolves to belongs to the agent, which folds it into the answer it alone delivers. So a tool an
agent dispatches runs under a CLEARED binding, while the agent's OWN park still captures the
binding the door set for it.

What clearing achieves is bounded, and the bound is worth stating. It stops the HIJACK: no nested
driver can address the guest thread this agent's answer is owed to. It does NOT make the nested
park the agent's. A nested driver that binds its own resume continuation — a flow preset is the
live example — owns that park end to end: it resumes on its own continuation and hands the
outcome wherever its own face delivers, which is not necessarily back into this agent's turn.
Whether an agent should ADOPT a park raised beneath it (suspending its own run until the nested
outcome lands) is a known gap with its own design item, not something this module decides.

Clearing is the whole mechanism — a nested driver decides for itself what an unwired completion
means (park refused loudly, or a park delivered through its own resume face). This module names
no driver and no delivery tool.

The reach is this plugin's own dispatch seams, and there is a boundary beyond them. A composite
toolbox tool an agent calls is covered, because the agent dispatches the composite and the whole
chain runs inside this scope. A chain or batch step that is itself a foreign parking driver,
assembled OUTSIDE an agent (a toolbox chain wired straight onto a conversation route), is the
same hazard class under a different owner and out of this plugin's reach — the scope has to be
applied by whoever dispatches the step.

The binding is cleared for the DISPATCH ONLY, so the agent's own park — raised by the graph
outside any tool body — still captures the completion the door bound for it, unchanged.
"""

from __future__ import annotations

import contextlib
import functools
import logging
from collections.abc import Iterable, Iterator
from typing import Any

from langchain_core.tools import BaseTool
from tai42_contract.interactions import reset_park_completion, set_park_completion

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def nested_tool_dispatch() -> Iterator[None]:
    """Run a nested tool dispatch with NO park-completion bound.

    Wraps the exact call that hands control to a foreign tool body, and restores the caller's
    own binding in a ``finally`` — the agent's park, raised outside this scope, still sees the
    completion the door bound for it."""
    token = set_park_completion()
    try:
        yield
    finally:
        reset_park_completion(token)


def scope_nested_dispatch[ToolT: BaseTool](tool: ToolT) -> ToolT:
    """``tool`` with its body wrapped in :func:`nested_tool_dispatch`.

    Applied to every tool an agent is given, whatever it was resolved from (a live object, a
    registered name, or a preset), so the ownership rule holds for the whole tool list rather
    than the one resolution path that happens to reach a parking driver today.

    Typed over any :class:`~langchain_core.tools.BaseTool` and returning the caller's OWN tool
    type: an agent whose list is ``list[StructuredTool]`` gets one back, while a bodyless subclass
    (a real input, see below) is not a type error at the call site.

    The wrapper is a body swap ON A COPY: the tool keeps its name, description, and advertised
    ``args_schema``, and the wrapped callables carry the original's ``__signature__`` /
    annotations, so nothing the model or the graph sees changes. A copy rather than an in-place
    mutation because these objects are NOT this agent's to edit — ``get_client_tools`` builds them
    over the shared tool registry and a caller may hand the same live ``StructuredTool`` to
    several agents, so wrapping in place would push one agent's scope onto every other holder
    (and re-wrap the same body on each resolution).

    The two callable slots are read defensively: a plain :class:`~langchain_core.tools.BaseTool`
    subclass implements ``_run``/``_arun`` and carries NEITHER attribute, so a direct read would
    raise rather than take the fallback below."""
    update: dict[str, Any] = {}
    func = getattr(tool, "func", None)
    coroutine = getattr(tool, "coroutine", None)
    if func is not None:
        update["func"] = _scoped_sync(func)
    if coroutine is not None:
        update["coroutine"] = _scoped_async(coroutine)
    if not update:
        # No callable body to swap (a ``BaseTool`` subclass implementing ``_run``/``_arun``): this
        # tool dispatches UNSCOPED, which is an ownership hole, so say so. Not an exception —
        # raising would take down hosts whose tools work fine today over a hazard that only
        # materializes if the tool turns out to host a parking driver.
        logger.warning(
            "agents: tool %r exposes no func/coroutine body to scope, so it dispatches without the "
            "park-completion binding cleared; a parking driver reached through it could capture this "
            "agent's deferred-answer address",
            getattr(tool, "name", tool),
        )
        return tool
    return tool.model_copy(update=update)


def scope_nested_dispatch_all[ToolT: BaseTool](tools: Iterable[ToolT]) -> list[ToolT]:
    """Every tool in ``tools``, delivery-scoped. Applied wherever an agent's tool list is
    ASSEMBLED — the last point before the list is handed to a graph — so the rule holds for
    every agent in the plugin, not only the ones that build their list through
    :func:`~tai42_agents._internal.resolve_tools.resolve_tools`."""
    return [scope_nested_dispatch(tool) for tool in tools]


def _scoped_sync(func: Any) -> Any:
    @functools.wraps(func)
    def scoped(*args: Any, **kwargs: Any) -> Any:
        with nested_tool_dispatch():
            return func(*args, **kwargs)

    return scoped


def _scoped_async(coroutine: Any) -> Any:
    @functools.wraps(coroutine)
    async def scoped(*args: Any, **kwargs: Any) -> Any:
        with nested_tool_dispatch():
            return await coroutine(*args, **kwargs)

    return scoped
