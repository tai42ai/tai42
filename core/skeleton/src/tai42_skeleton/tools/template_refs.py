"""The process-wide per-tool template-references registry.

The body behind a base tool's ``template_refs`` declaration on ``@app.tools.tool``: how
to read the stored template ids a kwargs blob handed to that tool names, for a body its
input annotations cannot show. The reference walk consults it for the settled base tool.

Reset on every ``start()`` so a reload re-imports the tool modules and re-registers
cleanly; a duplicate name within one load raises loudly.
"""

from __future__ import annotations

from tai42_contract.tools import TemplateRefsExtractor


class TemplateRefsRegistry:
    """Registry mapping a base tool name to its template-references extractor."""

    def __init__(self) -> None:
        """Create an empty registry."""
        self._extractors: dict[str, TemplateRefsExtractor] = {}

    def register(self, name: str, extractor: TemplateRefsExtractor) -> None:
        """Register ``extractor`` for base tool ``name``; a duplicate name raises loudly."""
        if name in self._extractors:
            raise ValueError(f"template-references extractor for tool {name!r} is already registered")
        self._extractors[name] = extractor

    def get(self, name: str) -> TemplateRefsExtractor | None:
        """Return the extractor registered for ``name``, or ``None`` when none is."""
        return self._extractors.get(name)

    def reset(self) -> None:
        """Clear every registered extractor (called on each ``start()``)."""
        self._extractors.clear()
