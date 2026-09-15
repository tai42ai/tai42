"""The interaction event-stream frames: the add/answered/removed event types and the SSE field builder."""

from __future__ import annotations

ADD_EVENT = "interaction.add"
ANSWERED_EVENT = "interaction.answered"
REMOVED_EVENT = "interaction.removed"
_EVENTS_MAXLEN = 10000


def _event_fields(
    event_type: str, interaction_id: str, group_id: str, audience: str | None, *, reason: str | None = None
) -> dict[str, str]:
    # An answered/removed event frame. ``audience`` rides it so the tail-only SSE
    # filters the frame directly (a restricted caller sees only its own); it is
    # omitted when None (an unaddressed question) — a redis stream field is never None.
    # ``reason`` TAGS a removed event with WHY the question left pending (``"cancelled"``
    # for an operator per-interaction cancel), so a live operator surface can tell a
    # deliberate withdrawal apart from a timeout/expiry removal; omitted (untagged) for
    # every other removal, exactly as before.
    fields = {"type": event_type, "interaction_id": interaction_id, "group_id": group_id}
    if audience is not None:
        fields["audience"] = audience
    if reason is not None:
        fields["reason"] = reason
    return fields
