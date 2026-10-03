"""Render a RAISED ``RunTerminalFailed``'s opaque outcome as a bounded detail string.

A failed terminal RAISES :class:`~tai42_contract.interactions.RunTerminalFailed` carrying the
driver's outcome as an OPAQUE payload. Whichever platform door owns that raise records the payload
WHOLE through :func:`failed_outcome_detail`, never reading a key inside it — a conversation turn, the
synchronous run-tool door, the delivery chokepoint. The render is capped so a large payload cannot
bloat the recorded detail. No key is pulled out of the payload by name; it is reproduced as-is.
"""

from __future__ import annotations

#: A rendered outcome is capped, so a large payload cannot bloat the recorded detail.
FAILED_OUTCOME_DETAIL_LIMIT = 200

#: Appended to a value the cap clipped, so a truncated detail never reads as a complete one.
FAILED_OUTCOME_DETAIL_ELLIPSIS = "…(truncated)"


def capped_repr(value: object) -> str:
    """``value``'s repr, clipped to :data:`FAILED_OUTCOME_DETAIL_LIMIT` with an explicit truncation marker.

    Never silently: a clipped value that read as a whole one would make a recorded detail lie about
    the payload it came from.
    """
    text = repr(value)
    if len(text) <= FAILED_OUTCOME_DETAIL_LIMIT:
        return text
    return text[:FAILED_OUTCOME_DETAIL_LIMIT] + FAILED_OUTCOME_DETAIL_ELLIPSIS


def failed_outcome_detail(outcome: object) -> str:
    """The internal detail for a RAISED :class:`RunTerminalFailed`, carrying its payload WHOLE.

    The outcome is an OPAQUE payload the driver that failed wrote; the owning door records it as-is
    (capped), never pulling ``error_kind`` / ``missing_results`` / ``session_id`` out by name.
    """
    return f"tool run failed: {capped_repr(outcome)}"
