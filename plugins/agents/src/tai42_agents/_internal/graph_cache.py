"""The compiled tools-agent graph, cached per event loop and reused across runs.

A run's graph depends only on its inputs (tool names, presets, rendered system message,
structured-output format, model and checkpoint providers), the tool surface it resolved its
tools from, the serving epoch and the debug flag; per-run state stays per run (messages,
config, the re-prompt counters bound per drive). So a run whose inputs match a compiled graph
of this loop reuses it. The key carries the tool-surface generation and the client epoch read
BEFORE the build, so a graph built from a surface or an epoch that has since changed is never
served again. A run carrying caller-built tool objects, or an input with no canonical form,
is compiled fresh and not stored. Only a successful build is stored.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
from asyncio import AbstractEventLoop
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any
from weakref import WeakKeyDictionary

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, SecretStr
from tai42_contract.agent.base import PresetSpec
from tai42_contract.app import tai42_app
from tai42_contract.secrets import SecretValue
from tai42_kit.clients.base import current_client_epoch
from tai42_kit.logging.settings import logging_settings
from tai42_kit.settings import register_settings_reset

from tai42_agents._internal.base_tool_agent import _compile_tools_agent
from tai42_agents._internal.resolve_tools import resolve_tools
from tai42_agents.settings import agents_limits_settings


@dataclass(frozen=True)
class ToolsAgentGraphSpec:
    """The inputs a tools-agent graph is compiled from."""

    tool_names: tuple[str, ...] = ()
    presets: tuple[PresetSpec, ...] = ()
    live_tools: tuple[StructuredTool, ...] = ()
    system_message: str = ""
    system_content_kwargs: dict[str, Any] | None = None
    response_format: Any = None
    llm_provider: str | None = None
    llm_kwargs: dict[str, Any] | None = None
    checkpoint_provider: str | None = None


@dataclass(frozen=True)
class ToolsAgentGraph:
    """A compiled tools-agent graph: its strategy, the tools it binds and its checkpoint provider."""

    agent: Any
    strategy: Any
    tools: tuple[StructuredTool, ...]
    response_format: Any
    checkpoint_provider: str | None


class _UnkeyableError(Exception):
    """An input with no canonical, collision-free key form."""


def _secret_digest(plaintext: Any) -> tuple[str, str]:
    return ("secret", hashlib.sha256(repr(_canonical(plaintext)).encode("utf-8")).hexdigest())


def _canonical(value: Any) -> Any:
    """A hashable, type-tagged form of ``value``; equal forms mean equal compile inputs.

    Secrets enter as a digest of their plaintext, never the plaintext. A pydantic model class
    (a resolved ``response_format``) enters by identity. Anything else that is not plain data
    raises :class:`_UnkeyableError`.
    """
    if value is None or isinstance(value, bool | int | float | str):
        return (type(value).__name__, value)
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise _UnkeyableError
        return ("dict", tuple(sorted((key, _canonical(item)) for key, item in value.items())))
    if isinstance(value, list | tuple):
        return (type(value).__name__, tuple(_canonical(item) for item in value))
    if isinstance(value, SecretStr):
        return _secret_digest(value.get_secret_value())
    if isinstance(value, SecretValue):
        return _secret_digest(value.reveal())
    if isinstance(value, type) and issubclass(value, BaseModel):
        return ("class", value.__module__, value.__qualname__, id(value))
    raise _UnkeyableError


def _spec_key(spec: ToolsAgentGraphSpec) -> tuple[Any, ...] | None:
    """The spec's cache key, or ``None`` when it must be compiled fresh."""
    if spec.live_tools:
        return None
    try:
        return (
            _canonical(list(spec.tool_names)),
            _canonical(
                [
                    {
                        "name": preset.name,
                        "description": preset.description,
                        "base_tool": preset.base_tool,
                        "fixed_kwargs": preset.fixed_kwargs,
                    }
                    for preset in spec.presets
                ]
            ),
            _canonical(spec.system_message),
            _canonical(spec.system_content_kwargs),
            _canonical(spec.response_format),
            _canonical(spec.llm_provider),
            _canonical(spec.llm_kwargs),
            _canonical(spec.checkpoint_provider),
        )
    except _UnkeyableError:
        return None


# Per loop: a compiled graph holds that loop's checkpointer. A graph also keeps its loop
# referenced, so a closed loop's map is dropped explicitly when the next graph is stored.
_graphs: WeakKeyDictionary[AbstractEventLoop, OrderedDict[tuple[Any, ...], ToolsAgentGraph]] = WeakKeyDictionary()
_graphs_lock = threading.Lock()


async def _build(spec: ToolsAgentGraphSpec) -> ToolsAgentGraph:
    tools = await resolve_tools(tai42_app.tools, list(spec.tool_names), list(spec.live_tools), list(spec.presets))
    agent, strategy = await _compile_tools_agent(
        tools,
        llm_provider=spec.llm_provider,
        checkpoint_provider=spec.checkpoint_provider,
        llm_kwargs=spec.llm_kwargs,
        response_format=spec.response_format,
        system_message=spec.system_message,
        system_content_kwargs=spec.system_content_kwargs,
    )
    return ToolsAgentGraph(
        agent=agent,
        strategy=strategy,
        tools=tuple(tools),
        response_format=spec.response_format,
        checkpoint_provider=spec.checkpoint_provider,
    )


async def tools_agent_graph(spec: ToolsAgentGraphSpec) -> ToolsAgentGraph:
    """This loop's compiled graph for ``spec``, compiled and stored on a miss."""
    key = _spec_key(spec)
    if key is None:
        return await _build(spec)
    full_key = (
        key,
        tai42_app.tools.surface_generation(),
        current_client_epoch(),
        logging_settings().is_enabled_for("DEBUG"),
    )
    loop = asyncio.get_running_loop()
    with _graphs_lock:
        held = _graphs.get(loop)
        if held is not None and (graph := held.get(full_key)) is not None:
            held.move_to_end(full_key)
            return graph
    graph = await _build(spec)
    size = agents_limits_settings().graph_cache_size
    with _graphs_lock:
        for closed in [other for other in _graphs if other.is_closed()]:
            del _graphs[closed]
        held = _graphs.setdefault(loop, OrderedDict())
        held[full_key] = graph
        held.move_to_end(full_key)
        while len(held) > size:
            held.popitem(last=False)
    return graph


@register_settings_reset
def reset_tools_agent_graphs() -> None:
    """Drop every loop's compiled graphs so a settings reload compiles afresh."""
    with _graphs_lock:
        _graphs.clear()
