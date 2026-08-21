"""The in-process carry-out slots shared by tool dispatch and preset bind.

The gate is armed for the duration of a ``TransformedTool`` (preset) in-process
dispatch. The preset's forwarding fn re-enters the parent tool's ``run`` — hence its
``convert_result`` — in-process, where two kinds of return must survive to the
dispatch's caller BY VALUE rather than being flattened into a ``ToolResult``:

* a wrapped secret, which must survive intact to the dispatch's caller rather than
  being revealed as it is on a live MCP edge — ``convert_result`` stows the RAW
  wrapper-bearing value on the reveal slot so the dispatch returns it intact, and the
  preset's output-schema guard reads the same stowed value so it validates the
  REVEALED result rather than the masked projection that flows back through the
  transform;
* an async-park sentinel (``SuspendedInteraction``), which the turn engine recognizes
  by TYPE — ``convert_result`` stows it on the park slot so the dispatch returns the
  object unflattened, exactly as the direct-run path preserves it, and a later answer
  is never stranded on a lost park.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any


class InprocessRevealGate:
    """The armed in-process dispatch's carry-out slots: a secret reveal payload and
    an async-park sentinel."""

    __slots__ = ("has_park", "has_payload", "park", "payload")

    def __init__(self) -> None:
        self.payload: Any = None
        self.has_payload = False
        self.park: Any = None
        self.has_park = False


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


def stowed_park() -> tuple[bool, Any]:
    """The current gate's stowed async-park sentinel as ``(has_park, park)``.

    ``(False, None)`` when the gate is unarmed or the dispatch carried no park — the
    caller then treats the return as an ordinary tool result.
    """
    gate = inprocess_reveal_gate.get()
    if gate is None or not gate.has_park:
        return False, None
    return True, gate.park


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
