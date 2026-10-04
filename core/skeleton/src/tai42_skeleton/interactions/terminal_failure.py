"""Render a RAISED ``RunTerminalFailed``'s opaque outcome as a bounded RECORDED detail string.

A failed terminal RAISES :class:`~tai42_contract.interactions.RunTerminalFailed` carrying the
driver's outcome as an OPAQUE payload. :func:`failed_outcome_detail` is for what a door RECORDS or
LOGS about that failure: it reproduces the payload as-is, never reading a key inside it, CAPPED so a
large payload cannot bloat the recorded detail. Its callers are the conversation turns (which record
the capped detail and deliver the route's generic client-safe reply) and the synchronous run-tool
door (which LOGS the capped detail — and, separately, delivers the outcome WHOLE to its one live
caller as data, untruncated, never through this capped render). The delivery chokepoint delivers the
outcome whole through its ladder and does not use this helper at all.
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
    """The internal recorded/logged detail for a RAISED :class:`RunTerminalFailed`, capped.

    The outcome is an OPAQUE payload the driver that failed wrote; the owning door records it as-is
    (capped), never pulling ``error_kind`` / ``missing_results`` / ``session_id`` out by name. This
    is the RECORDED detail only — a live caller the sync run-tool door serves is delivered the
    outcome whole as data, never this capped render.
    """
    return f"tool run failed: {capped_repr(outcome)}"
