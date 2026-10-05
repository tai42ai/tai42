"""``ChannelWithdrawal`` — one delivered question the platform has taken down."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator


class ChannelWithdrawal(BaseModel):
    """One delivered question the platform has taken down: release what the channel kept for it.

    Handed to a channel's OPTIONAL :meth:`~tai42_contract.channels.Channel.withdraw` member when the
    platform tears a delivered ask down (a cancel, a thread/person erase) so the medium-side state the
    channel reserved at delivery — its pending correlation, any per-interaction sidecar — is released
    at once, freeing the slot for the next ask. The channel resolves its own state from the two facts
    it reserved under: ``interaction_id`` names the ask, and ``recipient`` the address the
    :class:`~tai42_contract.channels.ChannelDelivery` carried (``None`` means the delivery named no
    recipient, so the plugin resolved its operator-configured default).
    """

    model_config = ConfigDict(frozen=True)

    interaction_id: str
    recipient: str | None = None  # the address the ChannelDelivery carried; None -> the plugin default

    @field_validator("recipient")
    @classmethod
    def _recipient_non_empty(cls, value: str | None) -> str | None:
        # Validated non-blank-when-present exactly as ``ChannelDelivery.recipient``: a set address is a
        # real address, never a blank string the channel would resolve to nothing.
        if value is not None and not value.strip():
            raise ValueError("recipient must be a non-empty address when present")
        return value
