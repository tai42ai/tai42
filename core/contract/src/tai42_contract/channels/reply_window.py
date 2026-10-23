"""``ReplyWindow`` — whether a free-form reply to one address is allowed now, and until when."""

from __future__ import annotations

from datetime import UTC
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, field_validator, model_validator


class ReplyWindow(BaseModel):
    """The free-form reply window a channel keeps for one (our identity, client address) pair.

    ``state`` is one of:

    - ``"none"`` — the pair has no reply window: a free-form send is not limited by time.
    - ``"closed"`` — a window exists for the pair and is not open now (never opened, or expired).
    - ``"open"`` — a free-form send is allowed until ``closes_at``.

    ``closes_at`` is set (an aware datetime, kept in UTC and in whole seconds) exactly when
    ``state`` is ``"open"`` and is ``None`` otherwise; any other combination raises. A
    sub-second close time is rounded down to its second, so the window is never reported
    longer than it is and every channel dumps the same ``YYYY-MM-DDTHH:MM:SSZ`` form. The state
    is what the channel knows when it is read; the medium's own rejection of a send outside its
    window stays the final check.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["open", "closed", "none"]
    closes_at: AwareDatetime | None

    @field_validator("closes_at")
    @classmethod
    def _closes_at_in_utc_whole_seconds(cls, value: AwareDatetime | None) -> AwareDatetime | None:
        return None if value is None else value.astimezone(UTC).replace(microsecond=0)

    @model_validator(mode="after")
    def _closes_at_iff_open(self) -> Self:
        if self.state == "open" and self.closes_at is None:
            raise ValueError("closes_at is required when state is 'open'")
        if self.state != "open" and self.closes_at is not None:
            raise ValueError(f"closes_at must be null when state is {self.state!r}")
        return self
