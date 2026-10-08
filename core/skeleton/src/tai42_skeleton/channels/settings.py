"""``CHANNELS_*`` config for the channel delivery surfaces the skeleton hosts.

The internal notifications feed and the send-receipt index live on the interactions
Redis (``interactions_settings().redis`` and ``.key_prefix``); their retention and size
bounds are channel concerns and are declared here.
"""

from typing import ClassVar

from pydantic import Field
from pydantic_settings import SettingsConfigDict
from tai42_kit.settings import TaiBaseSettings, settings_cache


class ChannelsSettings(TaiBaseSettings):
    """Channel delivery settings: the notifications feed bounds and the send-receipt index TTL."""

    model_config = SettingsConfigDict(env_prefix="CHANNELS_")
    env_prefix_owned: ClassVar[bool] = True

    # Bound on the internal notifications sink feed (the list ``notify_user`` with
    # no channel writes). The feed is a newest-first ring buffer: each write LTRIMs
    # it to this many entries, keeping the newest N and evicting older ones by
    # design, so the feed key cannot grow without limit. A deliberate, documented
    # retention cap — not a silent truncation. Must be positive.
    notifications_feed_max: int = Field(default=1000, gt=0)

    # Idle TTL (seconds) on a PER-AUDIENCE notifications feed key, refreshed on each
    # push (30d, mirroring the answer-record retention neighbor). A per-identity feed
    # is minted one-per-distinct-audience and read non-destructively, so without an
    # expiry its key would accumulate forever; this bounds an idle identity's key.
    # The shared feed key is deliberately NOT expired here — a TTL there could drop
    # the operator inbox after a quiet period. Must be positive.
    notifications_feed_ttl_seconds: int = Field(default=30 * 86400, gt=0)

    # TTL (seconds) on a send-receipt index entry: the
    # ``provider_message_id -> {trace_id, span_id}`` map a ``notify_user`` channel send
    # writes so a later out-of-band delivery receipt can be posted back onto the
    # originating trace (the send-outcome monitoring layer's tier 2). Sized to the
    # receipt-relevance window — long enough for a delayed carrier receipt to still
    # correlate to its run, bounded (24h) so the index cannot accumulate. Must be
    # positive — a non-positive TTL would delete the entry on write.
    send_receipt_index_ttl_seconds: int = Field(default=86400, gt=0)


@settings_cache
def channels_settings() -> ChannelsSettings:
    """The cached :class:`ChannelsSettings` for this process."""
    return ChannelsSettings()
