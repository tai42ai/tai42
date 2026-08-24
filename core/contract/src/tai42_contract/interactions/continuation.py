"""The driver-continuation context for an async ``ask_user``.

Two generic context variables naming registered tools a resuming driver binds:

* the RESUME continuation — the tool that resumes the CURRENT driver when a tool
  async-suspends. The resuming driver SETS it around a tool dispatch; the platform
  READS it when an ``ask_user`` is raised with ``mode="async"``, stamping the value
  onto the parked interaction as its ``continuation_tool``.
* the COMPLETION continuation — the tool a driver fires with the FINAL answer when a
  resumed run drives to a clean terminal (not a re-park), so a deferred response is
  delivered out of band, PLUS an opaque address context the delivery tool reads to route
  that answer back. A driver binds both around a run and keeps them with its own resume
  state — the platform stores no completion field on the interaction; a name of ``None``
  (the default) means no completion delivery is wired.

The resume continuation is a bare tool name; the completion continuation is a tool name
paired with an opaque context. Both are a generic platform mechanism that never names a
flow, a session, or any engine state — the context is fully opaque to this contract (it
carries no route/flow/thread meaning here) and MUST be JSON-serializable, since a durable
resume path round-trips it. A resume name of ``None`` (the default) means no resuming
driver is bound, and an async ask raised there fails loudly.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextvars import ContextVar, Token
from datetime import datetime
from typing import Any, Final, cast

__all__ = [
    "EXPIRY_ANSWER",
    "PARK_COMPLETION_FAILED",
    "PARK_COMPLETION_SUCCEEDED",
    "SUSPENDED_INTERACTION_MARKER_KEY",
    "get_park_completion",
    "get_resume_continuation_tool",
    "read_suspended_interaction_marker",
    "reset_park_completion",
    "reset_resume_continuation_tool",
    "set_park_completion",
    "set_resume_continuation_tool",
    "suspended_interaction_marker",
]

_resume_continuation_tool: ContextVar[str | None] = ContextVar("tai42_resume_continuation_tool", default=None)

# The completion binding: a (tool name, opaque context) pair a driver fires on a clean
# terminal drive. Held as ONE immutable tuple so both halves bind and reset atomically. The
# default ``(None, None)`` means no completion delivery is wired.
_ParkCompletion = tuple[str | None, Mapping[str, Any] | None]

_park_completion: ContextVar[_ParkCompletion] = ContextVar("tai42_park_completion", default=(None, None))


# The answer value a continuation receives when its interaction expired unanswered — the
# generic marker a resuming consumer reads to run its expiry branch instead of a real answer.
EXPIRY_ANSWER: Final[dict[str, bool]] = {"tai42:interaction_expired": True}


# The shared terminal-status vocabulary a completion fire names its outcome with. The
# resuming driver (which owns the parked run's terminal) and the delivery tool (which maps
# the outcome back to the caller) both key on these SAME two strings, so neither side has to
# name the other to agree on the wire vocabulary — the contract is the single shared point.
#
# The generic completion-fire payload a resuming driver dispatches the bound delivery tool
# with is the opaque bound context merged with the terminal outcome:
#
#     {**bound_context, "result": <terminal outcome>, "completion_id": <str>, "status": <status>}
#
# where ``bound_context`` is the :func:`set_park_completion` context (opaque to the contract),
# ``result`` is the run's terminal outcome value, ``completion_id`` is the stable idempotency
# id of the resolved terminal, and ``status`` is one of the two constants below.
# ``PARK_COMPLETION_SUCCEEDED`` names a clean-success terminal whose ``result`` the delivery
# tool maps back to the caller; ``PARK_COMPLETION_FAILED`` names ANY non-success terminal
# (failed/stopped/aborted/errored) the delivery tool surfaces as its uniform notice. A
# delivery tool treats ``FAILED`` as the fail-safe default, so an unstamped fire never pushes
# a non-success payload through the success mapping.
PARK_COMPLETION_SUCCEEDED: Final[str] = "succeeded"
PARK_COMPLETION_FAILED: Final[str] = "failed"


# The reserved key a platform-produced async-park RESULT carries in place of an answer,
# so a resuming driver recognizes the park by the RESULT shape (never a tool name). The
# in-process client-tool seam stamps it onto the tool result when an async ``ask_user``
# returns its ``SuspendedInteraction`` sentinel; a resuming driver reads it back off the
# serialized tool output. Generic: it carries only the parked interaction id + deadline,
# no driver or engine state.
SUSPENDED_INTERACTION_MARKER_KEY: Final[str] = "tai42:suspended_interaction"


def suspended_interaction_marker(interaction_id: str, expiry_at: datetime | None) -> dict[str, Any]:
    """Build the reserved marker dict a platform-produced async park returns in place
    of an answer. ``expiry_at`` is rendered ISO-8601 (or ``None``) so the marker
    round-trips through JSON tool-output serialization unchanged."""
    return {
        SUSPENDED_INTERACTION_MARKER_KEY: {
            "interaction_id": interaction_id,
            "expiry_at": expiry_at.isoformat() if expiry_at is not None else None,
        }
    }


def read_suspended_interaction_marker(content: Any) -> dict[str, Any] | None:
    """Read the reserved park marker back off a tool result, or ``None`` when the
    result is not a park.

    The tool-output serialization JSON-dumps a dict result to a string, so a
    resuming driver may see the marker as either the live dict or its JSON string;
    both are recognized. A ``str`` that is not JSON, or a JSON value without the
    reserved key, is a normal (non-park) result and yields ``None``. Returns the
    ``{"interaction_id", "expiry_at"}`` payload when the marker is present."""
    value: Any = content
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None
    if isinstance(value, dict) and SUSPENDED_INTERACTION_MARKER_KEY in value:
        return cast("dict[str, Any]", value[SUSPENDED_INTERACTION_MARKER_KEY])
    return None


def get_resume_continuation_tool() -> str | None:
    """The tool that resumes the current driver if a tool async-suspends, or ``None``
    when no resuming driver is bound (the default, and any code outside a driver
    dispatch)."""
    return _resume_continuation_tool.get()


def set_resume_continuation_tool(tool_name: str | None) -> Token[str | None]:
    """Bind ``tool_name`` as the current driver's resume continuation and return the
    reset token. The driver calls this around a tool dispatch; pass the returned
    token to :func:`reset_resume_continuation_tool` to restore the previous value."""
    return _resume_continuation_tool.set(tool_name)


def reset_resume_continuation_tool(token: Token[str | None]) -> None:
    """Restore the resume continuation to the value captured in ``token`` by the
    matching :func:`set_resume_continuation_tool` call."""
    _resume_continuation_tool.reset(token)


def get_park_completion() -> _ParkCompletion:
    """The ``(tool, context)`` a driver fires with a resumed run's FINAL answer: the tool
    that delivers the deferred answer and the opaque context it reads to route it. Both are
    ``None`` when no completion delivery is bound (the default, and any code outside a bound
    run)."""
    return _park_completion.get()


def set_park_completion(tool: str | None = None, context: Mapping[str, Any] | None = None) -> Token[_ParkCompletion]:
    """Bind ``tool`` as the current run's completion continuation, carrying an opaque
    ``context`` the delivery tool reads to route the answer, and return the reset token. A
    driver calls this around a run whose deferred final answer must be delivered out of
    band; ``context`` is treated as fully opaque here and MUST be JSON-serializable. Pass
    the returned token to :func:`reset_park_completion` to restore the previous value.

    ``tool`` defaults to ``None``: a driver on a run-face that carries no out-of-band
    delivery still binds a completion (typically to reset a prior binding for the nested
    run), naming no delivery tool."""
    return _park_completion.set((tool, context))


def reset_park_completion(token: Token[_ParkCompletion]) -> None:
    """Restore the completion continuation to the value captured in ``token`` by the
    matching :func:`set_park_completion` call."""
    _park_completion.reset(token)
