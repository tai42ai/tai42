"""The reserved park markers' vocabulary: the wire form of a park signal where the object cannot travel.

A ``SuspendedInteraction`` and a ``ResumeBuffered`` are kept by TYPE in process. Where a tool result
is serialized — a resuming driver reading a serialized tool output, an MCP ``tools/call`` result —
each travels as a reserved marker dict instead. This module holds each marker's reserved key, its
JSON Schema (what an MCP edge advertises beside a pausing tool's declared output) and the error a
malformed marker raises. Each marker's builder and strict reader are ``tai42_kit.interactions.park_marker``.
"""

from __future__ import annotations

from typing import Any, Final

__all__ = [
    "RESUME_BUFFERED_MARKER_KEY",
    "RESUME_BUFFERED_MARKER_SCHEMA",
    "SUSPENDED_INTERACTION_MARKER_KEY",
    "SUSPENDED_INTERACTION_MARKER_SCHEMA",
    "MalformedParkMarkerError",
]


# The reserved key a platform-produced async-park RESULT carries in place of an answer,
# so a resuming driver recognizes the park by the RESULT shape (never a tool name). The
# in-process client-tool seam stamps it onto the tool result when an async ``ask``
# returns its ``SuspendedInteraction`` sentinel; a resuming driver reads it back off the
# serialized tool output. Generic: it carries the parked interaction id, its deadline, and
# the park's resume OWNER — no driver or engine state.
SUSPENDED_INTERACTION_MARKER_KEY: Final[str] = "tai42:suspended_interaction"


# The JSON Schema of exactly what the kit's ``suspended_interaction_marker`` emits: the wire shape of the
# park signal. An MCP edge advertises it beside a pausing tool's declared output schema, so a
# client that validates ``structuredContent`` against the advertised ``outputSchema`` accepts a
# park as well as the tool's result.
SUSPENDED_INTERACTION_MARKER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        SUSPENDED_INTERACTION_MARKER_KEY: {
            "type": "object",
            "properties": {
                "interaction_id": {"type": "string"},
                "expiry_at": {"type": ["string", "null"]},
                "resume_owner": {"type": ["string", "null"]},
                "interaction_ids": {"type": "array", "items": {"type": "string"}},
                "caller_interaction_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["interaction_id", "expiry_at", "resume_owner", "interaction_ids", "caller_interaction_ids"],
            "additionalProperties": False,
        }
    },
    "required": [SUSPENDED_INTERACTION_MARKER_KEY],
    "additionalProperties": False,
}


class MalformedParkMarkerError(ValueError):
    """A park marker carries its reserved key but lacks a valid id list.

    A suspended-interaction marker that names an interaction without valid ``interaction_ids`` /
    ``caller_interaction_ids`` lists cannot say which of its asks address the caller; a
    resume-buffered marker without a valid ``remaining_ids`` list cannot say which asks are still
    open. Neither is ever read as a park.
    """


# The reserved key the wire form of a :class:`~tai42_contract.interactions.models.ResumeBuffered`
# carries: a resume that left sibling asks of the same super-step open, serialized where the object
# itself cannot travel (an MCP ``tools/call`` result). Like the suspended-interaction marker it is a
# non-terminal park signal, never the tool's output.
RESUME_BUFFERED_MARKER_KEY: Final[str] = "tai42:resume_buffered"


# The JSON Schema of exactly what the kit's ``resume_buffered_marker`` emits; an MCP edge advertises it
# beside a pausing tool's declared output schema, as it does the suspended-interaction marker's.
RESUME_BUFFERED_MARKER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        RESUME_BUFFERED_MARKER_KEY: {
            "type": "object",
            "properties": {"remaining_ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["remaining_ids"],
            "additionalProperties": False,
        }
    },
    "required": [RESUME_BUFFERED_MARKER_KEY],
    "additionalProperties": False,
}
