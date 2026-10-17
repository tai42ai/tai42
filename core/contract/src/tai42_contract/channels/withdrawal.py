"""``ChannelWithdrawal`` — one delivered question the platform has taken down."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator


class ChannelWithdrawal(BaseModel):
    """One delivered question the platform has taken down: release what the channel kept for it.

    ``interaction_id`` and ``recipient`` are the two facts the channel reserved under at
    delivery: the id of the question and the address the :class:`ChannelDelivery` carried
    (``None`` when the delivery named none, so the channel resolves its own default exactly
    as its ``deliver`` did).
    """

    model_config = ConfigDict(frozen=True)

    interaction_id: str
    recipient: str | None = None  # the address the ChannelDelivery carried; None -> the channel's own default

    @field_validator("recipient")
    @classmethod
    def _recipient_non_empty(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("recipient must be a non-empty address when present")
        return value
