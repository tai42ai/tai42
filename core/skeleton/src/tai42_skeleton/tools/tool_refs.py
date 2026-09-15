"""The process-wide per-tool tool-references registry.

The body behind a base tool's ``tool_refs`` declaration on ``@app.tools.tool``.

A base tool declares, at registration, how to read the tool names a preset of it
composes out of that preset's ``fixed_kwargs``. The preset reference collector
consults the registered extractor for a body's ``base_tool``.

Reset on every ``start()`` (like the write-validator registry) so a reload
re-imports the tool modules and re-registers cleanly; a duplicate name within one
load raises loudly (a silent overwrite could swap a base tool's declaration out
from under it).
"""

from __future__ import annotations

from tai42_contract.tools import ToolRefsExtractor


class ToolRefsRegistry:
    """Registry mapping a base tool name to its tool-references extractor."""

    def __init__(self) -> None:
        """Create an empty registry."""
        self._extractors: dict[str, ToolRefsExtractor] = {}

    def register(self, name: str, extractor: ToolRefsExtractor) -> None:
        """Register ``extractor`` for base tool ``name``; a duplicate name raises loudly."""
        if name in self._extractors:
            raise ValueError(f"tool-references extractor for tool {name!r} is already registered")
        self._extractors[name] = extractor

    def get(self, name: str) -> ToolRefsExtractor | None:
        """Return the extractor registered for ``name``, or ``None`` when none is."""
        return self._extractors.get(name)

    def reset(self) -> None:
        """Clear every registered extractor (called on each ``start()``)."""
        self._extractors.clear()
