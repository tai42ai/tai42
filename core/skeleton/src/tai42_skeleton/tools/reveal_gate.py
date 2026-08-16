"""The in-process secret carry-out slot shared by tool dispatch and preset bind.

The gate is armed for the duration of a ``TransformedTool`` (preset) in-process
dispatch. The preset's forwarding fn re-enters the parent tool's ``run`` — hence its
``convert_result`` — in-process, where a wrapped secret must survive to the dispatch's
caller rather than being revealed as it is on a live MCP edge. ``convert_result`` stows
the RAW wrapper-bearing value here so the dispatch returns it intact; the preset's
output-schema guard reads the same stowed value so it validates the REVEALED result
rather than the masked projection that flows back through the transform.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any


class InprocessRevealGate:
    """The armed in-process dispatch's secret carry-out slot."""

    __slots__ = ("has_payload", "payload")

    def __init__(self) -> None:
        self.payload: Any = None
        self.has_payload = False


# Armed only inside a preset dispatch. ``None`` (the default) is the genuine-MCP-call
# state every other ``convert_result`` sees.
inprocess_reveal_gate: ContextVar[InprocessRevealGate | None] = ContextVar("inprocess_reveal_gate", default=None)


def stowed_reveal_payload() -> tuple[bool, Any]:
    """The current gate's stowed raw payload as ``(has_payload, payload)``.

    ``(False, None)`` when the gate is unarmed or carried no secret-bearing return —
    the caller then falls back to the masked projection that flowed through.
    """
    gate = inprocess_reveal_gate.get()
    if gate is None or not gate.has_payload:
        return False, None
    return True, gate.payload


# Set when an UNARMED MCP-edge reveal exposed a secret in this task's result; read by
# the preset output-schema guard to redact its failure text so a revealed secret never
# rides a logged validation error. Each MCP request runs in its own task/context, so
# the default is fresh per call — no cross-call reset is needed.
revealed_secret_presence: ContextVar[bool] = ContextVar("revealed_secret_presence", default=False)


def note_secret_reveal() -> None:
    """Record that an unarmed MCP-edge reveal exposed a secret in this task."""
    revealed_secret_presence.set(True)


def secret_was_revealed() -> bool:
    """Whether an unarmed MCP-edge reveal exposed a secret in this task."""
    return revealed_secret_presence.get()
