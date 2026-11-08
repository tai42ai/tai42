"""The process-wide preset-rename cascade registry — the body behind ``app.tools.register_rename_cascade``.

A holder of records keyed on a preset/tool name registers an async cascade that, given the old and
the new name of one tool a rename moves, re-keys what it holds against the old name onto the new.
The rename calls EVERY registered cascade once for each tool it moves (the preset, then each
extension branch tool bound off it), only after every veto and guard has passed and the new name is
bound, before the old name is torn down; a cascade raising fails the rename loudly and the rename
calls back every cascade it already applied with the names swapped. The platform does not order
cascades among themselves.

Reset on every ``start()`` (like the referee registries) so a reload re-imports the tool modules
and re-registers cleanly. Registering the same provider object twice raises loudly — a double
registration is a bug, never a silent duplicate re-key.
"""

from __future__ import annotations

from tai42_contract.tools import ToolRenameCascade


class ToolRenameCascadeRegistry:
    """The process-wide registry of preset-rename cascades."""

    def __init__(self) -> None:
        """Start with no registered cascades."""
        self._cascades: list[ToolRenameCascade] = []

    def register(self, provider: ToolRenameCascade) -> None:
        """Register ``provider`` as a rename cascade, raising on a double registration."""
        if provider in self._cascades:
            raise ValueError("this rename cascade is already registered")
        self._cascades.append(provider)

    def all(self) -> list[ToolRenameCascade]:
        """Every registered rename cascade, in registration order."""
        return list(self._cascades)

    def reset(self) -> None:
        """Clear every registered cascade (called on each ``start()``)."""
        self._cascades.clear()
