"""The process-wide state-template detach referee registry — the body behind
``app.tools.register_detach_referee``.

A holder of door bindings that name templates (a plugin's per-node state bindings, or the
platform's own preset/route/hook/schedule bindings) registers an async referee that, given
the ``(state name, template name)`` about to be detached, returns human-readable
descriptions of the live bindings that still name that template on that state (empty = no
objection). The detach gate consults EVERY registered referee and blocks the detach when
any answer is non-empty; a referee raising fails the detach loudly (never a silent bypass).

Reset on every ``start()`` (like the rename/delete-referee registries) so a reload
re-imports the tool modules and re-registers cleanly, and the platform-internal referees
re-arm through their startup/reload handler. Registering the same provider object twice
raises loudly — a double registration is a bug, never a silent duplicate consult.
"""

from __future__ import annotations

from tai42_contract.tools import StateTemplateDetachReferee


class StateTemplateDetachRefereeRegistry:
    def __init__(self) -> None:
        self._referees: list[StateTemplateDetachReferee] = []

    def register(self, provider: StateTemplateDetachReferee) -> None:
        if provider in self._referees:
            raise ValueError("this detach referee is already registered")
        self._referees.append(provider)

    def all(self) -> list[StateTemplateDetachReferee]:
        return list(self._referees)

    def reset(self) -> None:
        self._referees.clear()
