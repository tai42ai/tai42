"""The reserved park markers: the wire form of a park signal where the object cannot travel.

A ``SuspendedInteraction`` and a ``ResumeBuffered`` are kept by TYPE in process. Where a tool result
is serialized — a resuming driver reading a serialized tool output, an MCP ``tools/call`` result —
each travels as a reserved marker dict instead. This module holds each marker's key, its builder,
its reader, and its JSON Schema (what an MCP edge advertises beside a pausing tool's declared output).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Final, cast

__all__ = [
    "RESUME_BUFFERED_MARKER_KEY",
    "RESUME_BUFFERED_MARKER_SCHEMA",
    "SUSPENDED_INTERACTION_MARKER_KEY",
    "SUSPENDED_INTERACTION_MARKER_SCHEMA",
    "MalformedParkMarkerError",
    "read_resume_buffered_marker",
    "read_suspended_interaction_marker",
    "resume_buffered_marker",
    "suspended_interaction_marker",
]


# The reserved key a platform-produced async-park RESULT carries in place of an answer,
# so a resuming driver recognizes the park by the RESULT shape (never a tool name). The
# in-process client-tool seam stamps it onto the tool result when an async ``ask``
# returns its ``SuspendedInteraction`` sentinel; a resuming driver reads it back off the
# serialized tool output. Generic: it carries the parked interaction id, its deadline, and
# the park's resume OWNER — no driver or engine state.
SUSPENDED_INTERACTION_MARKER_KEY: Final[str] = "tai42:suspended_interaction"


def suspended_interaction_marker(
    interaction_id: str,
    expiry_at: datetime | None,
    resume_owner: str | None,
    *,
    interaction_ids: list[str],
    caller_interaction_ids: list[str],
) -> dict[str, Any]:
    """Build the reserved marker dict a platform-produced async park returns in place of an answer.

    ``expiry_at`` is rendered ISO-8601 (or ``None``) so the marker
    round-trips through JSON tool-output serialization unchanged.

    ``resume_owner`` is the park's :attr:`SuspendedInteraction.resume_owner`, carried on the
    WIRE FORM so a driver that claims the park off a serialized tool result — never having
    seen the sentinel object — can still check it owns it (:func:`~tai42_contract.interactions.assert_park_adoptable`).
    ``None`` names no owner, and no bound driver may adopt it: such a marker is treated as a
    nested/foreign park rather than silently claimed.

    ``interaction_ids`` are every ask this park represents and ``caller_interaction_ids`` the
    subset addressed to the caller (``to="caller"``); both ride the wire so a driver reading
    the marker off a serialized tool result can MERGE them when it surfaces a whole super-step
    at one tool face.

    Disclosure: the ``resume_owner`` field rides the MCP wire wherever a park sentinel is
    serialized, so it exposes an internal resume-tool NAME to the caller. That caller already
    receives the interaction's ``continuation_tool`` on the stored question, so the name is not
    new information to it; the exposure is accepted.
    """
    return {
        SUSPENDED_INTERACTION_MARKER_KEY: {
            "interaction_id": interaction_id,
            "expiry_at": expiry_at.isoformat() if expiry_at is not None else None,
            "resume_owner": resume_owner,
            "interaction_ids": list(interaction_ids),
            "caller_interaction_ids": list(caller_interaction_ids),
        }
    }


# The JSON Schema of exactly what :func:`suspended_interaction_marker` emits: the wire shape of the
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


def read_suspended_interaction_marker(content: Any) -> dict[str, Any] | None:
    """Read the reserved park marker back off a tool result, or ``None`` when the result is not a park.

    The tool-output serialization JSON-dumps a dict result to a string, so a
    resuming driver may see the marker as either the live dict or its JSON string;
    both are recognized. A ``str`` that is not JSON, or a JSON value without the
    reserved key, is a normal (non-park) result and yields ``None``. Returns the
    payload — ``interaction_id``, ``interaction_ids``, ``caller_interaction_ids``,
    ``expiry_at`` and ``resume_owner`` — when a well-formed marker is present.

    The payload is UNTRUSTED (tool-result content a model can also author), so it is
    SHAPE-CHECKED: one that is not a dict or carries no string ``interaction_id`` names nothing to
    resume and yields ``None``; one whose id lists are missing or not lists of strings raises
    :class:`MalformedParkMarkerError`. A claimer passes ``resume_owner`` to
    :func:`~tai42_contract.interactions.assert_park_adoptable`, which refuses a marker naming no owner.
    """
    value: Any = content
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None
    if not (isinstance(value, dict) and SUSPENDED_INTERACTION_MARKER_KEY in value):
        return None
    marker = cast("object", value[SUSPENDED_INTERACTION_MARKER_KEY])
    if not isinstance(marker, dict):
        return None
    payload = cast("dict[str, Any]", marker)
    interaction_id = payload.get("interaction_id")
    if not isinstance(interaction_id, str):
        return None
    for field in ("interaction_ids", "caller_interaction_ids"):
        ids = payload.get(field)
        if not isinstance(ids, list) or not all(isinstance(item, str) for item in cast("list[object]", ids)):
            raise MalformedParkMarkerError(
                f"park marker for interaction {interaction_id!r} lacks a valid {field!r} list"
            )
    return payload


# The reserved key the wire form of a :class:`~tai42_contract.interactions.models.ResumeBuffered`
# carries: a resume that left sibling asks of the same super-step open, serialized where the object
# itself cannot travel (an MCP ``tools/call`` result). Like the suspended-interaction marker it is a
# non-terminal park signal, never the tool's output.
RESUME_BUFFERED_MARKER_KEY: Final[str] = "tai42:resume_buffered"


# The JSON Schema of exactly what :func:`resume_buffered_marker` emits; an MCP edge advertises it
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


def resume_buffered_marker(remaining_ids: list[str]) -> dict[str, Any]:
    """Build the reserved marker dict a ``ResumeBuffered`` travels as on a serialized wire.

    ``remaining_ids`` are the still-unanswered asks of the resumed super-step.
    """
    return {RESUME_BUFFERED_MARKER_KEY: {"remaining_ids": list(remaining_ids)}}


def read_resume_buffered_marker(content: Any) -> dict[str, Any] | None:
    """Read the reserved resume-buffered marker back off a tool result, or ``None`` when it is not one.

    Like :func:`read_suspended_interaction_marker`, the live dict and its JSON string are both
    recognized; a value without the reserved key, or whose payload is not a dict, yields ``None``.
    The payload is UNTRUSTED, so a payload without a list-of-strings ``remaining_ids`` raises
    :class:`MalformedParkMarkerError`. Returns the payload (``remaining_ids``).
    """
    value: Any = content
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None
    if not (isinstance(value, dict) and RESUME_BUFFERED_MARKER_KEY in value):
        return None
    marker = cast("object", value[RESUME_BUFFERED_MARKER_KEY])
    if not isinstance(marker, dict):
        return None
    payload = cast("dict[str, Any]", marker)
    ids = payload.get("remaining_ids")
    if not isinstance(ids, list) or not all(isinstance(item, str) for item in cast("list[object]", ids)):
        raise MalformedParkMarkerError("resume-buffered marker lacks a valid 'remaining_ids' list")
    return payload
