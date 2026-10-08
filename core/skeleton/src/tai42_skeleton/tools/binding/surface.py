"""The tool surface's generation: one owner of every add to and removal from the listed tool set.

Every mutation of the main server's tool surface goes through :func:`add_surface_tool` /
:func:`remove_surface_tool`, which bump a process-wide generation AFTER the mutation, so a
cache filled from the old surface is stored under the old generation. Process-wide, not per
server: the server is rebuilt per serving epoch, and a per-server counter would restart.
:class:`SurfaceToolIndex` is the serving core's name lookup keyed on it.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from fastmcp.tools import Tool

_generation = 0
_lock = threading.Lock()


def tool_surface_generation() -> int:
    """The current tool-surface generation (monotonic within the process)."""
    return _generation


def bump_tool_surface_generation() -> int:
    """Advance the tool-surface generation and return the new value."""
    global _generation
    with _lock:
        _generation += 1
        return _generation


def add_surface_tool(fast_mcp: FastMCP, tool: Tool) -> Tool:
    """Add ``tool`` to ``fast_mcp``'s listed surface, then bump the generation."""
    added = fast_mcp.add_tool(tool)
    bump_tool_surface_generation()
    return added


def remove_surface_tool(fast_mcp: FastMCP, name: str) -> None:
    """Remove the tool ``name`` from ``fast_mcp``'s listed surface, then bump the generation."""
    try:
        fast_mcp.local_provider.remove_tool(name)
    finally:
        bump_tool_surface_generation()


class SurfaceToolIndex:
    """One serving core's name-to-tool answers, valid for one tool-surface generation.

    Lives on the serving core it answers for (never module-level): two cores coexist while a
    profile apply builds a new epoch, and the generation does not tell their servers apart.
    Only an answer the core's server gave under the CURRENT generation is stored, and a store
    under a newer generation drops every older answer, so a lookup never returns a tool from
    a surface that has since changed. An unknown name is never stored.
    """

    def __init__(self) -> None:
        """Start empty, matching no generation."""
        self._lock = threading.Lock()
        self._generation = -1
        self._tools: dict[str, Tool] = {}

    def lookup(self, name: str, generation: int) -> Tool | None:
        """The tool stored for ``name`` under ``generation``, else ``None``."""
        if generation != self._generation:
            return None
        return self._tools.get(name)

    def store(self, name: str, tool: Tool, generation: int) -> None:
        """Store the server's answer ``tool`` for ``name``, read under ``generation``.

        Nothing is stored when the surface has moved since ``generation`` was read.
        """
        with self._lock:
            if generation != tool_surface_generation():
                return
            if generation != self._generation:
                # A fresh map, never a clear: a lookup that already passed its generation check
                # reads either the old map or this one, never a mix.
                self._tools = {}
                self._generation = generation
            self._tools[name] = tool
