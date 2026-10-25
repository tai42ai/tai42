"""What an agent run's ``run`` root records: the turn the run was given and what the run produced.

* :func:`run_input` is the run's input: the turn arguments a face was given, those left unset dropped.
* :class:`TerminalEvents` gathers a streamed run's terminal events (``final=True``), the output its root
  records when the run ends.

A block run records its returned result as its output.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.agent.events import StreamEvent


def run_input(**turn: Any) -> dict[str, Any]:
    """The run's input: each turn argument given, an unset (``None``) one left out."""
    return {name: value for name, value in turn.items() if value is not None}


class TerminalEvents:
    """A streamed run's terminal events in the order they were yielded."""

    __slots__ = ("events",)

    def __init__(self) -> None:
        """Start with no terminal event."""
        self.events: list[StreamEvent] = []

    def note(self, event: StreamEvent) -> StreamEvent:
        """Keep ``event`` when it is terminal, and hand it back for the caller to yield."""
        if event.final:
            self.events.append(event)
        return event

    def has(self, *kinds: type[StreamEvent]) -> bool:
        """Whether a terminal event of one of ``kinds`` was kept."""
        return any(isinstance(event, kinds) for event in self.events)


__all__ = ["TerminalEvents", "run_input"]
