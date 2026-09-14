"""Answer-format enums for an ``ask_user`` question.

``AnswerFormat`` names the shape a question's answer takes; ``AnswerMismatchPolicy``
names what a channel-delivered ask does with a reply the answer door rejects.
"""

from __future__ import annotations

from enum import StrEnum


class AnswerFormat(StrEnum):
    TEXT = "text"
    CONFIRM = "confirm"
    SELECT = "select"
    FORM = "form"
    EXTERNAL = "external"


class AnswerMismatchPolicy(StrEnum):
    """What a channel-delivered ask does with a participant reply the answer door REJECTS (a 400 on a
    live ask — the reply did not fit the question's format).

    ``RETRY`` (the default, today's behavior): keep the ask parked and tell the participant what's
    expected so they can answer again in place. ``BRIDGE``: treat an unmatched reply as a
    DIGRESSION — keep the ask parked (no notice), and hand the reply to the conversation as a fresh
    routed turn so the flow handles it; the ask then ends ONLY by a real answer or its timeout,
    never by unmatched input. Set per ask by the tool author; a plain freeform/select ask keeps the
    default.
    """

    RETRY = "retry"
    BRIDGE = "bridge"
