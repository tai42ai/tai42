"""The process-wide preset-delete cascade registry — the body behind ``app.tools.register_delete_cascade``.

A holder of resources keyed on a preset/tool name registers an async cascade that, given a name
a delete removes, cleans up what it holds against that name. The delete calls EVERY registered
cascade once for each name it removes (the preset, then each extension branch tool bound off it),
only after every veto and guard has passed and before the record is removed; a cascade raising
fails the delete loudly. The platform does not order cascades among themselves.

Reset on every ``start()`` (like the referee registries) so a reload re-imports the tool modules
and re-registers cleanly. Registering the same provider object twice raises loudly — a double
registration is a bug, never a silent duplicate cleanup.
"""

from __future__ import annotations

from tai42_contract.tools import ToolDeleteCascade


class ToolDeleteCascadeRegistry:
    """The process-wide registry of preset-delete cascades."""

    def __init__(self) -> None:
        """Start with no registered cascades."""
        self._cascades: list[ToolDeleteCascade] = []

    def register(self, provider: ToolDeleteCascade) -> None:
        """Register ``provider`` as a delete cascade, raising on a double registration."""
        if provider in self._cascades:
            raise ValueError("this delete cascade is already registered")
        self._cascades.append(provider)

    def all(self) -> list[ToolDeleteCascade]:
        """Every registered delete cascade, in registration order."""
        return list(self._cascades)

    def reset(self) -> None:
        """Clear every registered cascade (called on each ``start()``)."""
        self._cascades.clear()
