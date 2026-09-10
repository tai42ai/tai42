"""The process-wide preset-delete referee registry — the body behind
``app.tools.register_delete_referee``.

A holder of resources keyed on a preset/tool name (a plugin, e.g. a flow engine holding
per-node state bindings that reference a preset) registers an async referee that, given
the name about to be deleted, either CASCADES its own cleanup and returns an empty list
(allow) or VETOES by returning human-readable descriptions of the references it will not
let the delete strand. The delete gate consults EVERY registered referee for the name and
blocks the delete when any answer is non-empty; a referee raising fails the delete loudly
(never a silent bypass). The platform does not order referees, so a referee that intends
to cascade must run its own veto check first.

Reset on every ``start()`` (like the rename-referee registry) so a reload re-imports the
tool modules and re-registers cleanly. Registering the same provider object twice raises
loudly — a double registration is a bug, never a silent duplicate consult.
"""

from __future__ import annotations

from tai42_contract.tools import ToolDeleteReferee


class ToolDeleteRefereeRegistry:
    def __init__(self) -> None:
        self._referees: list[ToolDeleteReferee] = []

    def register(self, provider: ToolDeleteReferee) -> None:
        if provider in self._referees:
            raise ValueError("this delete referee is already registered")
        self._referees.append(provider)

    def all(self) -> list[ToolDeleteReferee]:
        return list(self._referees)

    def reset(self) -> None:
        self._referees.clear()
