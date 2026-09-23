"""The process-wide per-tool declared-extras registry.

The body behind a base tool's ``extras_keys`` declaration on ``@app.tools.tool``.

A tool declares, at registration, the door ``extras`` keys it reads through ``app.tools.extras()``.
The visit checks a door's ``extras`` against a target's declared keys before starting it and
refuses an undeclared key; a preset inherits its base tool's declared keys.

Reset on every ``start()`` (like the tool-references registry) so a reload re-imports the tool
modules and re-registers cleanly; a duplicate name within one load raises loudly (a silent
overwrite could swap a tool's declared read surface out from under it).
"""

from __future__ import annotations


class ToolExtrasRegistry:
    """Registry mapping a tool name to the frozen set of door ``extras`` keys it declares reading."""

    def __init__(self) -> None:
        """Create an empty registry."""
        self._keys: dict[str, frozenset[str]] = {}

    def register(self, name: str, keys: frozenset[str]) -> None:
        """Register ``keys`` for tool ``name``; a duplicate name raises loudly."""
        if name in self._keys:
            raise ValueError(f"extras keys for tool {name!r} are already registered")
        self._keys[name] = keys

    def get(self, name: str) -> frozenset[str] | None:
        """Return the extras keys registered for ``name``, or ``None`` when none are."""
        return self._keys.get(name)

    def reset(self) -> None:
        """Clear every registered declaration (called on each ``start()``)."""
        self._keys.clear()
