"""The process-wide registry of bound tool names that can pause.

Filled at bind time from a tool's ``TOOL_META_PAUSES`` declaration and from extension stacks
holding a ``pauses=True`` extension; reset on every ``start()`` with the other per-tool
registries so a reload re-registers cleanly.
"""

from __future__ import annotations


class ToolPauseRegistry:
    """The set of bound tool names a dispatch of which can return a park signal."""

    def __init__(self) -> None:
        """Create an empty registry."""
        self._names: set[str] = set()

    def register(self, name: str) -> None:
        """Record that bound tool ``name`` can pause."""
        self._names.add(name)

    def __contains__(self, name: object) -> bool:
        """Whether bound tool ``name`` was recorded as able to pause."""
        return name in self._names

    def reset(self) -> None:
        """Clear every recorded name (called on each ``start()``)."""
        self._names.clear()
